"""The perception stack as an MCP server: look, focus, act.

Runs where the SCREEN is — this Mac, or the RDP box with Unreal on it — while
the agent runs wherever it likes. Everything below the tools is a network call
to the tier services, so this process holds no models, no icon library and no
weights: importing the whole client path pulls PIL and nothing else.

## The two-step view

    screen()          text outline of the whole screen, no image
    focus(ref)        one region or area, as a marked crop + its elements
    viewport()        the 3D view, which is never cached

A step is usually a NAVIGATION decision — which part of the screen matters —
and only then a POINTING one. Sending a 2560x1440 screenshot every step pays
for both at once, and the outline costs a few hundred tokens.

## Coordinates

Every number an agent sees is in the pixels of the image it was last handed.
It points at what it sees; `View.to_screen` maps back here. The server holds
the current view for exactly this reason — it is serving that view, and
nothing else knows what the agent is looking at.

That is also why the actions take no `ref` by default: they mean "in the thing
you are looking at". An agent that has not called `focus` or `viewport` yet is
told to, rather than having its coordinates guessed at.

`drag` is the exception, because it is the one action whose two ends can be in
different places — an asset dragged out of the content browser and into the
viewport is two focuses and two coordinate frames. So every view is kept, by
ref, and `from_ref`/`to_ref` say which frame each end was read in. They all
expire together when the region set changes, since a remembered frame then
describes a screen that is gone.

## Acting

Actuation is delegated, never reimplemented. `hermes_unreal.backend` already
knows the RDP delivery path, that a menu popup "fails" a foreground-activation
check whose restore then dismisses the menu, and which keystrokes reach Slate
popups. Reproducing that here would be reproducing its bugs too.

Every action settles on L0 before returning — the blocking gate is the cheapest
thing in the stack (~0.2s, no OCR, no icons, no grouping) and it answers exactly
"is the UI ready yet". An action that returns before the screen has caught up
reports a state that has not happened.

Every action also reports what was actually under it: the nearest element and
its distance. Feedback, not a constraint — the click goes where the agent aimed,
but a miss becomes visible on the next step instead of a silent drift.

## One display

`live.Live` is a conversation with one screen, and two agents interleaving
frames would build a cache describing neither. One lock, requests serialized.
"""
import argparse
import io
import os
import sys
import threading
import time

from mcp.server.fastmcp import FastMCP, Image

from perception import config as pcfg
from perception import groups as G
from perception import view as V
from perception.cascade import perceive
from perception.detect import Detector
from perception.icons_remote import RemoteLibrary
from perception.live import Live
from perception.ocr import Ocr
from perception.soft import naming_note, note, perceive_soft

SETTLE = float(os.environ.get("PERC_MCP_SETTLE", "4.0"))
POLL = float(os.environ.get("PERC_MCP_POLL", "0.25"))
# How long one region's reading is trusted before it is read again even though
# its pixels did not change — per region, see live.py.
TTL = float(os.environ.get("PERC_MCP_TTL", "60"))


# ------------------------------------------------------------------ screen

from mcp_server.screen import capture as _grab                # shared with the recorder


class Dry:
    """An actuator that reports instead of acting.

    Not a test double — a deployment mode. The first time this runs against a
    real machine you want to read what it WOULD have done, and retrofitting
    that after an agent has already clicked something is too late.
    """

    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        def call(*a, **kw):
            self.log.append((name, {**{f"_{i}": v for i, v in enumerate(a)},
                                    **kw}))
            shown = [repr(v) for v in a] + [f"{k}={v!r}" for k, v in kw.items()]
            return {"ok": True, "dry_run": True,
                    "would": f"{name}({', '.join(shown)})"}
        return call


