"""Deskhand: a computer-use agent that works THIS Windows machine.

    eyes    StudioApprentice perception (repos/StudioApprentice-perception), its
            models served locally by scripts/stack.py
    tools   StudioApprentice studio's MCP server (mcp_server/serve_mcp.py, from
            vendor/studio or repos/studio) run in-process: screen / focus /
            viewport / click / drag / scroll / type_text / key, with real
            Windows input (act_win32)
    brain   an OpenAI model through the Responses API, calling those tools

    .venv\\Scripts\\python scripts\\cua_agent.py "Add a UV sphere" --launch blender --close

Every run writes runs/agent/<time>-<slug>/: steps.jsonl (each tool call, its
arguments, its reply and how long it took), the pictures the model was shown,
a screenshot before and after, and result.json.

The guard: input only ever reaches windows of `--app`. A click whose point is
over any other window, or a key while another window has focus, is refused and
the model is told why. This machine is the one being worked on, and an agent
that clicks into the wrong window types into someone's editor.
"""
import argparse
import asyncio
import base64
import ctypes
import io
import json
import os
import re
import sys
import threading
import time
from ctypes import wintypes

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
HERE = os.path.dirname(os.path.abspath(__file__))
CUA = os.path.dirname(HERE)
# studio's tools: a vendored copy (the public repo) or a full checkout.
STUDIO = next((p for p in (os.path.join(CUA, "vendor", "studio"),
                           os.path.join(CUA, "repos", "studio"))
               if os.path.isdir(os.path.join(p, "mcp_server"))),
              os.path.join(CUA, "vendor", "studio"))
PERC = os.path.join(CUA, "repos", "StudioApprentice-perception")
for p in (STUDIO, PERC):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("PERCEPTION_CONFIG", os.path.join(CUA, "perception.json"))
# No see cards here, so the proxy L2 has to be asked for each region's areas;
# without this the outline lists regions with nothing under them.
os.environ.setdefault("PERC_GROUP_ASK", "1")
# StudioApprentice's 6 s default is tuned for its GPU server's link to OpenAI;
# from here a big panel's area call took longer, and a timed-out call leaves
# the region with no areas.
os.environ.setdefault("PERC_GROUP_TIMEOUT", "15")
# act_win32 types by injecting Unicode characters (VK_PACKET), which Unreal's
# Slate accepts and Blender ignores: on 2026-10-20 type_text("Box") left a
# Blender rename field reading "Cube", while the same letters sent as key
# presses renamed it. "vk" synthesises real keystrokes for each character.
os.environ.setdefault("PERC_TYPE_MODE", "vk")
# OPENAI_API_KEY comes from the environment, else from the first of these
# files that has it (KEY=value lines): $DESKHAND_ENV_FILE, then .env here.
ENV_FILES = [f for f in (os.environ.get("DESKHAND_ENV_FILE"),
                         os.path.join(CUA, ".env")) if f]


def _load_key():
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return
    for f in ENV_FILES:
        try:
            for line in open(f, encoding="utf-8"):
                k, _, v = line.strip().partition("=")
                if k.strip() == "OPENAI_API_KEY" and v.strip():
                    os.environ["OPENAI_API_KEY"] = v.strip().strip('"').strip("'")
                    return
        except OSError:
            pass
    raise SystemExit("no OPENAI_API_KEY: set it in the environment, or put "
                     f"OPENAI_API_KEY=... in {os.path.join(CUA, '.env')}")


# ------------------------------------------------------------------ keys

def _more_keys():
    """Numpad keys for act_win32, which has none, and Blender's views need
    them: numpad7 is top view, a bare 7 is the number row and does something
    else entirely. Added to its tables at runtime, so the studio repo is
    untouched."""
    from mcp_server import act_win32 as A
    A.VK.update({f"numpad{i}": 0x60 + i for i in range(10)})
    A.VK.update({"numpadmultiply": 0x6A, "numpadplus": 0x6B,
                 "numpadminus": 0x6D, "numpadperiod": 0x6E,
                 "numpaddivide": 0x6F})
    for i in range(10):
        A.ALIASES[f"num{i}"] = A.ALIASES[f"kp{i}"] = f"numpad{i}"
    A.ALIASES.update({"numpad.": "numpadperiod", "num.": "numpadperiod",
                      "kp.": "numpadperiod", "numpaddecimal": "numpadperiod",
                      "numpad*": "numpadmultiply", "numpad/": "numpaddivide",
                      "numpad-": "numpadminus"})
    A._EXTENDED.add(0x6F)


