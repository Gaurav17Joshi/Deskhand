"""Deskhand's floating panel: tell the agent what to do, watch it, steer it.

    .venv\\Scripts\\pythonw scripts\\deskhand_ui.py         (or Deskhand.cmd)

A small always-on-top window. Pick what the agent works in (a fresh Blender,
a fresh Chrome, or a window you click), type a task and Send.

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
from screen capture. --show-in-captures turns that off to record a demo; the
panel then hides itself for the instant the agent takes each screenshot.

For scripts and tests it also listens on 127.0.0.1:<port> (default 8777):
    GET  /state                      status, target, last log lines, router
    POST /task   {"text", "target"}  target: blender | chrome | current
    POST /stop   /pause   /resume   /quit   /preview
"""
import argparse
import asyncio
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
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
BG, CARD, FG, DIM = "#16181d", "#1f2229", "#e6e6e6", "#8b8f98"
BLUE, RED, GREEN = "#4c8dff", "#e5484d", "#46a758"
KEY = "#010203"                       # transparent colour of the pointer windows
LOCK = os.environ.get("DESKHAND_LOCK")


def hwnd_of(w):
    w.update_idletasks()
    return u32.GetAncestor(w.winfo_id(), 2)                 # GA_ROOT


def hide_from_capture(hwnd):
    ok = u32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    got = wintypes.DWORD()
    u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
    return bool(ok) and got.value == WDA_EXCLUDEFROMCAPTURE


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

    def _pointer(self, colour, label, ink):
        w = tk.Toplevel(self.root, bg=KEY)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.attributes("-transparentcolor", KEY)
        w.geometry("90x44+-400+-400")
        c = tk.Canvas(w, width=90, height=44, bg=KEY, highlightthickness=0)
        c.pack()
        arrow = c.create_polygon(2, 2, 2, 26, 9, 20, 14, 31, 18, 29, 13, 18,
                                 22, 18, fill=colour, outline="black", width=1.5)
        c.create_rectangle(20, 22, 20 + 8 * len(label) + 10, 38, fill=colour, outline="")
        c.create_text(20 + (8 * len(label) + 10) // 2, 30, text=label, fill=ink,
                      font=("Segoe UI", 8, "bold"))
        return w, c, arrow

    def show(self, on, you=None):
        for h in self.frame_hwnds + [self.ptr_hwnd]:
            u32.ShowWindowAsync(h, SW_SHOWNOACTIVATE if on else SW_HIDE)
        u32.ShowWindowAsync(self.you_hwnd, SW_SHOWNOACTIVATE if (on if you is None
                                                                 else you) else SW_HIDE)

    def _place(self, hwnd, x, y):
        u32.SetWindowPos(hwnd, HWND_TOPMOST, int(x) - 2, int(y) - 2, 0, 0,
                         SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_ASYNC)

    def point(self, x, y):
        """The agent's tag on the real cursor. Safe from any thread."""
        self._place(self.ptr_hwnd, x, y)

    def you_to(self, x, y):
        """Your own pointer. Safe from any thread."""
        self._place(self.you_hwnd, x, y)

    def flash(self):
        """A click: the agent's pointer goes yellow for a moment. Tk thread."""
        self.canvas.itemconfigure(self.arrow, fill=YELLOW)
        self.root.after(350, lambda: self.canvas.itemconfigure(self.arrow, fill=ORANGE))


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

TARGETS = {"New Blender": "blender", "New Chrome": "chrome",
           "A window I pick": "pick"}