def _actuator(dry=False, kind="auto"):
    """Who actually delivers the input.

    `win32` is the real one and needs nothing but this repo: real events at
    screen coordinates, injected by `act_win32`. `hermes` is the old
    cua-driver delegation, kept only for the Mac-drives-RDP topology it was
    written for — on the box it costs a ~5s window capture per anchor and
    cannot open a Slate menu at all (see act_win32's docstring).
    """
    if dry:
        return Dry()
    if kind == "auto":
        kind = "win32" if sys.platform == "win32" else "hermes"
    if kind == "win32":
        from mcp_server.act_win32 import Actuator
        return Actuator(type_mode=os.environ.get("PERC_TYPE_MODE", "unicode"))
    try:
        from hermes_unreal.backend import UnrealPerceptionBackend
    except Exception as e:                              # noqa: BLE001
        raise SystemExit(
            f"no actuator: hermes_unreal.backend did not import ({e}).\n"
            "On Windows use --actuator win32 (no extra install). Elsewhere, "
            "run with --dry-run to serve perception only.") from e
    b = UnrealPerceptionBackend()
    b.start()
    return b


# ----------------------------------------------------------------- session

class Session:
    """One screen, one cache, one current view. Serialized."""

    def __init__(self, dry=False, grab=None, actuator="auto"):
        c = pcfg.load()
        self.det = Detector(c)
        # The whole look is one request to the box when a perceive server is
        # configured (serve_perceive.py); the readers below stay as the fallback.
        from perception.perceive_remote import RemotePerceiver, configured
        self.remote = RemotePerceiver(c) if configured(c) else None
        self.ocr = Ocr(c)
        self.icons = RemoteLibrary(c)
        self.live = Live(ttl=TTL)
        self.act = _actuator(dry, actuator)
        self.grab = grab or _grab
        self.lock = threading.RLock()
        self.p = None                # the last full Perception
        self.view = None             # what the agent is looking at NOW
        # Every view still standing, by ref. A drag can start in one region and
        # end in another — dropping a picture out of a slider and onto a target
        # is two focuses and two coordinate frames — so the frame a number was
        # read in has to outlive the next focus() call. They are dropped
        # together the moment the region set changes, because after that a
        # remembered frame describes a screen that is gone.
        self.views = {}
        self.dry = dry
        self.dropped = {}          # readers that were down last look
        self.last_outline = None   # so a repeat look can answer short

    # -- perception ------------------------------------------------------

    def _capture(self):
        """Pixels and the cursor-space origin of their (0,0), together.

        From one query on purpose: taken separately they can disagree the
        moment a display is plugged in, and a stale offset is a click that
        lands somewhere plausible and wrong.
        """
        got = self.grab()
        return got if isinstance(got, tuple) else (got, (0, 0))

    def look(self, groups=True):
        """Capture and perceive. Incremental — see live.py.

        A reader that is down costs detail, not the screen: see soft.py. What
        was lost rides along in `self.dropped` and is said out loud in the
        reply, because a degraded screen the agent knows about is usable and
        one it does not is a lie.
        """
        img, self.origin = self._capture()
        why = None
        if getattr(self, "remote", None) is not None:
            from perception.perceive_remote import PerceiveError
            try:
                self.p, self.dropped = self.remote.look(img, groups=groups)
                self.img = img
                return self.p
            except PerceiveError as e:
                # The models are separate servers and may well still be up:
                # look from here instead, and say so.
                why = str(e)[:200]
        self.p, self.dropped = perceive_soft(
            img, self.det, conf={"l0": 0.25, "l1": 0.35}, ocr=self.ocr,
            icons=self.icons, groups=groups, live=self.live)
        if why:
            self.dropped = {**self.dropped, "perceive server": why}
        self.img = img
        return self.p

    def gate(self):
        """L0 only: is the UI mid-action? ~0.2s, no readers."""
        img, _ = self._capture()
        p = perceive(img, self.det, conf={"l0": 0.25}, depth=0)
        return p.blocking, p.reason

    def settle(self, timeout=SETTLE):
        """Wait for the screen to stop being busy, then say what it settled to.

        Bounded: a progress bar that never finishes must not hang the agent's
        step. Returning "still busy after 4s" is information; blocking is not.
        """
        end = time.time() + timeout
        blocking, reason = self.gate()
        while blocking and time.time() < end:
            time.sleep(POLL)
            blocking, reason = self.gate()
        if blocking:
            return f"still busy after {timeout:g}s ({reason})"
        return "idle"

    def after(self, what):
        """What every action returns: did the screen settle, and did its
        structure change under us."""
        before = [d.box.as_tuple() for d in (self.p.l1 if self.p else [])]
        state = self.settle()
        note = ""
        if before:
            self.look()
            now = [d.box.as_tuple() for d in self.p.l1]
            if now != before:
                note = ("\nThe screen's regions changed — call screen() again; "
                        "your previous indices and views are stale.")
                self.forget()
        return f"{what}\n{state}{note}"

    def near(self, x, y, ref=None):
        """What the agent's coordinate actually landed on.

        "On" means INSIDE the element's box, not merely nearest to it. Distance
        alone reads as a hit whenever a region is sparse — a click 37px into
        empty space reported as "on 'RQ'" is worse than saying nothing, because
        it tells the agent the step worked.
        """
        v = self.views.get(str(ref)) if ref is not None else self.view
        if v is None or not v.elements:
            return ""
        n, (b, kind, s), d = v.near(x, y)
        sx, sy = v.to_screen(x, y)
        if b.x <= sx <= b.x2 and b.y <= sy <= b.y2:
            return f" — on [{n}] {kind} {s[:40]!r}"
        return (f" — NOT on any element; nearest is [{n}] {kind} {s[:30]!r}, "
                f"{round(d)}px away")

    def frame(self, ref=None):
        """The view a coordinate should be read in: a named one, or the current.

        Refuses rather than guessing when there is none. A raw number with no
        frame is the one input that cannot be checked — take it as screen
        coordinates and the click lands somewhere else entirely while the reply
        still reads fine.
        """
        if ref is None:
            if self.view is None:
                raise ValueError(
                    "no current view — call focus() or viewport() first; "
                    "coordinates are read in the frame of the image you were "
                    "last shown")
            return self.view
        v = self.views.get(str(ref))
        if v is None:
            seen = ", ".join(sorted(self.views)) or "none"
            raise ValueError(f"no view for {ref!r} — focus({ref!r}) first. "
                             f"Views you still have: {seen}")
        return v

    def xy(self, x, y, ref=None):
        """A coordinate in a view -> screen, refusing anything off the screen.

        Nothing legitimate lands outside the display, so a point that does is a
        misread frame or a hallucinated number. Saying so beats delivering it:
        the OS will silently do nothing with it, and the agent would read that
        as an action that worked.
        """
        sx, sy = self.frame(ref).to_screen(x, y)
        w, h = getattr(self, "img", None).size if getattr(self, "img", None) \
            else (None, None)
        if w and not (0 <= sx < w and 0 <= sy < h):
            raise ValueError(
                f"({x},{y}) is off the screen — it maps to ({sx},{sy}) and the "
                f"screen is {w}x{h}. Coordinates are read in the image you were "
                f"last shown; check you are using that picture's numbers.")
        # ... and only now into the cursor's own space. The captured frame is
        # the union of every display, whose top-left is (0,0) of the IMAGE but
        # not of the cursor: a monitor above or left of the primary makes it
        # negative. Bounds are checked in image space above, deliberately,
        # because that is the space the agent's numbers are in.
        ox, oy = getattr(self, "origin", (0, 0))
        return sx + ox, sy + oy

    def remember(self, ref, v):
        self.views[str(ref)] = v
        self.view = v

    def forget(self):
        self.views.clear()
        self.view = None


