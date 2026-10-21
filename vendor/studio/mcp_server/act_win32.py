"""Actuation on Windows: real input events, at screen coordinates.

This replaces the cua-driver delegation the MCP was written against. That path
carried a large tax for one reason — the driver interprets a pixel click in the
coordinate space of the last window capture it took, so `hermes_unreal.native`
had to keep an "anchor" capture per window geometry (a ~5.3s UIA provider
connect) and resample every locally-grabbed frame into it. Owning input
ourselves deletes the anchor, the resample, and the re-anchor-on-resize, and
with them the only slow call left in the loop.

What survives from that work is the part that was hard to learn:

* **Never front a window to deliver a click.** The driver's foreground click
  verifies "did the TARGET window activate?" afterwards. Clicking Unreal's menu
  bar opens a popup HWND which takes the foreground, so verification fails and
  the driver's restore dismisses the menu it just opened — menus were literally
  unopenable through that path. Here a click is a real event at a screen
  position: the OS routes it by cursor location, exactly as it routes yours.
  Nothing verifies, nothing restores, popups stay.

* **A drag needs intermediate motion.** Slate starts a drag off cursor movement
  while a button is held; teleporting from press to release reads as a click on
  the origin.

* **A click wants a beat after the move.** Hover state has to register or the
  control under the cursor never lights up (and some Slate widgets act on the
  hovered item, not the clicked point).

Keyboard input goes to whatever holds focus — we do not steal it. Click the
field first, exactly as the tool docstring says.

Every event we inject is stamped `MAGIC` in its `dwExtraInfo`. The lesson
recorder reads that back to tell the agent's actions from the teacher's, which
is more reliable than `LLMHF_INJECTED` (that flag is set by any injector,
including whatever else is on the box).
"""
import sys
import time

MAGIC = 0x9E0C0DE                 # our fingerprint on every injected event

_EXTENDED = {                     # keys that need KEYEVENTF_EXTENDEDKEY
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C,
}

# One name per key. Alternate spellings live in ALIASES and are resolved on the
# way in, so inverting this has exactly one answer — the recorder canonicalises
# a vk back through that inverse, and teach-then-correct is a diff over the
# names. A key the agent issued as "escape" and the lesson wrote down as "esc"
# shows up in that diff as a difference which is not one.
VK = {"escape": 0x1B, "enter": 0x0D, "tab": 0x09,
      "space": 0x20, "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
      "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24,
      "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "capslock": 0x14,
      "printscreen": 0x2C, "pause": 0x13,
      **{f"f{i}": 0x6F + i for i in range(1, 25)}}

ALIASES = {"esc": "escape", "return": "enter", "pgup": "pageup",
           "pgdn": "pagedown", "del": "delete", "ins": "insert"}

MODS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "option": 0x12,
        "shift": 0x10, "cmd": 0x5B, "win": 0x5B, "super": 0x5B, "meta": 0x5B}

BUTTONS = ("left", "right", "middle")


# ------------------------------------------------------------------ platform

def normalize(x, y, vx, vy, vw, vh):
    """Screen pixels -> the 0..65535 grid an absolute SendInput move wants.

    Relative to the VIRTUAL desktop, whose origin is negative when a monitor
    sits above or left of the primary — the same offset `screen.capture`
    reports, arrived at from the other direction.
    """
    return (round((x - vx) * 65535 / max(1, vw - 1)),
            round((y - vy) * 65535 / max(1, vh - 1)))


def _structs():
    """ctypes definitions, built once and only on Windows.

    Lazy because `ctypes.wintypes` raises on import on macOS, and this module
    has to import there for the tests to reach the logic above it.
    """
    import ctypes
    from ctypes import wintypes

    ulong_ptr = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ulong_ptr)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", UNION)]

    return ctypes, INPUT, MOUSEINPUT, KEYBDINPUT, UNION


