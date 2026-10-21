"""Deskhand's floating panel: tell the agent what to do, watch it, steer it.

    .venv\\Scripts\\pythonw scripts\\deskhand_ui.py         (or Deskhand.cmd)

A small always-on-top window. Pick what the agent works in (a fresh Blender,
Chrome, Calculator or File Explorer, or a window you click), type a task and
Send. Drag its header to move it, its corner to resize it; minimise folds it
into the header row, which keeps the status, the last step, Pause and Stop.

## Two mice, one cursor

Windows has one cursor, and the agent needs it: Blender aims its shortcuts at
whatever is under the cursor, so the cursor has to stay where the agent left
it. So while the agent drives:

- the real cursor is the agent's, marked with an orange "agent" tag;
- your mouse moves your own blue "you" pointer, drawn by the panel, and never
  the real cursor;
- your clicks and wheel reach the panel only: a click on the panel is relayed
  to it (the agent waits that instant), a click anywhere else is blocked;
- your keyboard types into the panel's box, without taking focus from the
  app; sending while the agent works steers it ("also make it red");
- Pause (or Esc twice) hands the real cursor back to you, at your pointer.

All of this is told apart by act_win32's MAGIC tag on the agent's own input
events; the panel tags the clicks it relays with REPLAY.

The agent never sees the panel, the frame or the pointers: they are excluded
from screen capture. --show-in-captures turns that off to record a demo; they
are then excluded only for the instant the agent takes each screenshot, which
changes nothing on the screen. If the agent clicks where the panel is, the
click passes through the panel to the app (the panel fades meanwhile).

For scripts and tests it also listens on 127.0.0.1:<port> (default 8777):
    GET  /state                      status, target, last log lines, router
    POST /task   {"text", "target"}  target: blender | chrome | calculator |
                                     explorer | pick (default: the selector's)
    POST /stop   /pause   /resume   /quit   /preview   /mini (fold or open)
"""
import argparse
import asyncio
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import font as tkfont
from tkinter import ttk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cua_agent as A  # noqa: E402

u32 = ctypes.WinDLL("user32", use_last_error=True)
# Physical pixels everywhere, before any window exists: screenshots, the
# agent's coordinates and this panel must agree on a display scaled to 125%,
# 150% or more. Tk then sizes its fonts from the real DPI.
try:
    u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))     # per-monitor v2
except Exception:                                              # noqa: BLE001
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:                                          # noqa: BLE001
        pass
u32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
u32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_ubyte,
                                           wintypes.DWORD]
u32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
u32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u32.GetAncestor.restype = wintypes.HWND
u32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
u32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.UINT]
u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.GetForegroundWindow.restype = wintypes.HWND
u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
u32.IsWindow.argtypes = [wintypes.HWND]
u32.ToUnicodeEx.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_char_p,
                            wintypes.LPWSTR, ctypes.c_int, wintypes.UINT, wintypes.HKL]
u32.GetKeyboardLayout.restype = wintypes.HKL

WDA_EXCLUDEFROMCAPTURE = 0x11
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_LAYERED = 0x20, 0x80000
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE, SWP_NOACTIVATE, SWP_SHOWWINDOW, SWP_ASYNC = 0x1, 0x10, 0x40, 0x4000
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
REPLAY = 0x9E0C0DF          # our tag on your clicks relayed to the panel
WM_MOUSEMOVE, WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 0x200, 0x20A, 0x20E
DOWN = {0x201: "left", 0x204: "right", 0x207: "middle"}
UP = {0x202: "left", 0x205: "right", 0x208: "middle"}

YELLOW, ORANGE, YOU = "#ffc400", "#ff7a00", "#4c8dff"
BG, HEAD, CARD, LINE = "#14161b", "#1b1e25", "#1e2129", "#2c303a"
FG, DIM, FAINT = "#e8e9ec", "#8b909a", "#5f6470"
BLUE, RED, GREEN = "#4c8dff", "#e5484d", "#3fa874"
KEY = "#010203"                       # transparent colour of the pointer windows
ALPHA = 0.99                          # the panel's opacity; below 1 makes it layered
LWA_ALPHA = 2
LOCK = os.environ.get("DESKHAND_LOCK")


def hwnd_of(w):
    w.update_idletasks()
    return u32.GetAncestor(w.winfo_id(), 2)                 # GA_ROOT


def hide_from_capture(hwnd):
    ok = u32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    got = wintypes.DWORD()
    u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
    return bool(ok) and got.value == WDA_EXCLUDEFROMCAPTURE


def set_alpha(hwnd, a):
    u32.SetLayeredWindowAttributes(hwnd, 0, int(a * 255), LWA_ALPHA)


def set_exstyle(hwnd, add=0, remove=0):
    ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, (ex | add) & ~remove)


def click_through(hwnd):
    set_exstyle(hwnd, WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW
                | WS_EX_NOACTIVATE)


def rect(hwnd):
    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def cursor():
    pt = wintypes.POINT()
    u32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# ------------------------------------------------------------- raw input

class Send:
    """SendInput with our own tag, for relaying your clicks to the panel."""

    def __init__(self):
        self._c, self._INPUT, self._MI, _, _ = A_win32()._structs()

    def _mouse(self, flags, data=0, dx=0, dy=0, tag=REPLAY):
        e = self._INPUT(type=0)
        e.u.mi = self._MI(dx, dy, data, flags, 0, tag)
        return e

    def _send(self, *ev):
        arr = (self._INPUT * len(ev))(*ev)
        u32.SendInput(len(ev), arr, ctypes.sizeof(self._INPUT))

    def move(self, x, y, tag=REPLAY):
        m = u32.GetSystemMetrics
        nx, ny = A_win32().normalize(x, y, m(76), m(77), m(78), m(79))
        self._send(self._mouse(0x8000 | 0x0001 | 0x4000, 0, nx, ny, tag))
        u32.SetCursorPos(int(x), int(y))

    def button(self, name, down):
        f = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}[name]
        self._send(self._mouse(f[0] if down else f[1]))

    def wheel(self, data, horizontal=False):
        self._send(self._mouse(0x1000 if horizontal else 0x800,
                               ctypes.c_int16(data >> 16).value & 0xFFFFFFFF))


def A_win32():
    from mcp_server import act_win32
    return act_win32


# ------------------------------------------------------------------ overlays