def _region_lines(text):
    """The top-level region lines — the ones that carry the numbering."""
    return [l for l in text.splitlines() if l and not l.startswith(" ")]


def delta(before, after):
    """What changed since the last look, or None when the whole outline is due.

    The agent looks again after every action, which is right — an action that
    reports "ok" has only been delivered, not necessarily done. But the second
    look is usually the same screen, and it was costing as much as the first:
    ~280 tokens to say what the agent already knew, kept in the conversation
    and resent on every later call for the rest of the run.

    Returning None means "send the whole thing", and it is the safe answer:

    * `outline()` numbers regions by their sorted position, so one region
      appearing or vanishing renumbers everything after it. A delta is only
      sound when the region lines are byte-identical — otherwise the numbers
      the agent is holding now point somewhere else, and a diff that reads
      perfectly sends it to the wrong panel.
    * If enough areas moved, the diff stops being cheaper than the outline and
      starts being harder to read. Past that point just say it all again.
    """
    if before is None or _region_lines(before) != _region_lines(after):
        return None
    if before == after:
        return ("No change since your last look. Same regions, same areas, "
                "same numbering — the refs you have are still good.")
    import difflib
    b, a = before.splitlines(), after.splitlines()
    lines = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, b, a).get_opcodes():
        if tag == "equal":
            continue
        lines += [f"  - {x.strip()}" for x in b[i1:i2]]
        lines += [f"  + {x.strip()}" for x in a[j1:j2]]
    if len(lines) > 12:
        return None
    return ("Since your last look the regions and their numbering are "
            "unchanged. Only these areas differ:\n" + "\n".join(lines)
            + "\n\nEverything else is as you last saw it; call "
              "screen(full=True) for the whole outline.")