# ----------------------------------------------------------------- guard

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32.WindowFromPoint.argtypes = [wintypes.POINT]
_u32.WindowFromPoint.restype = wintypes.HWND
_u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_u32.GetAncestor.restype = wintypes.HWND
_u32.GetForegroundWindow.restype = wintypes.HWND
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.SetForegroundWindow.argtypes = [wintypes.HWND]
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_u32.GetWindow.restype = wintypes.HWND
_u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                             wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]


def _owner(hwnd):
    """`(pid, exe name)` of the process owning a window's top-level window."""
    if not hwnd:
        return 0, ""
    root = _u32.GetAncestor(hwnd, 2) or hwnd            # GA_ROOT
    pid = wintypes.DWORD()
    _u32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    h = _k32.OpenProcess(0x1000, False, pid.value)       # QUERY_LIMITED_INFORMATION
    if not h:
        return pid.value, ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return pid.value, os.path.basename(buf.value).lower()
        return pid.value, ""
    finally:
        _k32.CloseHandle(h)


def owner_at(x, y):
    """The process owning the window at a screen point. Our own windows (the
    panel's frame and pointer, which clicks pass through) are skipped: what
    counts is the window the click would actually reach."""
    hwnd = _u32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    got = _owner(hwnd)
    if got[0] != os.getpid():
        return got
    h = _u32.GetAncestor(hwnd, 2)
    while True:
        h = _u32.GetWindow(h, 2)                               # GW_HWNDNEXT
        if not h:
            return 0, ""
        if not _u32.IsWindowVisible(h):
            continue
        r = wintypes.RECT()
        _u32.GetWindowRect(h, ctypes.byref(r))
        if r.left <= x < r.right and r.top <= y < r.bottom:
            o = _owner(h)
            if o[0] != os.getpid():
                return o


def owner_focused():
    return _owner(_u32.GetForegroundWindow())


class Refused(RuntimeError):
    pass


class _PE32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PE32)]


def family(pid):
    """`pid` and every process descended from it. Chrome shows its Save As
    dialog from a child utility process, not the browser process (2026-10-20:
    the dialog's window belonged to a chrome.exe child of the one we
    launched), so the guard has to admit the whole tree."""
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)              # TH32CS_SNAPPROCESS
    kids = {}
    try:
        e = _PE32()
        e.dwSize = ctypes.sizeof(_PE32)
        ok = _k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            kids.setdefault(e.th32ParentProcessID, []).append(e.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    out, todo = set(), [pid]
    while todo:
        p = todo.pop()
        if p not in out:
            out.add(p)
            todo += kids.get(p, [])
    return out


_wts = ctypes.WinDLL("wtsapi32")
_wts.WTSQuerySessionInformationW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
_STATES = ["active", "connected", "connect-query", "shadow", "disconnected",
           "idle", "listen", "reset", "down", "init"]


def session_state():
    """This Windows session's connect state. Only "active" has a screen: a
    disconnected RDP session is not drawn, so screenshots fail and input goes
    nowhere — measured 2026-10-20, when the client dropped mid-suite."""
    buf, n = ctypes.c_void_p(), wintypes.DWORD()
    if not _wts.WTSQuerySessionInformationW(None, 0xFFFFFFFF, 8,  # WTSConnectState
                                            ctypes.byref(buf), ctypes.byref(n)):
        return "unknown"
    try:
        v = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int)).contents.value
        return _STATES[v] if 0 <= v < len(_STATES) else str(v)
    finally:
        _wts.WTSFreeMemory(buf)