class Overlays:
    """The yellow frame and the two pointers. All let clicks through and are
    hidden from capture; all are moved with Win32 calls, so other threads can
    drive them without touching Tk."""

    T = 6                                  # frame thickness, px

    def __init__(self, root, excluded):
        self.root = root
        # Around the whole virtual desktop, every monitor: the agent's
        # screenshots cover all of them too.
        vx, vy, vw, vh = (u32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        T = self.T
        self.frame = []
        for g in (f"{vw}x{T}+{vx}+{vy}", f"{vw}x{T}+{vx}+{vy + vh - T}",
                  f"{T}x{vh}+{vx}+{vy}", f"{T}x{vh}+{vx + vw - T}+{vy}"):
            w = tk.Toplevel(root, bg=YELLOW)
            w.overrideredirect(True)
            w.geometry(g)
            w.attributes("-topmost", True)
            w.attributes("-alpha", 0.9)
            self.frame.append(w)
        self.ptr, self.canvas, self.arrow = self._pointer(ORANGE, "agent", "black")
        self.you, _, _ = self._pointer(YOU, "you", "white")
        self.frame_hwnds = [hwnd_of(w) for w in self.frame]
        self.ptr_hwnd, self.you_hwnd = hwnd_of(self.ptr), hwnd_of(self.you)
        self.hwnds = self.frame_hwnds + [self.ptr_hwnd, self.you_hwnd]
        self.excluded = True
        for h in self.hwnds:
            click_through(h)
            if excluded:
                self.excluded &= hide_from_capture(h)
        self.show(False)

    TIP = 24                               # the arrow's tip, inside its window

    def _pointer(self, colour, label, ink):
        """A cursor-shaped arrow with a white edge and a soft shadow, and a
        rounded name tag; the window is larger than both so a click ripple
        fits around the tip."""
        w = tk.Toplevel(self.root, bg=KEY)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.attributes("-transparentcolor", KEY)
        w.geometry("150x96+-400+-400")
        c = tk.Canvas(w, width=150, height=96, bg=KEY, highlightthickness=0)
        c.pack()
        t = self.TIP
        shape = [(0, 0), (0, 25), (6.5, 19), (11, 29), (15.5, 27), (11, 17.5), (19, 17.5)]
        c.create_polygon(*[v + d for x, y in shape for v, d in ((x, t + 2), (y, t + 3))],
                         fill="#2a2a2a", outline="")
        arrow = c.create_polygon(*[v + t for x, y in shape for v in (x, y)],
                                 fill=colour, outline="white", width=2, joinstyle="round")
        x0, y0 = t + 15, t + 22
        x1, y1 = x0 + 7 * len(label) + 18, y0 + 17
        r = 8
        pill = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
                x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
        c.create_polygon(*pill, smooth=True, fill=colour, outline="white", width=1)
        c.create_oval(x0 + 6, y0 + 6, x0 + 11, y0 + 11, fill=ink, outline="")
        c.create_text((x0 + 13 + x1) // 2, (y0 + y1) // 2, text=label, fill=ink,
                      font=("Segoe UI", 8, "bold"))
        return w, c, arrow

    def show(self, on, you=None):
        for h in self.frame_hwnds + [self.ptr_hwnd]:
            u32.ShowWindowAsync(h, SW_SHOWNOACTIVATE if on else SW_HIDE)
        u32.ShowWindowAsync(self.you_hwnd, SW_SHOWNOACTIVATE if (on if you is None
                                                                 else you) else SW_HIDE)

    def _place(self, hwnd, x, y):
        u32.SetWindowPos(hwnd, HWND_TOPMOST, int(x) - self.TIP, int(y) - self.TIP, 0, 0,
                         SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_ASYNC)

    def point(self, x, y):
        """The agent's tag on the real cursor. Safe from any thread."""
        self._place(self.ptr_hwnd, x, y)

    def you_to(self, x, y):
        """Your own pointer. Safe from any thread."""
        self._place(self.you_hwnd, x, y)

    def flash(self):
        """A click: a ring spreads from the agent's tip and fades. Tk thread."""
        c, t = self.canvas, self.TIP
        ring = c.create_oval(t - 4, t - 4, t + 4, t + 4, outline=YELLOW, width=3)
        c.tag_lower(ring)

        def grow(i=0):
            if i >= 7:
                c.delete(ring)
                return
            r = 5 + i * 3
            c.coords(ring, t - r, t - r, t + r, t + r)
            c.itemconfigure(ring, width=max(1, 3 - i // 2),
                            outline=(YELLOW, YELLOW, ORANGE, ORANGE, "#c45a00",
                                     "#8a3f00", "#5a2a00")[i])
            self.root.after(45, grow, i + 1)
        grow()


# ----------------------------------------------------------- input router

class Router(threading.Thread):
    """Low-level mouse and keyboard hooks that keep your input and the
    agent's apart while it drives (see the module docstring). When it is not
    driving, everything passes untouched. Nothing you type is recorded: keys
    are only handed to the panel's box."""

    class MS(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("extra", ctypes.c_size_t)]

    class KB(ctypes.Structure):
        _fields_ = [("vk", wintypes.DWORD), ("scan", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("extra", ctypes.c_size_t)]

    MODS = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl",
            0xA3: "ctrl", 0x12: "alt", 0xA4: "alt", 0xA5: "alt"}

    def __init__(self, panel):
        super().__init__(daemon=True)
        self.p = panel
        self.driving = False
        self.user = cursor()
        self.held = None                  # a button you hold on the panel
        self.mods = set()
        self.last_esc = 0.0
        self.blocked = 0
        # Over RDP the mouse arrives as absolute positions; a local mouse as
        # moves relative to wherever the real cursor is.
        self.absolute = os.environ.get("SESSIONNAME", "").upper().startswith("RDP")

    def drive(self, on):
        if on and not self.driving:
            self.user = cursor()
            self.p.over.you_to(*self.user)
        self.driving = on

    def _inside_panel(self, x, y):
        if not u32.IsWindowVisible(self.p.hwnd):
            return False
        l, t, r, b = rect(self.p.hwnd)
        return l <= x < r and t <= y < b

    def _mouse(self, n, w, l):
        if n != 0:
            return None
        m = ctypes.cast(l, ctypes.POINTER(self.MS)).contents
        if m.extra in (A.MAGIC, REPLAY):
            return None
        if not self.driving:
            self.user = (m.x, m.y)
            return None
        if w == WM_MOUSEMOVE:
            if self.absolute:
                self.user = (m.x, m.y)
            else:
                cx, cy = cursor()
                vx, vy, vw, vh = (u32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
                self.user = (min(max(self.user[0] + m.x - cx, vx), vx + vw - 1),
                             min(max(self.user[1] + m.y - cy, vy), vy + vh - 1))
            self.p.over.you_to(*self.user)
            if self.held:
                self.p.relay.put(("move", None, self.user, 0))
            return 1
        x, y = self.user
        if w in DOWN:
            if self._inside_panel(x, y) and not self.held:
                self.held = DOWN[w]
                self.p.relay.put(("down", DOWN[w], self.user, 0))
            else:
                self.blocked += 1
                self.p.ui(self.p.hint)
            return 1
        if w in UP:
            if self.held == UP[w]:
                self.held = None
                self.p.relay.put(("up", UP[w], self.user, 0))
            return 1
        if w in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
            if self._inside_panel(x, y):
                self.p.relay.put(("wheel", w == WM_MOUSEHWHEEL, self.user, m.mouseData))
            return 1
        return 1

    def _keys(self, n, w, l):
        if n != 0:
            return None
        k = ctypes.cast(l, ctypes.POINTER(self.KB)).contents
        if k.extra in (A.MAGIC, REPLAY) or not self.driving:
            return None
        down = w in (0x100, 0x104)
        if k.vk in self.MODS:
            (self.mods.add if down else self.mods.discard)(self.MODS[k.vk])
            return 1
        if down:
            if k.vk == 0x1B:                                       # Esc, twice
                now = time.time()
                if now - self.last_esc < 0.7:
                    self.p.ui(self.p.pause)
                self.last_esc = now
            elif k.vk == 0x13:                                     # Pause key
                self.p.ui(self.p.pause)
            else:
                self.p.ui(self.p.typed, k.vk, k.scan, frozenset(self.mods))
        return 1

    def run(self):
        PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM,
                                  wintypes.LPARAM)
        u32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                       wintypes.WPARAM, wintypes.LPARAM]
        u32.CallNextHookEx.restype = ctypes.c_ssize_t
        u32.SetWindowsHookExW.argtypes = [ctypes.c_int, PROC, wintypes.HINSTANCE,
                                          wintypes.DWORD]
        u32.SetWindowsHookExW.restype = wintypes.HANDLE

        def wrap(fn):
            def hook(n, w, l):
                try:
                    if fn(n, w, l):
                        return 1                          # swallowed
                except Exception:                         # noqa: BLE001
                    pass
                return u32.CallNextHookEx(None, n, w, l)
            return PROC(hook)

        self._m, self._k = wrap(self._mouse), wrap(self._keys)   # keep alive
        self.ok = bool(u32.SetWindowsHookExW(14, self._m, None, 0)) and \
            bool(u32.SetWindowsHookExW(13, self._k, None, 0))
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))