def build(session):
    mcp = FastMCP("perception")

    # `structured_output=False` on every tool below, deliberately.
    #
    # A tool annotated `-> str` makes FastMCP publish an output schema, and the
    # client then receives the same text TWICE: once as content, once again in
    # `structuredContent`. Measured on a real screen(): 2,534 characters to
    # carry 1,150 characters of outline, and every copy is resent on every
    # later model call for the rest of the run. Nothing here returns anything a
    # machine wants to parse — it is prose for a reader — so the schema buys
    # nothing and costs exactly 100% overhead.

    @mcp.tool(structured_output=False)
    def screen(full: bool = False) -> str:
        """What is on screen, as a text outline. Start here.

        Returns every region of the application with a one-line summary and the
        areas inside it, numbered:

            1. content browser — browse, import and manage assets
               1.1 search and filter row
               1.2 asset grid

        No image and no coordinates: this is for deciding WHERE to work. Use
        the number (`1.2`) with focus() to actually see and click something.

        **Looking again is cheap.** After an action, call this to check what
        happened: if the screen is the same you get a one-line "no change"
        rather than the whole outline again, and if only some areas moved you
        get just those. Either way the numbering you already have still holds —
        you are told explicitly when it does not. Pass `full=True` if you want
        the entire outline regardless.
        """
        with session.lock:
            session.look()
            text = V.outline(session.p)
            short = None if full else delta(session.last_outline, text)
            session.last_outline = text
            if short:
                return short + note(session.dropped)
            return text + note(session.dropped) + naming_note(text)

    @mcp.tool(structured_output=False)
    def focus(ref: str) -> list:
        """Look closely at one region or area, e.g. "1" or "1.2".

        Returns a picture of just that part of the screen, with a numbered dot
        on every element it found, and a list of those elements.

        **Coordinates in the list are in the returned image's own pixels.**
        Click using those numbers, or any position you can see in the image —
        do not convert anything.
        """
        with session.lock:
            if session.p is None:
                session.look()
            session.remember(ref, V.focus(session.p, session.img, ref))
            buf = io.BytesIO()
            session.view.image.save(buf, "PNG")
            return [Image(data=buf.getvalue(), format="png"),
                    session.view.text + note(session.dropped)]

    @mcp.tool(structured_output=False)
    def viewport() -> list:
        """Look at the 3D viewport — the rendered scene.

        Same as focus() but finds the viewport for you, wherever it is. Its
        contents change every frame and are never cached, so this is always
        current.
        """
        with session.lock:
            session.look()
            i = next((g.parent for g in session.p.groups
                      if g.label == G.RENDER_LABEL), None)
            if i is None:
                return ["No viewport on screen. Use screen() to see what is."]
            order = V._order(session.p)
            ref = str(order.index(i) + 1)
            session.remember(ref, V.focus(session.p, session.img, ref))
            buf = io.BytesIO()
            session.view.image.save(buf, "PNG")
            return [Image(data=buf.getvalue(), format="png"),
                    session.view.text]

    @mcp.tool(structured_output=False)
    def click(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
        """Click at (x, y) in the image you were last shown.

        `clicks=2` double-clicks. `button` is "left", "right" or "middle".
        Coordinates are in the last focus()/viewport() image's pixels.
        """
        with session.lock:
            sx, sy = session.xy(x, y)
            hit = session.near(x, y)
            session.act.click(x=sx, y=sy, button=button, click_count=clicks)
            return session.after(
                f"{'double-' if clicks == 2 else ''}{button}-clicked "
                f"({x},{y}){hit}")

    @mcp.tool(structured_output=False)
    def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
             from_ref: str = None, to_ref: str = None) -> str:
        """Drag from (x1, y1) to (x2, y2).

        Both points are read in the image you were last shown — so orbiting the
        viewport or moving a slider needs nothing but the four numbers.

        To drag BETWEEN two parts of the screen, name the view each point came
        from. Focus one, note the position, focus the other, note that
        position, then pass both:

            focus("6.4")   -> an asset at (120, 88) in that picture
            focus("4")     -> a spot in the viewport at (600, 350)
            drag(120, 88, 600, 350, from_ref="6.4", to_ref="4")

        Views stay usable until the screen's regions change, at which point you
        are told and must look again.
        """
        with session.lock:
            a = session.xy(x1, y1, from_ref)
            b = session.xy(x2, y2, to_ref)
            where = (f" [{from_ref} -> {to_ref}]"
                     if (from_ref or to_ref) else "")
            session.act.drag(from_xy=a, to_xy=b, button=button)
            return session.after(f"dragged ({x1},{y1}) -> ({x2},{y2}){where}"
                                 f"{session.near(x1, y1, from_ref)}")

    @mcp.tool(structured_output=False)
    def scroll(direction: str, amount: int = 3,
               x: int = None, y: int = None) -> str:
        """Scroll "up", "down", "left" or "right".

        Give (x, y) — in the last image's pixels — to scroll a particular list
        or panel; omit it to scroll whatever is under the pointer.
        """
        with session.lock:
            kw = {"direction": direction, "amount": amount}
            if x is not None and y is not None:
                kw["x"], kw["y"] = session.xy(x, y)
            session.act.scroll(**kw)
            return session.after(f"scrolled {direction} x{amount}")

    @mcp.tool(structured_output=False)
    def type_text(text: str) -> str:
        """Type text into whatever currently has keyboard focus.

        Click the field first — this does not focus anything by itself.
        """
        with session.lock:
            session.act.type_text(text)
            return session.after(f"typed {text[:60]!r}")

    @mcp.tool(structured_output=False)
    def key(keys: str) -> str:
        """Press a key or combination, e.g. "escape", "ctrl+s", "alt+tab"."""
        with session.lock:
            session.act.key(keys)
            return session.after(f"pressed {keys!r}")

    return mcp