class Guard:
    """The actuator, but only into windows of one application — and, with
    `pid`, of one process: other sessions on this machine run their own
    Blender, and those windows are someone else's work."""

    def __init__(self, act, app, pid=None, hook=None):
        self.act, self.app, self.pid, self.io = act, app.lower(), pid, act.io
        # hook(kind, points) runs before any input is sent and returns a
        # callable to run after it. The panel uses it to stop or hold the
        # agent, draw its pointer and step out of the way of a click.
        self.hook = hook

    def _before(self, kind, *pts):
        after = self.hook(kind, pts) if self.hook else None
        return after or (lambda: None)

    def _ok(self, got):
        pid, exe = got
        return exe == self.app and (self.pid is None or pid in family(self.pid))

    def _name(self):
        return f"{self.app}" + (f" (pid {self.pid})" if self.pid else "")

    # Another window in front is usually a person or a notification, not a
    # mistake of the agent's, and it passes: say so, so the agent waits
    # instead of ending the task at the first refusal.
    COVERED = (" If another window has come in front of it, this is not your "
               "doing and it usually passes: call wait(20), look again with "
               "screen(), and retry. After three such waits, end the task stuck.")

    def _at(self, x, y):
        got = owner_at(x, y)
        if not self._ok(got):
            raise Refused(f"refused: ({x},{y}) on screen is over "
                          f"{got[1] or 'no window'} (pid {got[0]}), not the "
                          f"{self._name()} you are working in." + self.COVERED)

    def _keys(self):
        got = owner_focused()
        if not self._ok(got):
            raise Refused(f"refused: the keyboard would go to "
                          f"{got[1] or 'no window'} (pid {got[0]}), not "
                          f"{self._name()}. Click inside it first." + self.COVERED)

    # The hook runs first in each method: it may hide something of ours that
    # sits over the point, which the window check below would otherwise see.

    def click(self, x, y, **kw):
        after = self._before("click", (x, y))
        try:
            self._at(x, y)
            return self.act.click(x, y, **kw)
        finally:
            after()

    def drag(self, from_xy, to_xy, **kw):
        after = self._before("drag", tuple(from_xy), tuple(to_xy))
        try:
            self._at(*from_xy)
            self._at(*to_xy)
            return self.act.drag(from_xy, to_xy, **kw)
        finally:
            after()

    def scroll(self, direction, amount=3, x=None, y=None, **kw):
        if x is None or y is None:
            pt = wintypes.POINT()
            _u32.GetCursorPos(ctypes.byref(pt))
            px, py = pt.x, pt.y
        else:
            px, py = x, y
        after = self._before("scroll", (px, py))
        try:
            self._at(px, py)
            return self.act.scroll(direction, amount, x, y, **kw)
        finally:
            after()

    def key(self, keys, **kw):
        after = self._before("key")
        try:
            self._keys()
            return self.act.key(keys, **kw)
        finally:
            after()

    def type_text(self, text, **kw):
        after = self._before("type")
        try:
            self._keys()
            return self.act.type_text(text, **kw)
        finally:
            after()

    def move(self, x, y):
        after = self._before("point", (x, y))
        try:
            self._at(x, y)
            self.io.move(x, y)
        finally:
            after()


# --------------------------------------------------------------- blender

def _find_blender():
    """$DESKHAND_BLENDER, else the newest Blender under Program Files, else PATH."""
    if os.environ.get("DESKHAND_BLENDER"):
        return os.environ["DESKHAND_BLENDER"]
    import glob
    import shutil
    found = []
    for root in {os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramW6432", "")}:
        if root:
            found += glob.glob(os.path.join(root, "Blender Foundation", "Blender *",
                                            "blender.exe"))

    def version(p):
        m = re.search(r"Blender (\d+)\.(\d+)", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0)
    return max(found, key=version) if found else (shutil.which("blender") or "")


def _find_chrome():
    """$DESKHAND_CHROME, else the usual install places, else the registry."""
    if os.environ.get("DESKHAND_CHROME"):
        return os.environ["DESKHAND_CHROME"]
    for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        p = os.path.join(os.environ.get(v, ""), r"Google\Chrome\Application\chrome.exe")
        if os.environ.get(v) and os.path.isfile(p):
            return p
    import shutil
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                                      r"\App Paths\chrome.exe") as k:
                p = winreg.QueryValue(k, None)
                if p and os.path.isfile(p):
                    return p
        except OSError:
            pass
    return shutil.which("chrome") or ""


BLENDER = _find_blender()
_WNDENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def windows_of(pid):
    """Visible top-level windows of a process, largest first."""
    found = []

    def cb(hwnd, _):
        p = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and _u32.IsWindowVisible(hwnd):
            r = wintypes.RECT()
            _u32.GetWindowRect(hwnd, ctypes.byref(r))
            found.append(((r.right - r.left) * (r.bottom - r.top), hwnd))
        return True
    _u32.EnumWindows(_WNDENUM(cb), 0)
    return [h for _, h in sorted(found, reverse=True)]


def bring_forward(hwnd):
    """Maximise and foreground. Windows only hands the foreground to a process
    that just had input, so tap alt first (the documented workaround)."""
    _u32.ShowWindow(hwnd, 3)                               # SW_MAXIMIZE
    return focus_window(hwnd)


MAGIC = 0x9E0C0DE      # act_win32's tag on the agent's own input events