class Win32Input:
    """The OS calls, one thin method each, so the logic above can be tested.

    Everything here is a single API call. If a method in this class is doing
    something clever, it is in the wrong class.
    """

    # SendInput event types / flags
    _MOUSE, _KEY = 0, 1
    _MOVE_ABSOLUTE = 0x8000 | 0x0001 | 0x4000        # ABSOLUTE|MOVE|VIRTUALDESK
    _DOWN = {"left": 0x0002, "right": 0x0008, "middle": 0x0020}
    _UP = {"left": 0x0004, "right": 0x0010, "middle": 0x0040}
    _WHEEL, _HWHEEL = 0x0800, 0x1000
    _KEYUP, _UNICODE, _EXT = 0x0002, 0x0004, 0x0001

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Win32Input needs Windows; use --dry-run "
                               "elsewhere")
        self._c, self._INPUT, self._MI, self._KI, _ = _structs()
        self._u = self._c.windll.user32
        try:    # physical pixels even if this process is DPI-virtualized;
                # without it every coordinate is silently scaled on a HiDPI box
            self._u.SetProcessDpiAwarenessContext(self._c.c_void_p(-4))
        except Exception:                                        # noqa: BLE001
            try:
                self._c.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:                                    # noqa: BLE001
                pass

    def _send(self, *events):
        n = len(events)
        arr = (self._INPUT * n)(*events)
        self._u.SendInput(n, arr, self._c.sizeof(self._INPUT))

    def _mouse(self, flags, data=0, dx=0, dy=0):
        e = self._INPUT(type=self._MOUSE)
        e.u.mi = self._MI(dx, dy, data, flags, 0, MAGIC)
        return e

    def _key(self, vk, scan, flags):
        e = self._INPUT(type=self._KEY)
        e.u.ki = self._KI(vk, scan, flags, 0, MAGIC)
        return e

    # -- what the actuator calls -----------------------------------------

    def screen_size(self):
        return (self._u.GetSystemMetrics(0), self._u.GetSystemMetrics(1))

    def move(self, x, y):
        """Motion a low-level hook can SEE, landing on the exact pixel.

        `SetCursorPos` alone positions the cursor but generates no input event,
        so a `WH_MOUSE_LL` hook observes nothing. Measured on the box: 20
        SetCursorPos moves, 0 seen; 20 SendInput moves, 20 seen. The recorder
        therefore saw a drag as a bare press and release and wrote it down as a
        click on the target — the exact "teleporting drag reads as a click"
        failure `Actuator.drag` exists to avoid, reintroduced from the capture
        side where nothing was looking for it.

        So: an absolute SendInput move, which is real input, then SetCursorPos
        to correct — the wire format is 1/65535 of the virtual desktop and
        rounds to a neighbouring pixel.
        """
        x, y = int(x), int(y)
        m = self._u.GetSystemMetrics
        nx, ny = normalize(x, y, m(76), m(77), m(78), m(79))
        self._send(self._mouse(self._MOVE_ABSOLUTE, 0, nx, ny))
        self._u.SetCursorPos(x, y)

    def button(self, name, down):
        table = self._DOWN if down else self._UP
        self._send(self._mouse(table[name]))

    def wheel(self, notches, horizontal=False):
        self._send(self._mouse(self._HWHEEL if horizontal else self._WHEEL,
                               int(notches) * 120))

    def keydown(self, vk):
        self._send(self._key(vk, 0, self._EXT if vk in _EXTENDED else 0))

    def keyup(self, vk):
        f = self._KEYUP | (self._EXT if vk in _EXTENDED else 0)
        self._send(self._key(vk, 0, f))

    def char(self, ch):
        """One character by codepoint, bypassing the keyboard layout."""
        code = ord(ch)
        self._send(self._key(0, code, self._UNICODE),
                   self._key(0, code, self._UNICODE | self._KEYUP))

    def vk_for_char(self, ch):
        """(vk, needs_shift) for a character on the current layout, or None."""
        r = self._u.VkKeyScanW(ord(ch))
        if r == -1:
            return None
        return r & 0xFF, bool((r >> 8) & 1)

    def sleep(self, seconds):
        time.sleep(seconds)


# ------------------------------------------------------------------ actuator