VIEW_HELP = """\
A plain-HTTP surface for a human driving the screen directly, beside the MCP
tools but deliberately not among them.

    GET  /view/frame?scale=0.5&quality=60   -> JPEG of the whole screen
    GET  /view/boxes                       -> every tier's geometry, to overlay
    POST /view/click  {x, y, button, clicks}
    POST /view/drag   {x1, y1, x2, y2, button}
    POST /view/scroll {direction, amount, x, y}
    POST /view/type   {text}
    POST /view/key    {keys}

Coordinates here are absolute screen pixels, in the frame `/view/frame`
returned — the caller knows that image's exact size, so the mapping is one
scale factor with nothing to drift.

Not MCP tools, for two reasons. They would cost the agent tokens on every
request for something it must never use; and `click()` refuses a coordinate
without a view on purpose, because an unframed number is the one input that
cannot be checked. A human pointing at a picture has the frame in their hand.
The agent has to look first, and that stays true.
"""


def _view_app(mcp_app, session):
    """Route /view/* to the human-driving surface, everything else to MCP."""
    import io as _io
    import json as _json
    from urllib.parse import parse_qs

    async def reply(send, status, body, ctype=b"application/json"):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", ctype),
                                (b"content-length", str(len(body)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})

    async def body_of(receive):
        buf, more = b"", True
        while more:
            m = await receive()
            buf += m.get("body", b"")
            more = m.get("more_body", False)
        return _json.loads(buf or b"{}")

    async def frame(scope, send):
        q = parse_qs(scope.get("query_string", b"").decode())
        scale = float(q.get("scale", ["0.5"])[0])
        quality = int(q.get("quality", ["60"])[0])
        with session.lock:
            img, origin = session._capture()
        w, h = img.size
        if 0 < scale < 1:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = _io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=quality)
        data = buf.getvalue()
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"content-type", b"image/jpeg"),
            (b"content-length", str(len(data)).encode()),
            (b"cache-control", b"no-store"),
            # The full-resolution size and where its (0,0) sits in cursor
            # space, so a click read off this image can be mapped back exactly.
            (b"x-screen-width", str(w).encode()),
            (b"x-screen-height", str(h).encode()),
            (b"x-screen-origin", f"{origin[0]},{origin[1]}".encode())]})
        await send({"type": "http.response.body", "body": data})

    async def boxes(scope, send):
        """Every tier's geometry for the current screen, to draw over a frame.

        A tier at a time, because they answer different questions and overlap:
        L0 says where the app is, L1 the regions inside it, groups the areas
        inside those, and text/icons the things you can actually click. Seeing
        which tier produced a box is how you tell a bad region from a bad name.
        """
        from perception import groups as G
        with session.lock:
            session.look()
            p = session.p
        def rect(b):
            return [int(b.x), int(b.y), int(b.x + b.w), int(b.y + b.h)]
        out = {
            "size": list(p.size),
            "l0": [{"box": rect(d.box), "label": d.cls, "conf": round(d.conf, 3)}
                   for d in p.l0],
            "l1": [{"box": rect(d.box), "label": f"region {i + 1}",
                    "conf": round(d.conf, 3)} for i, d in enumerate(p.l1)],
            "groups": [{"box": rect(g.box), "label": g.label,
                        "parent": g.parent} for g in p.groups],
            "text": [{"box": rect(t.box), "label": t.text} for t in p.text],
            "icons": [{"box": rect(m.box), "label": m.label or m.icon}
                      for m in p.icons],
        }
        body = _json.dumps(out).encode()
        await reply(send, 200, body)

    async def act(path, b):
        """Absolute screen coordinates -> the actuator, with the origin applied."""
        with session.lock:
            ox, oy = getattr(session, "origin", (0, 0))
            if path == "/view/click":
                session.act.click(x=int(b["x"]) + ox, y=int(b["y"]) + oy,
                                  button=b.get("button", "left"),
                                  click_count=int(b.get("clicks", 1)))
            elif path == "/view/drag":
                session.act.drag(from_xy=(int(b["x1"]) + ox, int(b["y1"]) + oy),
                                 to_xy=(int(b["x2"]) + ox, int(b["y2"]) + oy),
                                 button=b.get("button", "left"))
            elif path == "/view/scroll":
                kw = {"direction": b.get("direction", "down"),
                      "amount": int(b.get("amount", 3))}
                if b.get("x") is not None:
                    kw["x"], kw["y"] = int(b["x"]) + ox, int(b["y"]) + oy
                session.act.scroll(**kw)
            elif path == "/view/type":
                session.act.type_text(b.get("text", ""))
            elif path == "/view/key":
                session.act.key(b.get("keys", ""))
            else:
                return None
            return {"ok": True}

    async def app(scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/view"):
            return await mcp_app(scope, receive, send)
        path = scope["path"].rstrip("/")
        try:
            if path == "/view" or path == "/view/help":
                return await reply(send, 200, VIEW_HELP.encode(),
                                   b"text/plain; charset=utf-8")
            if path == "/view/frame":
                return await frame(scope, send)
            if path == "/view/boxes":
                return await boxes(scope, send)
            got = await act(path, await body_of(receive))
            if got is None:
                return await reply(send, 404, b'{"error":"no such view route"}')
            return await reply(send, 200, _json.dumps(got).encode())
        except Exception as e:                                   # noqa: BLE001
            msg = _json.dumps({"error": f"{type(e).__name__}: {e}"})
            return await reply(send, 500, msg.encode())

    return app


def _log_to_stderr():
    """Keep everything that is not JSONRPC off stdout.

    Under `stdio` transport stdout IS the protocol channel: one stray line and
    the client fails to parse a message. A one-line startup banner did exactly
    that here — the session survived only because the client skips unparseable
    lines, which is not something to rely on.

    Logging is pointed at stderr and the chatty clients are turned down.
    stdout is deliberately NOT redirected: FastMCP writes the protocol through
    it, so capturing it would send JSONRPC to stderr and break everything.
    """
    import logging
    logging.basicConfig(stream=sys.stderr,
                        level=os.environ.get("PERC_MCP_LOG", "WARNING").upper())
    for name in ("httpx", "httpcore", "openai", "sceneprogllm", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _bearer(app, token):
    """A single shared secret in front of the whole server.

    Not a substitute for the tailnet — that is the real boundary — but the box
    runs several services and an unauthenticated one that can click things is a
    different class of thing from one that can only answer questions.
    """
    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization") != f"Bearer {token}".encode():
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await app(scope, receive, send)
    return guarded


def _own_names():
    """Every name this machine might legitimately be addressed by."""
    import socket
    out = {"localhost", "127.0.0.1", "[::1]"}
    try:
        me = socket.gethostname()
        out.add(me)
        for info in socket.getaddrinfo(me, None):
            out.add(info[4][0])
    except OSError:
        pass
    return out


def _security(host, allow):
    """Which `Host:` headers this server will answer to.

    The MCP SDK allows only localhost by default, so a browser on the machine
    cannot be used as a confused deputy against a local server. That default is
    wrong the moment you bind to 0.0.0.0 on purpose: every remote call comes
    back 421 *after* passing the bearer, which reads like a broken server
    rather than a policy.

    A server told to listen off-box therefore has to say what it answers to.
    Names are matched with a wildcard port, since the client may reach us
    through a forwarded one.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    if "*" in allow:
        # Deliberate opt-out: the bearer and the tailnet are the real boundary.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    names = _own_names()
    if host not in ("0.0.0.0", "::", ""):
        names.add(host)
    hosts = sorted({f"{n}:*" for n in names} | set(allow))
    return TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[f"http://{h}" for h in hosts]
                        + [f"https://{h}" for h in hosts])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--transport", default="stdio",
                    choices=["stdio", "sse", "streamable-http"],
                    help="stdio for a local agent; http to run this on the "
                         "machine with the screen and the agent elsewhere")
    ap.add_argument("--dry-run", action="store_true",
                    help="serve perception, report actions instead of "
                         "performing them")
    ap.add_argument("--actuator", default="auto",
                    choices=["auto", "win32", "hermes"],
                    help="auto picks win32 on Windows")
    ap.add_argument("--host", default=os.environ.get("PERC_MCP_HOST", "0.0.0.0"),
                    help="bind address for the http transports")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PERC_MCP_PORT", "8310")))
    ap.add_argument("--token", default=os.environ.get("PERC_MCP_TOKEN"),
                    help="require this bearer token (http transports only)")
    ap.add_argument("--allow-host", action="append", default=[],
                    metavar="HOST[:PORT]",
                    help="accept this Host header as well as our own names; "
                         "repeatable. A tailnet address usually needs this — "
                         "it is not on the interface list getaddrinfo returns, "
                         "so it cannot be discovered. Pass '*' to turn the "
                         "check off.")
    a = ap.parse_args(argv)
    _log_to_stderr()
    if a.dry_run:
        print("DRY RUN: actions will be reported, not performed",
              file=sys.stderr, flush=True)
    G.warm()                     # build the grouping client off the first call
    s = Session(dry=a.dry_run, actuator=a.actuator)
    mcp = build(s)
    mcp.settings.host, mcp.settings.port = a.host, a.port
    if a.transport != "stdio":
        mcp.settings.transport_security = _security(a.host, a.allow_host)
        print("answering to Host: "
              f"{', '.join(mcp.settings.transport_security.allowed_hosts) or 'any'}",
              file=sys.stderr, flush=True)
    if a.transport == "streamable-http" and a.token:
        import uvicorn
        uvicorn.run(_bearer(_view_app(mcp.streamable_http_app(), s), a.token),
                    host=a.host, port=a.port, log_level="warning")
        return
    if a.transport != "stdio" and not a.token:
        print("WARNING: serving over http with no --token; anything that can "
              "reach this port can drive the machine", file=sys.stderr)
    mcp.run(transport=a.transport)


if __name__ == "__main__":
    main()