def focus_window(hwnd):
    """Foreground a window without resizing it. The alt tap carries MAGIC, so
    the panel's watch on your input does not take it for you."""
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, 9)                           # SW_RESTORE
    _u32.keybd_event(0x12, 0, 0, MAGIC)
    _u32.keybd_event(0x12, 0, 2, MAGIC)
    _u32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return _u32.GetForegroundWindow() == hwnd


CHROME = _find_chrome()

BLENDER_NOTES = """\
Keys go to whatever is under the mouse pointer in Blender. To aim a shortcut,
use point(x, y) over the area first (a click there would change the
selection). Typing into a field: double-click it, key ctrl+a, type_text the
value, key enter.
In a 3D view: drag with button "middle" turns the view, scroll zooms, and the
keypad snaps it - key "numpad1" front, "numpad3" right, "numpad7" top,
"numpad5" ortho/perspective, "numpad0" camera, "numpadperiod" frame selected.
A bare "7" is the number row and does something else."""

CHROME_NOTES = """\
Keys go to the focused element: the address bar, or a text box you clicked.
key ctrl+l focuses the address bar; type_text then key enter goes there.
Web pages keep loading after an action; if one looks incomplete, wait(2) and
look again. A page longer than the window scrolls with scroll() over it.
Right-click menus, the downloads bubble and the Windows file dialogs Chrome
opens all belong to this browser, so you may work in them.
This is a fresh profile with no accounts: never sign in, buy anything or enter
personal data. You may dismiss cookie, sign-in or "make Chrome yours" prompts
(for example "No thanks", "Reject all")."""

# What `--launch <kind>` starts. Each launch is a process of our own, and the
# guard then admits input to that process only.
APPS = {
    "blender": {"exe": BLENDER, "name": "Blender", "notes": BLENDER_NOTES,
                "args": ["--factory-startup"]},
    # A separate --user-data-dir makes this its own browser process (so the
    # pid we launch is the one that owns the window) and keeps the person's
    # own Chrome profile, logins and history out of reach.
    "chrome": {"exe": CHROME, "name": "Google Chrome", "notes": CHROME_NOTES,
               "args": [f"--user-data-dir={os.path.join(CUA, 'runs', 'chrome-profile')}",
                        "--no-first-run", "--no-default-browser-check",
                        "--hide-crash-restore-bubble",
                        "--new-window", "about:blank"]},
}


def notes_for(exe):
    for app in APPS.values():
        if os.path.basename(app["exe"]).lower() == exe.lower():
            return app["name"], app["notes"]
    return exe, ""


def launch(kind, timeout=90):
    """A fresh window of our own: its pid is the only one the guard allows."""
    import subprocess
    app = APPS[kind]
    if not app["exe"] or not os.path.isfile(app["exe"]):
        raise SystemExit(f"{app['name']} was not found on this PC. Install it, or set "
                         f"DESKHAND_{kind.upper()} to its .exe")
    p = subprocess.Popen([app["exe"], *app["args"]], cwd=os.path.dirname(app["exe"]))
    end = time.time() + timeout
    while time.time() < end:
        if p.poll() is not None:
            raise SystemExit(f"{app['name']} exited at once (code {p.returncode}); "
                             f"it may have handed off to an instance already running")
        wins = windows_of(p.pid)
        if wins:
            time.sleep(3)                     # let the first draw finish
            wins = windows_of(p.pid)
            front = bring_forward(wins[0])
            time.sleep(1.5)
            return p, wins[0], front
        time.sleep(0.5)
    p.kill()
    raise SystemExit(f"{app['name']} did not open a window in time")


# ----------------------------------------------------------------- brief