class Actuator:
    """The action vocabulary the MCP server calls, in screen pixels.

    Timings are the ones the native path arrived at on this box: they are not
    padding, they are what Slate needs to see the input as input.
    """

    HOVER = 0.06          # after moving, before pressing — hover has to register
    REPEAT = 0.08         # between clicks of a double-click (< the OS threshold)
    DRAG_STEPS = 24       # intermediate moves; a teleport reads as a click
    DRAG_TIME = 0.45

    def __init__(self, io=None, type_mode="unicode"):
        self.io = io or Win32Input()
        self.type_mode = type_mode

    # -- mouse -----------------------------------------------------------

    def click(self, x, y, button="left", click_count=1, **_):
        self._check_button(button)
        self.io.move(x, y)
        self.io.sleep(self.HOVER)
        for i in range(max(1, int(click_count))):
            self.io.button(button, True)
            self.io.button(button, False)
            if i + 1 < click_count:
                self.io.sleep(self.REPEAT)
        return {"ok": True, "at": (x, y), "button": button,
                "clicks": click_count}

    def drag(self, from_xy, to_xy, button="left", **_):
        """Press, move in steps, release.

        The steps are the point. Slate begins a drag when the cursor moves with
        a button held, so the intermediate positions are what distinguish this
        from a click on the origin.
        """
        self._check_button(button)
        x1, y1 = from_xy
        x2, y2 = to_xy
        self.io.move(x1, y1)
        self.io.sleep(self.HOVER)
        self.io.button(button, True)
        self.io.sleep(self.HOVER)
        step = self.DRAG_TIME / self.DRAG_STEPS
        for i in range(1, self.DRAG_STEPS + 1):
            t = i / self.DRAG_STEPS
            self.io.move(round(x1 + (x2 - x1) * t), round(y1 + (y2 - y1) * t))
            self.io.sleep(step)
        self.io.button(button, False)
        return {"ok": True, "from": (x1, y1), "to": (x2, y2), "button": button}

    def scroll(self, direction, amount=3, x=None, y=None, **_):
        d = str(direction).lower()
        if d not in ("up", "down", "left", "right"):
            raise ValueError(f"scroll direction must be up/down/left/right, "
                             f"not {direction!r}")
        if x is not None and y is not None:
            self.io.move(x, y)
            self.io.sleep(self.HOVER)
        notches = int(amount) * (1 if d in ("up", "right") else -1)
        self.io.wheel(notches, horizontal=d in ("left", "right"))
        return {"ok": True, "direction": d, "amount": amount}

    # -- keyboard --------------------------------------------------------

    def key(self, keys, **_):
        """A combo like "ctrl+s", "escape", "ctrl+shift+n"."""
        mods, vk, shift = self.parse(keys)
        held = list(mods) + ([MODS["shift"]] if shift else [])
        for m in held:
            self.io.keydown(m)
        self.io.keydown(vk)
        self.io.keyup(vk)
        for m in reversed(held):
            self.io.keyup(m)
        return {"ok": True, "keys": keys}

    def type_text(self, text, **_):
        """Type a string into whatever holds focus.

        Unicode injection by default: it carries the codepoint directly and so
        does not care about the keyboard layout. `type_mode="vk"` falls back to
        synthesising layout keystrokes, for anything that ignores WM_CHAR.
        """
        for ch in str(text):
            if ch == "\n":
                self.key("enter")
            elif ch == "\t":
                self.key("tab")
            elif self.type_mode == "unicode":
                self.io.char(ch)
            else:
                got = self.io.vk_for_char(ch)
                if got is None:
                    self.io.char(ch)                 # unmappable: codepoint it
                    continue
                vk, shift = got
                if shift:
                    self.io.keydown(MODS["shift"])
                self.io.keydown(vk)
                self.io.keyup(vk)
                if shift:
                    self.io.keyup(MODS["shift"])
        return {"ok": True, "typed": len(str(text))}

    # -- parsing ---------------------------------------------------------

    def parse(self, keys):
        """"ctrl+shift+s" -> ([VK_CONTROL, VK_SHIFT], VK_S, needs_shift).

        Raises rather than half-pressing. A combo we cannot express must not
        arrive as a bare keystroke — `ctrl+s` delivered as `s` types a letter
        into the level.
        """
        parts = [p.strip().lower() for p in str(keys).split("+") if p.strip()]
        if not parts:
            raise ValueError("no key given")
        if len(parts) > 1 and parts[-1] == "":
            raise ValueError(f"{keys!r} ends in a separator")
        *mod_names, last = parts
        unknown = [m for m in mod_names if m not in MODS]
        if unknown:
            raise ValueError(f"{keys!r}: not a modifier: {', '.join(unknown)}. "
                             f"Known: {', '.join(sorted(set(MODS)))}")
        mods = [MODS[m] for m in mod_names]
        last = ALIASES.get(last, last)
        if last in VK:
            return mods, VK[last], False
        if len(last) == 1:
            got = self.io.vk_for_char(last)
            if got is None:
                raise ValueError(f"{keys!r}: {last!r} is not on this keyboard "
                                 f"layout")
            vk, shift = got
            return mods, vk, shift
        raise ValueError(f"{keys!r}: unknown key {last!r}. Known names: "
                         f"{', '.join(sorted(VK))}")

    @staticmethod
    def _check_button(button):
        if button not in BUTTONS:
            raise ValueError(f"button must be one of {BUTTONS}, "
                             f"not {button!r}")