class Relay(threading.Thread):
    """Delivers your clicks on the panel while the agent drives: the real
    cursor visits your pointer, does what you did, and goes back to where the
    agent left it. It holds the input lock meanwhile, so the agent's next
    action waits that instant instead of fighting you for the cursor."""

    def __init__(self, panel):
        super().__init__(daemon=True)
        self.p = panel
        self.q = queue.Queue()
        self.send = None
        self.saved = None
        self.holding = False

    def put(self, item):
        self.q.put(item)

    def _visit(self, pos):
        if not self.holding:
            self.p.input_lock.acquire()
            self.saved = cursor()
            self.holding = True
        self.send.move(*pos)

    def _leave(self):
        if self.holding:
            # Back to where the agent left the cursor, unless your click just
            # paused or ended its turn: then the cursor stays with you.
            x, y = self.saved if self.p.router.driving else self.p.router.user
            self.send.move(x, y, tag=A.MAGIC)
            self.holding = False
            self.p.input_lock.release()

    def run(self):
        self.send = Send()
        while True:
            try:
                kind, what, pos, data = self.q.get(timeout=10)
            except queue.Empty:
                self._leave()                  # a lost button-up must not hold forever
                continue
            try:
                if kind == "down":
                    self._visit(pos)
                    time.sleep(0.02)
                    self.send.button(what, True)
                elif kind == "move" and self.holding:
                    self.send.move(*pos)
                elif kind == "up":
                    if self.holding:
                        self.send.move(*pos)
                        self.send.button(what, False)
                        time.sleep(0.05)
                    self._leave()
                elif kind == "wheel":
                    self._visit(pos)
                    time.sleep(0.02)
                    self.send.wheel(data, horizontal=what)
                    time.sleep(0.05)
                    self._leave()
            except Exception:                                    # noqa: BLE001
                self._leave()


# ------------------------------------------------------------------- panel

# ------------------------------------------------------------------ widgets

def _mix(c, d, f):
    """Colour c moved a fraction f of the way to colour d."""
    a = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(d[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * f) for x, y in zip(a, b))