SYSTEM = """\
You operate a real Windows desktop through tools. The application you work in
is {app_name}; input to any other window is refused. A person may be watching.

## Looking
1. screen() - start here. Text only: the application's regions by name and the
   areas inside them, numbered ("3" is region 3, "3.2" an area inside it). It is
   for deciding WHERE to work.
2. focus(ref) - a close-up of one region or area: a picture of just that part
   with every element it found numbered, plus a list of those elements.
3. viewport() - the 3D view, always current.
While a dialog or menu is open, screen() lists only what is inside it: deal
with it first. A ref belongs to the outline it came from; once the screen has
changed, look again rather than reuse it.

## The listing is a finding aid; the picture is right
The element list is built by a reader that guesses which label belongs to
which box. It can be out of date, it cannot see colour (selected, keyframed,
highlighted all read the same), and it sometimes misses a control or names it
oddly. When the text and the picture disagree, the picture is right. If what
you want is in neither, it is in another panel: look there rather than aiming
at the nearest thing in this one. Values carry reading noise ("0 m" as "Om").

## Coordinates
Every coordinate is in the pixels of the picture you were last shown, and you
never convert anything - the tools undo the crop and scale. Prefer the x,y of
a listed element: it lands on the middle of a real control. Use your own x,y
only for things with no element, such as a point on a canvas. Each action's
reply names what you actually hit, or says you hit nothing: read it.

## Acting
Each action waits for the screen to settle and says whether the regions
changed; if they did, your refs and views are stale - look again. An action
reply is delivery, not success: confirm the outcome by looking (focus the
panel that should have changed), not by trusting the reply.

## {app_name}
{app_notes}

## Rules
- Work through the interface only: clicks, fields, menus, shortcuts. Never
  write or run code (no Python console, no scripts, no terminal, no Run box).
- This is a real machine. Do not close windows you did not open, do not save
  over files, do not accept dialogs you did not cause. If something looks
  destructive or you are unsure, stop.
- Nobody will answer questions during a task. Do your best with what you see.
- Finish by calling end_task exactly once: status "done" when the goal's
  outcome is visible on screen (say what you saw that proves it), or "stuck"
  when you cannot get past something (say what stopped you). A plain message is
  not an ending.
"""

EXTRA_TOOLS = [
    {"type": "function", "name": "point",
     "description": "Move the mouse pointer to (x, y) in the picture you were last "
                    "shown, without clicking. Use it to aim a keyboard shortcut at "
                    "an area, since keys go to whatever is under the pointer.",
     "parameters": {"type": "object", "properties": {
         "x": {"type": "integer"}, "y": {"type": "integer"}},
         "required": ["x", "y"]}},
    {"type": "function", "name": "wait",
     "description": "Wait this many seconds (max 30) for something to finish, "
                    "then report whether the screen is idle.",
     "parameters": {"type": "object", "properties": {
         "seconds": {"type": "number"}}, "required": ["seconds"]}},
    {"type": "function", "name": "end_task",
     "description": "End the task. Call exactly once, as your last action.",
     "parameters": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["done", "stuck"]},
         "reason": {"type": "string",
                    "description": "done: what on screen proves the goal. "
                                   "stuck: what stopped you."}},
         "required": ["status", "reason"]}},
]


# ------------------------------------------------------------------ run

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "task"


def _shot(path):
    from mcp_server.screen import grab
    grab().save(path)


class Stopped(Exception):
    """The person pressed Stop."""


class Control:
    """What a person at the panel can do to a running agent: stop it, or pause
    it and let it go on. Shared by the panel's thread and the agent's."""

    def __init__(self):
        self.stop = threading.Event()
        self._go = threading.Event()
        self._go.set()
        self.why = ""

    def pause(self, why=""):
        if self._go.is_set():
            self.why = why
            self._go.clear()

    def resume(self):
        self._go.set()

    @property
    def paused(self):
        return not self._go.is_set()

    def gate(self):
        """Wait while paused; raise Stopped once stopped. True if it waited."""
        waited = False
        while not self._go.wait(0.2):
            waited = True
            if self.stop.is_set():
                raise Stopped()
        if self.stop.is_set():
            raise Stopped()
        return waited


# Tools that send input. After a pause one of these is not carried out: it
# was planned on a screen the person may since have changed.
ACTIONS = {"click", "drag", "scroll", "type_text", "key", "point"}
PAUSED_NOTE = ("not done: the person paused you and may have used the computer "
               "meanwhile. Look again with screen() before acting.")
STOPPED_NOTE = "not done: the person stopped the task."