class Panel:
    def __init__(self, args):
        self.args = args
        self.q = queue.Queue()
        self.input_lock = threading.Lock()
        self.root = r = tk.Tk()
        r.title("Deskhand")
        r.configure(bg=BG)
        r.attributes("-topmost", True)
        r.attributes("-toolwindow", True)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        try:
            k = u32.GetDpiForSystem() / 96.0
        except Exception:                                      # noqa: BLE001
            k = 1.0
        W, H = int(380 * k), int(580 * k)
        r.geometry(f"{W}x{H}+{sw - W - int(24 * k)}+{max(0, sh - H - int(70 * k))}")
        r.protocol("WM_DELETE_WINDOW", self.quit)
        self._build()
        self.hwnd = hwnd_of(r)
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
        self._hidden = False
        self._hint_at = 0.0
        cap = ("hidden from screen capture" if self.excluded and self.over.excluded
               else "VISIBLE to screen capture" if args.show_in_captures
               else "could not hide from capture")
        self.log(f"Ready. The panel and pointers are {cap}.", "dim")
        self._set_status("idle")
        r.after(40, self._pump)
        if args.port:
            threading.Thread(target=self._serve, daemon=True).start()

    # -- layout ------------------------------------------------------------

    def _build(self):
        r = self.root
        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(top, text="Deskhand", bg=BG, fg=FG,
                 font=("Segoe UI", 13, "bold")).pack(side="left")
        self.pill = tk.Label(top, text="idle", bg=CARD, fg=DIM, padx=8, pady=1,
                             font=("Segoe UI", 9, "bold"))
        self.pill.pack(side="right")

        row = tk.Frame(r, bg=BG)
        row.pack(fill="x", padx=12, pady=4)
        tk.Label(row, text="Works in", bg=BG, fg=DIM,
                 font=("Segoe UI", 9)).pack(side="left")
        self.tvar = tk.StringVar(value="New Blender")
        om = tk.OptionMenu(row, self.tvar, *TARGETS, command=self._target_changed)
        om.configure(bg=CARD, fg=FG, activebackground=CARD, activeforeground=FG,
                     highlightthickness=0, relief="flat", font=("Segoe UI", 9))
        om["menu"].configure(bg=CARD, fg=FG, font=("Segoe UI", 9))
        om.pack(side="left", padx=6)
        self.pick_btn = tk.Button(row, text="Pick", command=self.pick, bg=CARD,
                                  fg=FG, relief="flat", font=("Segoe UI", 9),
                                  state="disabled")
        self.pick_btn.pack(side="left")
        tk.Button(row, text="Trace", command=self.open_trace, bg=CARD, fg=FG,
                  relief="flat", font=("Segoe UI", 9)).pack(side="right")
        self.tinfo = tk.Label(r, text="", bg=BG, fg=DIM, anchor="w",
                              font=("Segoe UI", 8))
        self.tinfo.pack(fill="x", padx=12)

        box = tk.Frame(r, bg=CARD)
        box.pack(fill="both", expand=True, padx=12, pady=6)
        self.text = tk.Text(box, bg=CARD, fg=FG, wrap="word", relief="flat",
                            font=("Segoe UI", 9), padx=8, pady=6, height=16,
                            state="disabled", cursor="arrow")
        sb = tk.Scrollbar(box, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, colour in (("you", BLUE), ("step", DIM), ("think", "#a99cff"),
                            ("say", FG), ("ok", GREEN), ("bad", RED),
                            ("warn", YELLOW), ("dim", DIM)):
            self.text.tag_configure(tag, foreground=colour)
        self.text.tag_configure("think", font=("Segoe UI", 9, "italic"))

        self.entry = tk.Text(r, height=3, bg=CARD, fg=FG, insertbackground=FG,
                             relief="flat", wrap="word", font=("Segoe UI", 10),
                             padx=8, pady=6)
        self.entry.pack(fill="x", padx=12)
        self.entry.bind("<Control-Return>", lambda e: (self.send(), "break")[1])

        bar = tk.Frame(r, bg=BG)
        bar.pack(fill="x", padx=12, pady=8)
        b = dict(relief="flat", font=("Segoe UI", 9, "bold"), padx=10, pady=3,
                 fg="white", activeforeground="white")
        self.send_btn = tk.Button(bar, text="Send", command=self.send, bg=BLUE,
                                  activebackground=BLUE, **b)
        self.send_btn.pack(side="left")
        self.pause_btn = tk.Button(bar, text="Pause", command=self.toggle_pause,
                                   bg="#3a3f4b", activebackground="#3a3f4b", **b)
        self.pause_btn.pack(side="left", padx=6)
        self.stop_btn = tk.Button(bar, text="Stop", command=self.stop, bg=RED,
                                  activebackground=RED, **b)
        self.stop_btn.pack(side="left")
        self.help = tk.Label(r, text="", bg=BG, fg=DIM, font=("Segoe UI", 8),
                             justify="left", anchor="w", wraplength=350)
        self.help.pack(fill="x", padx=12, pady=(0, 8))

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

    def log(self, msg, tag="say"):
        self.lines.append((tag, msg))
        self.lines = self.lines[-300:]
        t = self.text
        t.configure(state="normal")
        t.insert("end", msg.rstrip() + "\n", tag)
        t.see("end")
        t.configure(state="disabled")

    def _set_status(self, s):
        self.status = s
        colours = {"idle": (CARD, DIM), "starting": (CARD, YELLOW),
                   "working": (YELLOW, "black"), "paused": ("#5a4a00", YELLOW),
                   "done": (GREEN, "white"), "stuck": (RED, "white"),
                   "stopped": ("#3a3f4b", FG), "unfinished": (RED, "white"),
                   "error": (RED, "white")}
        bg, fg = colours.get(s, (CARD, FG))
        self.pill.configure(text=s, bg=bg, fg=fg)
        self.pause_btn.configure(text="Resume" if s == "paused" else "Pause")
        self.help.configure(text=(
            "The agent has the cursor. Your blue pointer and keyboard reach only "
            "this panel; Send steers the task. Esc twice pauses."
            if s == "working" else
            "Ctrl+Enter sends. While the agent works, your mouse and keyboard "
            "reach only this panel, and the agent cannot see it."))

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
                you = self.router.user
                u32.SetCursorPos(*you)                         # the cursor is yours again
                # ...and again once a click being relayed has finished.
                self.root.after(250, lambda: u32.SetCursorPos(*you))
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
            self.ui(self.log, "Looking at the screen…", "dim")
        elif kind == "think":
            self.ui(self.log, "· " + d["text"].strip()[:400], "think")
        elif kind == "say":
            self.ui(self.log, d["text"].strip()[:600], "say")
        elif kind == "tool":
            self.ui(self.log, self._describe(d), "step")

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

    def before_input(self, kind, pts):
        """Runs before every input event, on the agent's thread. Waits for any
        click of yours being relayed to the panel, draws the agent's pointer,
        and if the agent is clicking where the panel is, hides the panel for
        that click. Returns what to run afterwards."""
        self.input_lock.acquire()
        hide = False
        try:
            if kind in ("key", "type") and self.target:
                # Keys go to the focused window, and in Blender to the area
                # under the cursor: make sure both are still the agent's.
                if A.owner_focused()[0] not in A.family(self.target["pid"]):
                    wins = A.windows_of(self.target["pid"])
                    if wins:
                        A.focus_window(wins[0])
            if pts:
                x, y = pts[-1]
                self.over.point(x, y)
                if kind in ("click", "drag"):
                    self.ui(self.over.flash)
                l, t, rr, b = rect(self.hwnd)
                hide = any(l <= px < rr and t <= py < b for px, py in pts) \
                    and u32.IsWindowVisible(self.hwnd)
            if hide:
                u32.ShowWindowAsync(self.hwnd, SW_HIDE)
                time.sleep(0.15)
                self._hidden = True
        except Exception:                                        # noqa: BLE001
            self.input_lock.release()
            raise

        def after():
            try:
                if hide:
                    time.sleep(0.25)
                    u32.ShowWindowAsync(self.hwnd, SW_SHOWNOACTIVATE)
                    self._hidden = False
            finally:
                self.input_lock.release()
        return after

    # -- targets -------------------------------------------------------------------

    def _target_changed(self, *_):
        self.pick_btn.configure(state="normal" if TARGETS[self.tvar.get()] == "pick"
                                else "disabled")

    def pick(self):
        self.log("Click the window the agent should use…", "warn")
        start = time.time()

        def poll():
            h = u32.GetForegroundWindow()
            pid, exe = A._owner(h)
            if pid and pid != os.getpid():
                buf = ctypes.create_unicode_buffer(256)
                u32.GetWindowTextW(h, buf, 256)
                self._set_target({"kind": "pick", "pid": pid, "exe": exe,
                                  "proc": None, "title": buf.value})
                self.root.lift()
                return
            if time.time() - start < 30:
                self.root.after(200, poll)
            else:
                self.log("No window picked.", "dim")
        self.root.after(300, poll)

    def _set_target(self, t):
        if self.target and t and (self.target.get("pid"), self.target.get("exe")) == (
                t.get("pid"), t.get("exe")):
            return
        self.target, self.run = t, None
        if t:
            self.tinfo.configure(text=f"Working in {t['exe']} (pid {t['pid']})"
                                      + (f" — {t['title'][:40]}" if t.get("title") else ""))
            self.log(f"Target: {t['exe']} (pid {t['pid']}).", "dim")

    def _ensure_target(self, kind):
        """On the worker thread: a live target of the wanted kind."""
        t = self.target
        alive = t and (t["proc"] is None or t["proc"].poll() is None) \
            and A.windows_of(t["pid"])
        if kind == "pick":
            if not alive or t["kind"] != "pick":
                raise RuntimeError("pick a window first (Works in › A window I pick › Pick)")
            return t
        if alive and t["kind"] == kind:
            return t
        self.ui(self.log, f"Starting a fresh {A.APPS[kind]['name']}…", "dim")
        proc, hwnd, _ = A.launch(kind)
        t = {"kind": kind, "pid": proc.pid, "proc": proc,
             "exe": os.path.basename(A.APPS[kind]["exe"]).lower(),
             "title": A.APPS[kind]["name"]}
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
            if self.run is None or self.run.session.act.pid != t["pid"]:
                self.run = A.Run(text, t["exe"], self.args.model, self.args.effort,
                                 self.args.max_steps, pid=t["pid"], ui=self,
                                 control=self.control, summary="auto")
            self.run.control = self.control
            if self.args.show_in_captures and not getattr(self.run, "_blind", False):
                self._blind(self.run)
            wins = A.windows_of(t["pid"])
            if wins:
                A.focus_window(wins[0])
            self._take_lock()
            done = threading.Event()
            self.ui(lambda: (self._working(), done.set()))
            done.wait(5)
            result = asyncio.run(self.run.go(text))
        except Exception as e:                               # noqa: BLE001
            self.ui(self.log, f"Error: {type(e).__name__}: {e}", "bad")
            self.ui(self._set_status, "error")
        finally:
            self.ui(self._drive, False)
            self._drop_lock()
            if result:
                self.last_dir = result.get("dir")
                self.ui(self._finished, result)

    def _blind(self, run):
        """--show-in-captures: the panel is in screenshots, so take it out of
        the AGENT's by hiding our windows for the moment it captures. Measured
        2026-10-20: an agent that could see the panel took it for a blocking
        dialog and spent 15 of 35 steps on it, even trying to drag it away."""
        grab = run.session.grab

        def blind_grab():
            ours = [h for h in [self.hwnd] + self.over.hwnds if u32.IsWindowVisible(h)]
            for h in ours:
                u32.ShowWindowAsync(h, SW_HIDE)
            time.sleep(0.12)                     # let the compositor drop them
            try:
                return grab()
            finally:
                for h in ours:
                    u32.ShowWindowAsync(h, SW_SHOWNOACTIVATE)
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
        self.log(f"   {r['steps']} steps · {r['seconds']:.0f}s · "
                 f"{u['input'] // 1000}k tokens in · Trace opens its run folder", "dim")
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
            wins = A.windows_of(self.target["pid"])
            if wins:
                A.focus_window(wins[0])
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
                "panel_rect": rect(self.hwnd), "panel_hidden_for_click": self._hidden,
                "router": {"hooked": getattr(self.router, "ok", None),
                           "driving": self.router.driving, "you": self.router.user,
                           "cursor": cursor(), "blocked_clicks": self.router.blocked},
                "entry": self.entry.get("1.0", "end-1c"),
                "widgets": {n: (w.winfo_rootx() + w.winfo_width() // 2,
                                w.winfo_rooty() + w.winfo_height() // 2)
                            for n, w in (("entry", self.entry), ("send", self.send_btn),
                                         ("pause", self.pause_btn),
                                         ("stop", self.stop_btn), ("log", self.text))},
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
                         "/preview": panel.preview}.get(self.path)
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