def _round_rect(cv, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


class Chip(tk.Canvas):
    """A rounded button or label, drawn: Tk's own buttons cannot be rounded and
    look dated. With a command it lights up under the pointer and acts on
    release, like a button. bg=None draws nothing until the pointer is on it
    (for the header's icons)."""

    def __init__(self, parent, text, command=None, bg=None, fg=FG, font=None,
                 padx=12, pady=4, radius=None, hover=None, hover_fg=None, k=1.0,
                 fit=""):
        self.base = parent["bg"]
        super().__init__(parent, highlightthickness=0, bd=0, bg=self.base,
                         cursor="hand2" if command else "")
        self.font = tkfont.Font(font=font or ("Segoe UI", 9, "bold"))
        self.command, self.fill, self.ink = command, bg, fg
        self.hover_fill, self.hover_ink = hover, hover_fg
        self.padx, self.pady, self.radius = int(padx * k), int(pady * k), radius
        self.text, self.enabled, self._in = text, True, False
        self.fit = fit                      # also wide enough for this text: no jumping
        if command:
            self.bind("<Enter>", lambda e: self._hover(True))
            self.bind("<Leave>", lambda e: self._hover(False))
            self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def set(self, text=None, bg=None, fg=None, enabled=None):
        if text is not None:
            self.text = text
        if bg is not None:
            self.fill = bg
        if fg is not None:
            self.ink = fg
        if enabled is not None:
            self.enabled = enabled
        self._draw()

    def _hover(self, on):
        self._in = on
        self._draw()

    def _release(self, e):
        if self.enabled and 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height():
            self.command()

    def _draw(self):
        f = self.font
        w = max(f.measure(self.text), f.measure(self.fit)) + 2 * self.padx
        h = f.metrics("linespace") + 2 * self.pady
        self.configure(width=w, height=h)
        self.delete("all")
        lit = self._in and self.enabled and self.command
        fill, ink = self.fill, self.ink
        if lit:
            fill = self.hover_fill or _mix(fill or self.base, "#ffffff", 0.12)
            ink = self.hover_ink or ink
        if not self.enabled:
            ink = _mix(ink, fill or self.base, 0.6)
        if fill:
            r = min(self.radius if self.radius is not None else h // 2, h // 2)
            _round_rect(self, 0, 0, w - 1, h - 1, r, fill=fill, outline=fill)
        self.create_text(w // 2, h // 2, text=self.text, fill=ink, font=f)


def _icon_font():
    fams = set(tkfont.families())
    for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if f in fams:
            return f
    return None


def _round_corners(hwnd):
    """Windows 11: rounded corners and a thin border on the frameless panel.
    Earlier Windows ignores this."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        v = ctypes.c_int(2)                                   # DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(v), 4)
        r, g, b = (int(LINE[i:i + 2], 16) for i in (1, 3, 5))
        c = ctypes.c_uint(r | g << 8 | b << 16)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(c), 4)
    except Exception:                                          # noqa: BLE001
        pass


TARGETS = {"New Blender": "blender", "New Chrome": "chrome",
           "New Calculator": "calculator", "New File Explorer": "explorer",
           "A window I pick": "pick"}


def _alive(t):
    if not t:
        return False
    if t.get("window"):
        return bool(u32.IsWindow(t["hwnd"])) and bool(u32.IsWindowVisible(t["hwnd"]))
    return (t["proc"] is None or t["proc"].poll() is None) and bool(A.windows_of(t["pid"]))


def _main_window(t):
    if t.get("window"):
        return t["hwnd"]
    wins = A.windows_of(t["pid"])
    return wins[0] if wins else None


def _has_focus(t):
    fg = u32.GetForegroundWindow()
    root = u32.GetAncestor(fg, 2) or fg if fg else None
    if t.get("window"):
        return A.belongs(root, t["hwnd"])
    return A._owner(root)[0] in A.family(t["pid"])


class Panel:
    def __init__(self, args):
        self.args = args
        self.q = queue.Queue()
        self.input_lock = threading.Lock()
        self.root = r = tk.Tk()
        r.title("Deskhand")
        r.configure(bg=BG)
        # Frameless, with our own header: Windows' title bar for a small
        # always-on-top window has no minimise button and wastes a row.
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        # Layered (alpha below 1), so the agent's clicks can pass through the
        # panel instead of the panel having to vanish for them.
        r.attributes("-alpha", ALPHA)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        try:
            k = u32.GetDpiForSystem() / 96.0
        except Exception:                                      # noqa: BLE001
            k = 1.0
        self.k = k
        W, H = self.s(380), self.s(600)
        r.geometry(f"{W}x{H}+{sw - W - self.s(24)}+{max(0, sh - H - self.s(64))}")
        r.protocol("WM_DELETE_WINDOW", self.quit)
        self.mini = False
        self._full_size = (W, H)
        self.bubbles = []
        self.step_n = 0
        self._build()
        self.hwnd = hwnd_of(r)
        _round_corners(self.hwnd)
        self.excluded = (not args.show_in_captures) and hide_from_capture(self.hwnd)
        self.over = Overlays(r, not args.show_in_captures)
        self.relay = Relay(self)
        self.relay.start()
        self.router = Router(self)
        self.router.start()

        self.control = A.Control()
        self.run = None                   # A.Run for the current target
        self.target = None                # {"kind", "pid", "exe", "proc", "title"}
        self.worker = None
        self.status = "idle"
        self.lines = []
        self.last_dir = None
        self._took_lock = False
        self._through = False
        self._hint_at = 0.0
        cap = ("hidden from screen capture" if self.excluded and self.over.excluded
               else "VISIBLE to screen capture" if args.show_in_captures
               else "could not hide from capture")
        self.log(f"Ready. The panel and pointers are {cap}.", "dim")
        self._set_status("idle")
        r.after(40, self._pump)
        r.after(500, self._tick)
        if args.port:
            threading.Thread(target=self._serve, daemon=True).start()

    def s(self, px):
        """Pixels at this display's scaling."""
        return int(px * self.k)

    # -- layout ------------------------------------------------------------

    def _build(self):
        r, s, P = self.root, self.s, self.s(14)
        icon = _icon_font()
        glyph = (lambda g, alt: g if icon else alt)
        self.glyphs = {"min": glyph("\ue921", "–"), "full": glyph("\ue922", "□"),
                       "close": glyph("\ue8bb", "✕"), "pause": glyph("\ue769", "❚❚"),
                       "play": glyph("\ue768", "▶")}
        ifont = (icon, 8) if icon else ("Segoe UI", 9, "bold")
        st = ttk.Style(r)
        st.theme_use("clam")
        st.layout("Deskhand.Vertical.TScrollbar",
                  [("Vertical.Scrollbar.trough",
                    {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb",
                                                   {"expand": "1", "sticky": "nswe"})]})])
        st.configure("Deskhand.Vertical.TScrollbar", troughcolor=CARD, background=LINE,
                     bordercolor=CARD, lightcolor=LINE, darkcolor=LINE, gripcount=0,
                     arrowsize=s(7), relief="flat")
        st.map("Deskhand.Vertical.TScrollbar",
               background=[("active", _mix(LINE, "#ffffff", 0.15))])

        # Header: drag it to move the panel. Minimise folds the panel into this
        # one row, which keeps the status, the last step, Pause and Stop.
        head = self.head = tk.Frame(r, bg=HEAD)
        head.pack(fill="x")
        self.dot = tk.Canvas(head, width=s(9), height=s(9), bg=HEAD, highlightthickness=0)
        self.dot_id = self.dot.create_oval(0, 0, s(9) - 1, s(9) - 1, fill=FAINT, outline="")
        self.dot.pack(side="left", padx=(P, s(8)), pady=s(13))
        self.title = tk.Label(head, text="Deskhand", bg=HEAD, fg=FG,
                              font=("Segoe UI Semibold", 11))
        self.title.pack(side="left")
        self.pill = Chip(head, "idle", bg=CARD, fg=DIM, font=("Segoe UI", 8, "bold"),
                         padx=8, pady=1, k=self.k)
        self.pill.pack(side="left", padx=(s(10), s(6)))
        hb = dict(bg=None, fg=DIM, font=ifont, padx=11, pady=7, radius=s(6), k=self.k)
        self.close_btn = Chip(head, self.glyphs["close"], self.quit, hover=RED,
                              hover_fg="white", **hb)
        self.close_btn.pack(side="right", padx=(0, s(6)))
        self.min_btn = Chip(head, self.glyphs["min"], self.toggle_mini, hover_fg=FG, **hb)
        self.min_btn.pack(side="right")
        # Shown only when minimised.
        self.mini_step = tk.Label(head, text="", bg=HEAD, fg=DIM, anchor="w", width=1,
                                  font=("Segoe UI", 8))
        self.mini_pause = Chip(head, self.glyphs["pause"], self.toggle_pause,
                               hover_fg=FG, **hb)
        self.mini_stop = Chip(head, "■", self.stop,           # solid: not the restore square
                              **dict(hb, fg="#ff8f95", font=("Segoe UI Symbol", 9)))
        self.mini_step.bind("<Configure>", self._fit_mini)
        for w in (head, self.dot, self.title, self.pill, self.mini_step):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        body = self.body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True)
        tk.Frame(body, bg=LINE, height=1).pack(fill="x")

        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", padx=P, pady=(s(12), 0))
        tk.Label(row, text="Works in", bg=BG, fg=DIM,
                 font=("Segoe UI", 9)).pack(side="left")
        self.tvar = tk.StringVar(value="New Blender")
        self.tbtn = tk.Menubutton(row, text="New Blender  ▾", bg=CARD, fg=FG,
                                  activebackground=_mix(CARD, "#ffffff", 0.08),
                                  activeforeground=FG, relief="flat", bd=0,
                                  highlightthickness=0, padx=s(10), pady=s(4),
                                  font=("Segoe UI", 9), cursor="hand2")
        menu = tk.Menu(self.tbtn, tearoff=0, bg=CARD, fg=FG, bd=0,
                       activebackground=BLUE, activeforeground="white",
                       selectcolor=FG, font=("Segoe UI", 9))
        for name in TARGETS:
            menu.add_radiobutton(label=name, variable=self.tvar, value=name,
                                 command=self._target_changed)
        self.tbtn["menu"] = menu
        self.tvar.trace_add("write",
                            lambda *_: self.tbtn.configure(text=f"{self.tvar.get()}  ▾"))
        self.tbtn.pack(side="left", padx=(s(8), s(6)))
        small = dict(bg=CARD, fg=FG, font=("Segoe UI", 9), padx=10, pady=4,
                     radius=s(6), k=self.k)
        self.pick_btn = Chip(row, "Pick", self.pick, **small)
        self.pick_btn.pack(side="left")
        self.trace_btn = Chip(row, "Trace", self.open_trace, **small)
        self.trace_btn.pack(side="right")
        self.tinfo = tk.Label(body, text="A fresh app starts when you send a task.",
                              bg=BG, fg=FAINT, anchor="w", font=("Segoe UI", 8))
        self.tinfo.pack(fill="x", padx=P, pady=(s(6), 0))

        card = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=LINE,
                        highlightcolor=LINE)
        card.pack(fill="both", expand=True, padx=P, pady=(s(8), s(4)))
        self.text = tk.Text(card, bg=CARD, fg=FG, wrap="word", relief="flat", bd=0,
                            highlightthickness=0, font=("Segoe UI", 9), padx=s(10),
                            pady=s(8), height=10, state="disabled", cursor="arrow",
                            selectbackground="#2f4a7a", inactiveselectbackground="#2f4a7a")
        sb = ttk.Scrollbar(card, orient="vertical", command=self.text.yview,
                           style="Deskhand.Vertical.TScrollbar")
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, s(2)), pady=s(2))
        self.text.pack(side="left", fill="both", expand=True)
        tags = {
            "num": dict(foreground=FAINT, font=("Consolas", 8), lmargin2=s(22),
                        spacing1=s(1)),
            # A wrapped step continues under its text, not under its number.
            "act": dict(foreground="#c9ccd3", lmargin2=s(22)),
            "tail": dict(foreground=DIM, font=("Segoe UI", 8), lmargin2=s(22)),
            "step": dict(foreground=DIM),
            "think_h": dict(foreground="#b6abff", font=("Segoe UI", 9, "bold"),
                            spacing1=s(8)),
            "think": dict(foreground="#a49bd8", font=("Segoe UI", 9, "italic"),
                          spacing1=s(2), spacing3=s(6)),
            "say": dict(foreground=FG, spacing1=s(6), spacing3=s(4)),
            "say_b": dict(font=("Segoe UI", 9, "bold")),
            "ok": dict(foreground="#5fd39b", font=("Segoe UI", 9, "bold"), spacing1=s(10)),
            "bad": dict(foreground="#ff7b80", font=("Segoe UI", 9, "bold"), spacing1=s(10)),
            "warn": dict(foreground=YELLOW, spacing1=s(4)),
            "dim": dict(foreground=DIM, font=("Segoe UI", 8), spacing1=s(2)),
            "bubble": dict(justify="right", spacing1=s(10), spacing3=s(6)),
        }
        for tag, kw in tags.items():
            self.text.tag_configure(tag, **kw)
        self.text.bind("<Configure>", self._rewrap)

        self.prog = tk.Label(body, text="", bg=BG, fg=FAINT, anchor="w",
                             font=("Segoe UI", 8))
        self.prog.pack(fill="x", padx=P)

        ebox = self.ebox = tk.Frame(body, bg=CARD, highlightthickness=1,
                                    highlightbackground=LINE, highlightcolor=LINE)
        ebox.pack(fill="x", padx=P, pady=(s(6), 0))
        self.entry = tk.Text(ebox, height=3, bg=CARD, fg=FG, insertbackground=FG,
                             relief="flat", bd=0, highlightthickness=0, wrap="word",
                             font=("Segoe UI", 10), padx=s(10), pady=s(8),
                             selectbackground="#2f4a7a")
        self.entry.pack(fill="x")
        self.entry.bind("<Control-Return>", lambda e: (self.send(), "break")[1])
        self.entry.bind("<<Modified>>", self._entry_changed)
        self.entry.bind("<FocusIn>", lambda e: ebox.configure(highlightbackground=BLUE))
        self.entry.bind("<FocusOut>", lambda e: ebox.configure(highlightbackground=LINE))
        self.placeholder = tk.Label(ebox, text="Tell the agent what to do…", bg=CARD,
                                    fg=FAINT, font=("Segoe UI", 10), cursor="xterm")
        self.placeholder.place(x=s(11), y=s(8))
        self.placeholder.bind("<Button-1>", lambda e: self.entry.focus_set())

        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", padx=P, pady=(s(10), s(2)))
        big = dict(font=("Segoe UI", 9, "bold"), padx=16, pady=5, radius=s(7), k=self.k)
        self.send_btn = Chip(bar, "Send", self.send, bg=BLUE, fg="white", **big)
        self.send_btn.pack(side="left")
        self.pause_btn = Chip(bar, "Pause", self.toggle_pause, bg="#2a2f3a", fg=FG,
                              fit="Resume", **big)
        self.pause_btn.pack(side="left", padx=s(8))
        self.stop_btn = Chip(bar, "Stop", self.stop, bg="#3b2026", fg="#ff8f95", **big)
        self.stop_btn.pack(side="left")
        tk.Label(bar, text="Ctrl+Enter", bg=BG, fg=FAINT,
                 font=("Segoe UI", 8)).pack(side="right")
        self.help = tk.Label(body, text="", bg=BG, fg=FAINT, font=("Segoe UI", 8),
                             justify="left", anchor="w")
        self.help.pack(fill="x", padx=P, pady=(s(6), s(12)))
        body.bind("<Configure>", lambda e: self.help.configure(wraplength=e.width - 2 * P))

        # Resize from the bottom-right corner.
        g = self.grip = tk.Canvas(body, width=s(12), height=s(12), bg=BG,
                                  highlightthickness=0, cursor="size_nw_se")
        for i in (3, 7, 11):
            g.create_line(s(i), s(12), s(12), s(i), fill=FAINT)
        g.place(relx=1.0, rely=1.0, x=-s(3), y=-s(3), anchor="se")
        g.bind("<ButtonPress-1>", self._grip_start)
        g.bind("<B1-Motion>", self._grip_move)

    # -- moving, resizing, minimising --------------------------------------------

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        dx, dy = self._drag
        self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def _grip_start(self, e):
        self._grip = (e.x_root, e.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _grip_move(self, e):
        x0, y0, w, h = self._grip
        w = max(self.s(320), w + e.x_root - x0)
        h = max(self.s(380), h + e.y_root - y0)
        self.root.geometry(f"{w}x{h}")

    def toggle_mini(self):
        """Fold the panel into its header row, or open it again. The fold keeps
        the bottom edge where it was, as the panel sits at the bottom of the
        screen."""
        r = self.root
        r.update_idletasks()
        x, y, w, h = r.winfo_x(), r.winfo_y(), r.winfo_width(), r.winfo_height()
        if not self.mini:
            self._full_size = (w, h)
            self.body.pack_forget()
            self.title.pack_forget()                 # the dot and status say enough
            self.mini_stop.pack(side="right", after=self.min_btn)
            self.mini_pause.pack(side="right", after=self.mini_stop)
            # Packed last, so the buttons keep their room and the text gets
            # whatever is left.
            self.mini_step.pack(side="left", fill="x", expand=True, padx=(0, self.s(4)))
            self.min_btn.set(text=self.glyphs["full"])
            r.update_idletasks()
            hm = self.head.winfo_reqheight()
            r.geometry(f"{w}x{hm}+{x}+{y + h - hm}")
        else:
            for wdg in (self.mini_step, self.mini_pause, self.mini_stop):
                wdg.pack_forget()
            self.title.pack(side="left", after=self.dot)
            self.body.pack(fill="both", expand=True)
            self.min_btn.set(text=self.glyphs["min"])
            W, H = self._full_size
            r.geometry(f"{W}x{H}+{x}+{max(0, y + h - H)}")
        self.mini = not self.mini
        return "mini" if self.mini else "full"

    def _mini_text(self, text):
        self._mini_full = text
        self._fit_mini()

    def _fit_mini(self, _=None):
        """The folded row's step text, shortened with … to the room it has."""
        lab, t = self.mini_step, getattr(self, "_mini_full", "")
        f = tkfont.Font(font=lab["font"])
        room = lab.winfo_width() - self.s(4)
        if room > 10 and f.measure(t) > room:
            while t and f.measure(t + "…") > room:
                t = t[:-1]
            t = t.rstrip() + "…"
        lab.configure(text=t)

    # -- thread-safe plumbing -------------------------------------------------

    def ui(self, fn, *a):
        self.q.put((fn, a))

    def _pump(self):
        try:
            while True:
                fn, a = self.q.get_nowait()
                fn(*a)
        except queue.Empty:
            pass
        self.root.after(30, self._pump)

    def _tick(self):
        """The live line under the log: step, time and cost so far."""
        try:
            run = self.run
            if self.status in ("working", "paused") and run is not None \
                    and getattr(run, "t0", None):
                secs = int(time.time() - run.t0)
                c = run.cost_so_far()
                self.prog.configure(text=f"step {self.step_n} · {secs // 60}:{secs % 60:02d}"
                                         + (f" · ${c:.3f}" if c is not None else ""))
        except Exception:                                    # noqa: BLE001
            pass
        self.root.after(500, self._tick)

    # -- the log ------------------------------------------------------------------

    def log(self, msg, tag="say"):
        self.lines.append((tag, msg))
        self.lines = self.lines[-300:]
        t = self.text
        follow = t.yview()[1] >= 0.999          # don't yank you back down if you scrolled up
        t.configure(state="normal")
        if tag == "you":
            self._bubble(msg)
        elif tag == "step" and (m := re.match(r"\s*(\d+)\. (.*?)(?: — (.*))?$", msg, re.S)):
            t.insert("end", f"{m.group(1):>2}  ", "num", m.group(2), "act")
            if m.group(3):
                t.insert("end", "  " + m.group(3), "tail")
            t.insert("end", "\n", "act")
        elif tag == "think":
            body = msg[2:] if msg.startswith("· ") else msg
            m = re.match(r"\*\*(.+?)\*\*\s*(.*)", body.strip(), re.S)
            if m:
                t.insert("end", m.group(1).strip() + "\n", "think_h")
                if m.group(2).strip():
                    t.insert("end", m.group(2).strip() + "\n", "think")
            else:
                t.insert("end", body.strip() + "\n", "think")
        elif tag == "say":
            # The model writes **bold**: show it bold, without the stars.
            for i, part in enumerate(msg.rstrip().split("**")):
                if part:
                    t.insert("end", part, ("say", "say_b") if i % 2 else "say")
            t.insert("end", "\n", "say")
        else:
            t.insert("end", msg.rstrip() + "\n", tag)
        if follow:
            t.see("end")
        t.configure(state="disabled")

    def _bubble(self, msg):
        """Your messages, as right-aligned bubbles."""
        t, s = self.text, self.s
        steer = "while it works" in msg
        text = msg.split(": ", 1)[1] if msg.startswith("You") and ": " in msg else msg
        f = tk.Frame(t, bg=CARD)
        if steer:
            tk.Label(f, text="sent while it works", bg=CARD, fg=FAINT,
                     font=("Segoe UI", 7)).pack(anchor="e")
        lab = tk.Label(f, text=text, bg="#23395f", fg="#e3ecff", font=("Segoe UI", 9),
                       justify="left", padx=s(10), pady=s(6),
                       wraplength=max(s(140), int(t.winfo_width() * 0.75)))
        lab.pack(anchor="e")
        for w in (f, lab):
            w.bind("<MouseWheel>",
                   lambda e: t.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        self.bubbles.append(lab)
        at = t.index("end-1c")
        t.window_create(at, window=f)
        t.insert("end", "\n")
        t.tag_add("bubble", at, "end-1c")

    def _rewrap(self, e):
        for lab in self.bubbles:
            try:
                lab.configure(wraplength=max(self.s(140), int(e.width * 0.75)))
            except tk.TclError:
                pass

    def _entry_changed(self, _=None):
        if self.entry.get("1.0", "end-1c"):
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=self.s(11), y=self.s(8))
        self.entry.edit_modified(False)

    def _set_status(self, s):
        self.status = s
        colours = {"idle": (CARD, DIM), "starting": (CARD, YELLOW),
                   "working": (YELLOW, "black"), "paused": ("#5a4a00", YELLOW),
                   "done": (GREEN, "white"), "stuck": (RED, "white"),
                   "stopped": ("#3a3f4b", FG), "unfinished": (RED, "white"),
                   "error": (RED, "white")}
        bg, fg = colours.get(s, (CARD, FG))
        self.pill.set(text=s, bg=bg, fg=fg)
        dot = {"idle": FAINT, "starting": YELLOW, "working": YELLOW, "paused": ORANGE,
               "done": GREEN, "stopped": DIM}.get(s, RED)
        self.dot.itemconfigure(self.dot_id, fill=dot)
        busy = s in ("starting", "working", "paused")
        self.pause_btn.set(text="Resume" if s == "paused" else "Pause",
                           enabled=s in ("working", "paused"))
        self.stop_btn.set(enabled=busy)
        self.mini_pause.set(text=self.glyphs["play" if s == "paused" else "pause"],
                            enabled=s in ("working", "paused"))
        self.mini_stop.set(enabled=busy)
        self.help.configure(text=(
            "The agent has the cursor. Your blue pointer and keys reach only this "
            "panel: Send steers the task, Esc twice pauses."
            if s == "working" else
            "While the agent works, your mouse and keyboard reach only this panel, "
            "and the agent cannot see it."))

    # -- driving: who holds the real cursor ----------------------------------------

    def _drive(self, on):
        """Hand the real cursor and keyboard to the agent, or back to you."""
        if on:
            set_exstyle(self.hwnd, add=WS_EX_NOACTIVATE)       # clicks keep the app's focus
            self.router.drive(True)
            self.over.show(True)
        else:
            if self.router.driving:
                self.router.drive(False)
                u32.SetCursorPos(*self.router.user)            # the cursor is yours again

                def back():
                    # ...and again once the click being relayed (your Pause)
                    # or an action already under way lets go of the input: at
                    # your mouse, which the router keeps following meanwhile.
                    with self.input_lock:
                        u32.SetCursorPos(*self.router.user)
                threading.Thread(target=back, daemon=True).start()
            set_exstyle(self.hwnd, remove=WS_EX_NOACTIVATE)
            self.over.show(False)

    def hint(self):
        if time.time() - self._hint_at > 4:
            self._hint_at = time.time()
            self.log("The agent has the cursor: your clicks reach only this panel. "
                     "Pause (or Esc twice) to take over.", "dim")

    def typed(self, vk, scan, mods):
        """A key you pressed while the agent drives, into the panel's box."""
        e = self.entry
        if "ctrl" in mods:
            if vk == 0x0D:
                self.send()
            elif vk == ord("A"):
                e.tag_add("sel", "1.0", "end-1c")
            elif vk == ord("V"):
                try:
                    e.insert("insert", self.root.clipboard_get())
                except tk.TclError:
                    pass
            elif vk == ord("C") and e.tag_ranges("sel"):
                self.root.clipboard_clear()
                self.root.clipboard_append(e.get("sel.first", "sel.last"))
            return
        if e.tag_ranges("sel") and vk in (0x08, 0x2E):
            e.delete("sel.first", "sel.last")
            return
        moves = {0x25: "insert-1c", 0x27: "insert+1c", 0x26: "insert-1l",
                 0x28: "insert+1l", 0x24: "insert linestart", 0x23: "insert lineend"}
        if vk == 0x08:
            e.delete("insert-1c")
        elif vk == 0x2E:
            e.delete("insert")
        elif vk in moves:
            e.mark_set("insert", moves[vk])
        elif vk == 0x0D:
            e.insert("insert", "\n")
        else:
            state = (ctypes.c_ubyte * 256)()
            if "shift" in mods:
                state[0x10] = 0x80
            if u32.GetKeyState(0x14) & 1:
                state[0x14] = 1
            buf = ctypes.create_unicode_buffer(8)
            n = u32.ToUnicodeEx(vk, scan, ctypes.cast(state, ctypes.c_char_p), buf, 8,
                                0, u32.GetKeyboardLayout(0))
            if n > 0 and buf.value.isprintable():
                if e.tag_ranges("sel"):
                    e.delete("sel.first", "sel.last")
                e.insert("insert", buf.value[:n])
        e.see("insert")

    # -- what the agent's Run calls (its own thread) ---------------------------

    def event(self, kind, **d):
        if kind == "start":
            self.step_n = 0
            self.ui(self.log, "Looking at the screen…", "dim")
            self.ui(self._mini_text, "looking at the screen…")
        elif kind == "think":
            self.ui(self.log, "· " + d["text"].strip()[:400], "think")
        elif kind == "say":
            self.ui(self.log, d["text"].strip()[:600], "say")
        elif kind == "tool":
            self.step_n = d["step"]
            line = self._describe(d)
            self.ui(self.log, line, "step")
            self.ui(self._mini_text, line.strip().split(" — ")[0])

    @staticmethod
    def _describe(d):
        a, name = d["args"], d["name"]
        first = (d["reply"] or "").splitlines()[0] if d["reply"] else ""
        if name in ("click", "point"):
            what = f"{name} ({a.get('x')}, {a.get('y')})"
        elif name == "focus":
            what = f"look closer at {a.get('ref')}"
        elif name == "type_text":
            what = f"type “{a.get('text', '')[:40]}”"
        elif name == "key":
            what = f"press {a.get('keys')}"
        elif name == "screen":
            what = "look at the screen"
        else:
            what = name
        hit = first.split(" — ", 1)[1] if " — " in first else ""
        err = first if first.startswith(("error", "not done", "refused")) else ""
        tail = f" — {hit}" if hit else (f" — {err[:90]}" if err else "")
        return f"{d['step']:>2}. {what}{tail}"

    def glide_at(self, x, y):
        """The agent's tag follows the cursor along its glide and its drags."""
        self.over.point(x, y)

    def pressed(self, x, y):
        """A click or the start of a drag, once the glide has arrived: a ripple."""
        self.over.point(x, y)
        self.ui(self.over.flash)

    def before_input(self, kind, pts):
        """Runs before every input event, on the agent's thread. Waits for any
        click of yours being relayed to the panel, and if the agent is clicking
        where the panel is, lets that click pass through the panel to the app
        under it (the agent cannot see the panel, so it means the app), fading
        the panel meanwhile so you see what it clicks. It used to hide the panel
        instead, which made it blink. Returns what to run afterwards."""
        self.input_lock.acquire()
        c = self.control
        if c.stop.is_set() or c.paused:
            # It came while this action waited for the lock (your click on
            # Pause is delivered under it): the action was planned before, so
            # it is not carried out, and the cursor stays with you.
            self.input_lock.release()
            raise A.Refused(A.STOPPED_NOTE if c.stop.is_set() else A.PAUSED_NOTE)
        through = False
        try:
            if kind in ("key", "type") and self.target:
                # Keys go to the focused window, and in Blender to the area
                # under the cursor: make sure both are still the agent's.
                if not _has_focus(self.target):
                    h = _main_window(self.target)
                    if h:
                        A.focus_window(h)
            if pts:
                # The pointer itself is moved by glide_at and pressed.
                l, t, rr, b = rect(self.hwnd)
                through = any(l <= px < rr and t <= py < b for px, py in pts) \
                    and u32.IsWindowVisible(self.hwnd)
            if through:
                set_exstyle(self.hwnd, add=WS_EX_TRANSPARENT)    # layered + this: click-through
                set_alpha(self.hwnd, 0.4)
                self._through = True
        except Exception:                                        # noqa: BLE001
            self.input_lock.release()
            raise

        def after():
            try:
                if through:
                    time.sleep(0.2)          # the app handles the click before the panel is back
                    set_exstyle(self.hwnd, remove=WS_EX_TRANSPARENT)
                    set_alpha(self.hwnd, ALPHA)
                    self._through = False
            finally:
                self.input_lock.release()
        return after

    # -- targets -------------------------------------------------------------------

    def _target_changed(self, *_):
        self.pick_btn.set(enabled=TARGETS[self.tvar.get()] == "pick")

    def pick(self):
        self.log("Click the window the agent should use…", "warn")
        start = time.time()

        def poll():
            h = u32.GetForegroundWindow()
            h = u32.GetAncestor(h, 2) or h if h else None
            pid, exe = A._owner(h)
            if pid and pid != os.getpid():
                title = A.window_title(h)
                # A picked window is guarded as that one window: its process
                # may run other windows too (Explorer runs the taskbar).
                self._set_target({"kind": "pick", "name": title or exe, "pid": pid,
                                  "exe": exe, "proc": None, "hwnd": h, "window": True,
                                  "title": title})
                self.root.lift()
                return
            if time.time() - start < 30:
                self.root.after(200, poll)
            else:
                self.log("No window picked.", "dim")
        self.root.after(300, poll)

    def _set_target(self, t):
        def key(x):
            return x.get("pid"), (x.get("hwnd") if x.get("window") else None)
        if self.target and t and key(self.target) == key(t):
            return
        self.target, self.run = t, None
        if t:
            self.tinfo.configure(text=(f"{t['title'][:40]} · " if t.get("title") else "")
                                      + f"{t['exe']} · pid {t['pid']}")
            self.log(f"Target: {t['exe']} (pid {t['pid']}).", "dim")

    def _ensure_target(self, kind):
        """On the worker thread: a live target of the wanted kind."""
        t = self.target
        alive = _alive(t)
        if kind == "pick":
            if not alive or t["kind"] != "pick":
                raise RuntimeError("pick a window first (Works in › A window I pick › Pick)")
            return t
        if alive and t["kind"] == kind:
            return t
        self.ui(self.log, f"Starting a fresh {A.APPS[kind]['name']}…", "dim")
        t = A.launch(kind)
        t["title"] = t["name"]
        done = threading.Event()
        self.ui(lambda: (self._set_target(t), done.set()))
        done.wait(5)
        return t

    # -- commands --------------------------------------------------------------------

    def send(self, text=None, kind=None):
        text = (text if text is not None else self.entry.get("1.0", "end")).strip()
        if not text:
            return "empty"
        if self.worker and self.worker.is_alive():
            if self.run is None:
                self.log("Still starting: try again in a moment.", "warn")
                return "busy"
            self.entry.delete("1.0", "end")
            self.run.inbox.put(text)
            self.log(f"You (to the agent, while it works): {text}", "you")
            return "steered"
        if A.session_state() != "active":
            self.log("This Windows session is not active (RDP disconnected?).", "bad")
            return "inactive"
        if kind in TARGETS.values():
            # Sent from outside (HTTP): show it in the selector as well.
            self.tvar.set(next(k for k, v in TARGETS.items() if v == kind))
            self._target_changed()
        kind = kind or TARGETS[self.tvar.get()]
        self.entry.delete("1.0", "end")
        self.log(f"You: {text}", "you")
        self.control = A.Control()
        self._set_status("starting")
        self.worker = threading.Thread(target=self._work, args=(text, kind), daemon=True)
        self.worker.start()
        return "started"

    def _work(self, text, kind):
        result = None
        try:
            t = self._ensure_target(kind)
            if self.run is None or getattr(self.run, "_target", None) is not t:
                self.run = A.Run(text, t["exe"], self.args.model, self.args.effort,
                                 self.args.max_steps, pid=t["pid"], ui=self,
                                 control=self.control, summary="auto",
                                 kind=t["kind"] if t["kind"] in A.APPS else None,
                                 hwnd=t["hwnd"] if t.get("window") else None)
                self.run._target = t
            self.run.control = self.control
            if self.args.show_in_captures and not getattr(self.run, "_blind", False):
                self._blind(self.run)
            h = _main_window(t)
            if h:
                A.focus_window(h)
            self._take_lock()
            done = threading.Event()
            self.ui(lambda: (self._working(), done.set()))
            done.wait(5)
            result = asyncio.run(self.run.go(text))
        except (Exception, SystemExit) as e:                 # noqa: BLE001
            # launch() raises SystemExit (it is the command line's way out):
            # caught here, or the panel would sit at "starting" for ever.
            msg = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
            self.ui(self.log, f"Error: {msg}", "bad")
            self.ui(self._set_status, "error")
        finally:
            self.ui(self._drive, False)
            self._drop_lock()
            if result:
                self.last_dir = result.get("dir")
                self.ui(self._finished, result)

    def _blind(self, run):
        """--show-in-captures: the panel is in screenshots, so take it out of
        the AGENT's by excluding our windows from capture for the moment it
        captures. Measured 2026-10-20: an agent that could see the panel took
        it for a blocking dialog and spent 15 of 35 steps on it. The exclusion
        changes captures only, never the screen, and applies at once; hiding
        the windows instead made them blink at every step."""
        grab = run.session.grab

        def blind_grab():
            ours = [self.hwnd] + self.over.hwnds
            for h in ours:
                u32.SetWindowDisplayAffinity(h, WDA_EXCLUDEFROMCAPTURE)
            time.sleep(0.03)
            try:
                return grab()
            finally:
                for h in ours:
                    u32.SetWindowDisplayAffinity(h, 0)
        run.session.grab = blind_grab
        run._blind = True

    def _working(self):
        self._set_status("working")
        self._drive(True)

    def _finished(self, r):
        e = r["ended"]
        s = e["status"]
        tag = {"done": "ok", "stuck": "bad", "unfinished": "bad"}.get(s, "dim")
        mark = {"done": "✓", "stuck": "✗", "stopped": "■"}.get(s, "•")
        self.log(f"{mark} {s}: {e.get('reason', '')}", tag)
        u = r["usage"]
        c = (r.get("cost_usd") or {}).get("total")
        self.log(f"   {r['steps']} steps · {r['seconds']:.0f}s · "
                 f"{u['input'] // 1000}k tokens in"
                 + (f" · ${c:.3f}" if c is not None else "")
                 + " · Trace opens its run folder", "dim")
        secs = int(r["seconds"])
        self.prog.configure(text=f"{r['steps']} steps · {secs // 60}:{secs % 60:02d}"
                                 + (f" · ${c:.3f}" if c is not None else ""))
        self._mini_text(e.get("reason", ""))
        self._set_status(s)

    def open_trace(self):
        d = self.last_dir or (self.run.dir if self.run is not None and
                              getattr(self.run, "dir", None) else None)
        if self.status == "working":
            self.log("Pause first: a window opening now would cover the app.", "dim")
            return "busy"
        if d and os.path.isdir(d):
            os.startfile(d)
            return d
        self.log("No run yet.", "dim")
        return None

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.control.stop.set()
            self.control.resume()
            self.log("Stopping…", "warn")
            return "stopping"
        return "idle"

    def toggle_pause(self):
        return self.resume() if self.control.paused else self.pause()

    def pause(self):
        if self.status != "working":
            return self.status
        self.control.pause("you paused it")
        self._drive(False)
        self._set_status("paused")
        self.log("Paused: the cursor is yours. Resume when you're ready.", "warn")
        return "paused"

    def resume(self):
        if not self.control.paused:
            return self.status
        if self.target:
            h = _main_window(self.target)
            if h:
                A.focus_window(h)
        self.control.resume()
        self._set_status("working")
        self._drive(True)
        self.log("Resumed: the agent has the cursor again.", "warn")
        return "working"

    def _take_lock(self):
        if LOCK and not os.path.exists(LOCK):
            with open(LOCK, "w", encoding="utf-8") as f:
                f.write("# Desktop held by Deskhand (the user's panel)\n\n"
                        f"- **Taken:** {time.strftime('%Y-%m-%d %H:%M')}; deleted "
                        "when the task ends.\n")
            self._took_lock = True

    def _drop_lock(self):
        if self._took_lock and LOCK and os.path.exists(LOCK):
            os.remove(LOCK)
        self._took_lock = False

    def preview(self, secs=6):
        """Show the frame and pointers for a few seconds, with no agent running."""
        if self.status == "working":
            return "busy"
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.over.show(True)
        self.over.point(sw // 3, sh // 2)
        self.over.you_to(sw // 3 + 160, sh // 2 + 90)
        self.over.flash()
        self.root.after(int(secs * 1000), lambda: self.status != "working"
                        and self.over.show(False))
        return "preview"

    def quit(self):
        self.stop()
        self._drive(False)
        self._drop_lock()
        self.root.after(200, self.root.destroy)
        return "bye"

    # -- local control port --------------------------------------------------------

    def state(self):
        t = self.target or {}
        return {"status": self.status, "paused": self.control.paused,
                "target": {k: t.get(k) for k in ("kind", "pid", "exe", "title")},
                "excluded_from_capture": bool(self.excluded and self.over.excluded),
                "panel_rect": rect(self.hwnd), "click_through": self._through,
                "mini": self.mini,
                "router": {"hooked": getattr(self.router, "ok", None),
                           "driving": self.router.driving, "you": self.router.user,
                           "cursor": cursor(), "blocked_clicks": self.router.blocked},
                "entry": self.entry.get("1.0", "end-1c"),
                "widgets": {n: (w.winfo_rootx() + w.winfo_width() // 2,
                                w.winfo_rooty() + w.winfo_height() // 2)
                            for n, w in (("entry", self.entry), ("send", self.send_btn),
                                         ("pause", self.pause_btn),
                                         ("stop", self.stop_btn), ("log", self.text),
                                         ("minimise", self.min_btn),
                                         ("close", self.close_btn),
                                         ("mini_pause", self.mini_pause),
                                         ("mini_stop", self.mini_stop))},
                "log": [m for _, m in self.lines[-25:]]}

    def _serve(self):
        panel = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _on_ui(self, fn, *a):
                box, done = {}, threading.Event()

                def run():
                    box["r"] = fn(*a)
                    done.set()
                panel.ui(run)
                done.wait(10)
                return box.get("r")

            def do_GET(self):
                self._reply(self._on_ui(panel.state))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n) or b"{}")
                route = {"/task": lambda: panel.send(d.get("text", ""),
                                                     d.get("target")),
                         "/stop": panel.stop, "/pause": panel.pause,
                         "/resume": panel.resume, "/quit": panel.quit,
                         "/preview": panel.preview,
                         "/mini": panel.toggle_mini}.get(self.path)
                self._reply({"result": self._on_ui(route) if route else "unknown"})

            def log_message(self, *a):
                pass

        try:
            srv = ThreadingHTTPServer(("127.0.0.1", self.args.port), H)
        except OSError as e:
            self.ui(self.log, f"Control port {self.args.port} is in use ({e.strerror}); "
                              f"scripts cannot reach this panel.", "dim")
            return
        srv.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.environ.get("CUA_MODEL", "gpt-6-sol"))
    ap.add_argument("--effort", default=os.environ.get("CUA_EFFORT", "medium"))
    ap.add_argument("--max-steps", type=int, default=80)
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--show-in-captures", action="store_true",
                    help="do not hide the panel from screen capture (for demos); "
                         "it then hides itself only while the agent captures")
    a = ap.parse_args()
    Panel(a).root.mainloop()


if __name__ == "__main__":
    main()