class Run:
    """One agent on one application. `go(task)` runs a task; calling it again
    on the same Run continues the conversation, so a follow-up instruction
    ("now make it red") knows what came before.

    `ui`, when given, is the panel: `ui.event(kind, **data)` hears every step,
    and `ui.before_input(kind, points)` runs before each input event and may
    return a callable to run after it. `control` is how the panel stops or
    pauses the agent. Without either, this is the plain command-line agent."""

    def __init__(self, task, app, model, effort, max_steps, out=None, pid=None,
                 ui=None, control=None, summary=None):
        _load_key()
        _more_keys()
        from mcp_server import serve_mcp as S
        from openai import OpenAI
        self.S = S
        self.task, self.app, self.model = task, app, model
        self.effort, self.max_steps, self.out = effort, max_steps, out
        self.ui, self.control, self.summary = ui, control, summary
        self.session = S.Session(actuator="win32")
        self.session.act = Guard(self.session.act, app, pid, hook=self._hook)
        self.mcp = S.build(self.session)
        self.client = OpenAI()
        self.images_in_output = True       # flipped off if the API refuses them
        self._tools = None
        self.last_id = None                # the conversation, for follow-ups
        self.carry = []                    # outputs owed to its last calls
        # Messages the person sends while a task runs: each goes to the model
        # with the next step, so a task can be steered without stopping it.
        import queue as _queue
        self.inbox = _queue.Queue()

    # -- the panel --------------------------------------------------------

    def _emit(self, kind, **data):
        if self.ui is not None:
            try:
                self.ui.event(kind, **data)
            except Exception:                            # noqa: BLE001
                pass

    def _hook(self, kind, pts):
        """Before any input: hold while paused, refuse once stopped, then let
        the panel draw the pointer and get out of the way."""
        c = self.control
        if c is not None:
            if c.stop.is_set():
                raise Refused(STOPPED_NOTE)
            try:
                if c.gate():
                    raise Refused(PAUSED_NOTE)
            except Stopped:
                raise Refused(STOPPED_NOTE)
        if self.ui is not None:
            return self.ui.before_input(kind, pts)
        return None

    def _stopped(self):
        return self.control is not None and self.control.stop.is_set()

    # -- tools ----------------------------------------------------------

    async def tool_specs(self):
        out = []
        for t in await self.mcp.list_tools():
            out.append({"type": "function", "name": t.name,
                        "description": t.description or "",
                        "parameters": t.inputSchema})
        return out + EXTRA_TOOLS

    async def call(self, name, args):
        """-> list of ("text", str) / ("image", png bytes)."""
        s = self.session
        try:
            if name == "end_task":
                self.ended = args
                return [("text", "ended")]
            if name == "wait":
                end = time.time() + min(30.0, max(0.0, float(args.get("seconds", 1))))
                while time.time() < end and not self._stopped():
                    time.sleep(0.2)
                with s.lock:
                    return [("text", s.settle())]
            if name == "point":
                with s.lock:
                    sx, sy = s.xy(int(args["x"]), int(args["y"]))
                    s.act.move(sx, sy)
                    return [("text", f"pointer at ({args['x']},{args['y']})"
                                     f"{s.near(int(args['x']), int(args['y']))}")]
            got = await self.mcp.call_tool(name, args)
            if isinstance(got, tuple):
                got = got[0]
            out = []
            for c in got if isinstance(got, (list, tuple)) else [got]:
                kind = getattr(c, "type", None)
                if kind == "text":
                    out.append(("text", c.text))
                elif kind == "image":
                    out.append(("image", base64.b64decode(c.data)))
                elif isinstance(c, dict):
                    out.append(("text", json.dumps(c)))
                else:
                    out.append(("text", str(c)))
            return out or [("text", "(no output)")]
        except Exception as e:                           # noqa: BLE001
            msg = str(e)
            # FastMCP wraps the tool's own error; the model needs only that.
            msg = re.sub(r"^Error executing tool \w+: ", "", msg)
            return [("text", f"error: {msg}")]

    def _save_image(self, png):
        self.n_img += 1
        name = f"img{self.n_img:03d}.png"
        with open(os.path.join(self.dir, name), "wb") as f:
            f.write(png)
        return name

    def _output(self, parts):
        items, names = [], []
        for kind, v in parts:
            if kind == "text":
                items.append({"type": "input_text", "text": v})
            else:
                names.append(self._save_image(v))
                items.append({"type": "input_image", "detail": "high",
                              "image_url": "data:image/png;base64,"
                                           + base64.b64encode(v).decode()})
        return items, names

    # -- the loop ---------------------------------------------------------

    def _create(self, input_, prev):
        name, notes = notes_for(self.app)
        reasoning = {"effort": self.effort}
        if self.summary:
            reasoning["summary"] = self.summary
        kw = dict(model=self.model,
                  instructions=SYSTEM.format(app_name=name, app_notes=notes),
                  input=input_, tools=self._tools, parallel_tool_calls=False,
                  reasoning=reasoning)
        if prev:
            kw["previous_response_id"] = prev
        r = self.client.responses.create(**kw)
        u = getattr(r, "usage", None)
        if u:
            self.usage["input"] += u.input_tokens or 0
            self.usage["output"] += u.output_tokens or 0
            d = getattr(u, "input_tokens_details", None)
            self.usage["cached"] += (getattr(d, "cached_tokens", 0) or 0) if d else 0
        self.usage["calls"] += 1
        return r

    def _report(self, resp):
        """Say what the model thought and said in this response."""
        for o in resp.output:
            if o.type == "reasoning":
                for s in getattr(o, "summary", None) or []:
                    t = getattr(s, "text", "")
                    if t.strip():
                        self._log(kind="think", text=t)
                        self._emit("think", text=t)
        said = " ".join(c.text for o in resp.output if o.type == "message"
                        for c in o.content if getattr(c, "type", "") == "output_text")
        if said.strip():
            self._log(kind="say", text=said)
            self._emit("say", text=said)
            print(f"[model] {said.strip()[:300]}")

    def _log(self, **rec):
        rec["t"] = round(time.time(), 2)
        self.steps.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.steps.flush()

    def _fallback(self, outputs, extra, prev, e):
        """The API refused images inside a tool output: send them as a
        message instead, now and from here on."""
        print(f"(images in tool output refused: {str(e)[:160]}; "
              f"sending them as a message instead)")
        self.images_in_output = False
        fixed, more = [], []
        for o in outputs:
            out = o["output"]
            if isinstance(out, str):
                fixed.append(o)
                continue
            txt = "\n".join(i["text"] for i in out if i["type"] == "input_text")
            imgs = [i for i in out if i["type"] == "input_image"]
            fixed.append({**o, "output": txt or "ok"})
            if imgs:
                more.append({"role": "user", "content": [
                    {"type": "input_text", "text": "(the returned picture)"}] + imgs})
        return self._create(fixed + more + extra, prev)

    async def go(self, task=None):
        task = task or self.task
        self.task = task
        t0 = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.dir = self.out or os.path.join(CUA, "runs", "agent", f"{stamp}-{_slug(task)}")
        os.makedirs(self.dir, exist_ok=True)
        self.steps = open(os.path.join(self.dir, "steps.jsonl"), "a", encoding="utf-8")
        self.n_img, self.ended, stopped = 0, None, False
        self.usage = {"input": 0, "output": 0, "cached": 0, "calls": 0}
        _shot(os.path.join(self.dir, "before.png"))
        if self._tools is None:
            self._tools = await self.tool_specs()
        first = await self.call("screen", {"full": True})
        first_text = first[0][1]
        follow = self.last_id is not None
        self._log(kind="start", task=task, model=self.model, effort=self.effort,
                  screen=first_text, follow_up=follow)
        self._emit("start", task=task, dir=self.dir, screen=first_text)
        print(f"--- task: {task}\n--- run dir: {self.dir}\n{first_text}\n")
        if follow:
            user = (f"New instruction from the person: {task}\n\nThe screen now "
                    f"(screen()):\n\n{first_text}")
            resp = self._create(self.carry + [{"role": "user", "content": user}],
                                self.last_id)
        else:
            user = (f"Task: {task}\n\nThe screen right now, before anything is "
                    f"touched (screen()):\n\n{first_text}")
            resp = self._create([{"role": "user", "content": user}], None)
        self.carry = []
        answered = {}                    # call_id -> output, for `resp`
        nudges, step = 0, 0
        while step < self.max_steps and self.ended is None:
            self._report(resp)
            answered = {}
            calls = [o for o in resp.output if o.type == "function_call"]
            if self._stopped():
                stopped = True
                break
            if not calls:
                nudges += 1
                if nudges > 2:
                    break
                resp = self._create([{"role": "user", "content":
                                      "Continue the task. End it by calling end_task."}],
                                    resp.id)
                continue
            outputs, extra = [], []
            for c in calls:
                if self._stopped():
                    stopped = True
                    o = {"type": "function_call_output", "call_id": c.call_id,
                         "output": STOPPED_NOTE}
                    outputs.append(o)
                    answered[c.call_id] = o
                    continue
                step += 1
                args = json.loads(c.arguments or "{}")
                waited = False
                if self.control is not None:
                    try:
                        waited = self.control.gate()
                    except Stopped:
                        stopped = True
                t1 = time.time()
                if stopped:
                    parts = [("text", STOPPED_NOTE)]
                elif waited and c.name in ACTIONS:
                    parts = [("text", PAUSED_NOTE)]
                else:
                    parts = await self.call(c.name, args)
                dt = time.time() - t1
                items, names = self._output(parts)
                text = "\n".join(v for k, v in parts if k == "text")
                self._log(kind="tool", step=step, name=c.name, args=args,
                          secs=round(dt, 2), reply=text, images=names)
                self._emit("tool", step=step, name=c.name, args=args,
                           secs=round(dt, 1), reply=text)
                print(f"[{step:02d}] {c.name}({json.dumps(args)[:120]}) "
                      f"{dt:.1f}s -> {text.splitlines()[0][:110] if text else ''}"
                      f"{'  +' + ','.join(names) if names else ''}")
                if self.images_in_output:
                    o = {"type": "function_call_output", "call_id": c.call_id,
                         "output": items}
                else:
                    o = {"type": "function_call_output", "call_id": c.call_id,
                         "output": text or "ok"}
                    imgs = [i for i in items if i["type"] == "input_image"]
                    if imgs:
                        extra.append({"role": "user", "content": [
                            {"type": "input_text",
                             "text": f"(the picture returned by {c.name})"}] + imgs})
                outputs.append(o)
                answered[c.call_id] = o
            if self.ended is not None or stopped or self._stopped():
                stopped = stopped or (self.ended is None and self._stopped())
                break
            while not self.inbox.empty():
                m = self.inbox.get_nowait()
                self._log(kind="person", text=m)
                extra.append({"role": "user", "content":
                              f"Message from the person while you work: {m}"})
            if step >= self.max_steps - 3:
                extra.append({"role": "user", "content":
                              "You are nearly out of steps: end_task now."})
            try:
                resp = self._create(outputs + extra, resp.id)
            except Exception as e:                       # noqa: BLE001
                if self.images_in_output and "image" in str(e).lower():
                    resp = self._fallback(outputs, extra, resp.id, e)
                else:
                    raise
        # Remember the conversation, and answer every call of its last
        # response: the API refuses to continue past an unanswered call.
        self.last_id = resp.id
        self.carry = [answered.get(o.call_id) or
                      {"type": "function_call_output", "call_id": o.call_id,
                       "output": "not done: the task ended first."}
                      for o in resp.output if o.type == "function_call"]
        _shot(os.path.join(self.dir, "after.png"))
        if stopped:
            ended = {"status": "stopped", "reason": "stopped by the person"}
        else:
            ended = self.ended or {"status": "unfinished",
                                   "reason": f"no end_task after {step} steps"}
        result = {"task": task, "model": self.model, "effort": self.effort,
                  "ended": ended, "steps": step,
                  "seconds": round(time.time() - t0, 1), "usage": self.usage,
                  "dir": self.dir}
        json.dump(result, open(os.path.join(self.dir, "result.json"), "w"), indent=1)
        self.steps.close()
        self._emit("end", result=result)
        print(f"\n--- {ended['status']}: {ended.get('reason', '')}"
              f"\n--- {step} steps, {result['seconds']}s, tokens in/out "
              f"{self.usage['input']}/{self.usage['output']} "
              f"(cached {self.usage['cached']}), {self.usage['calls']} model calls")
        return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--app", default="blender.exe",
                    help="the only executable whose windows may receive input")
    ap.add_argument("--model", default=os.environ.get("CUA_MODEL", "gpt-6-sol"))
    ap.add_argument("--effort", default=os.environ.get("CUA_EFFORT", "medium"))
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--pid", type=int, default=None,
                    help="only this process's windows may receive input")
    ap.add_argument("--launch", nargs="?", const="blender", choices=sorted(APPS),
                    help="start a fresh blender (factory settings) or chrome "
                         "(throwaway profile), maximise it, and allow input to that "
                         "process only. Bare --launch means blender.")
    ap.add_argument("--close", action="store_true",
                    help="with --launch: close it when the run ends")
    a = ap.parse_args(argv)
    state = session_state()
    if state != "active":
        raise SystemExit(f"this Windows session is {state}, not active: there is "
                         f"no screen to see or click. Reconnect RDP (and keep the "
                         f"window open) first.")
    proc = None
    if a.launch:
        proc, hwnd, front = launch(a.launch)
        a.pid, a.app = proc.pid, os.path.basename(APPS[a.launch]["exe"]).lower()
        print(f"--- launched {APPS[a.launch]['name']} pid {proc.pid}, "
              f"foreground={front}")
    try:
        return asyncio.run(Run(a.task, a.app, a.model, a.effort, a.max_steps,
                               pid=a.pid).go())
    finally:
        if proc is not None and a.close:
            close(proc)


def close(proc, wait=6):
    """Ask the windows to close, then end the process tree. Killing outright
    leaves Chrome offering to "restore pages" on its next start."""
    import subprocess
    for h in windows_of(proc.pid):
        _u32.PostMessageW(h, 0x0010, 0, 0)                 # WM_CLOSE
    try:
        proc.wait(wait)
    except Exception:                                    # noqa: BLE001
        pass
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                   capture_output=True)


if __name__ == "__main__":
    main()
