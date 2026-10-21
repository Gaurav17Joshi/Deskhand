"""Drive the floating panel through its control port and check what it does.

    .venv\\Scripts\\python scripts\\test_panel.py pause|followup|stop|human|twomice|pauseclick|mini

Needs the panel running (scripts/deskhand_ui.py, port 8777). Each test prints
a timeline and PASS/FAIL lines. `human` sends a mouse move WITHOUT the
agent's MAGIC tag, the way your own mouse would arrive, and expects a pause.
"""
import ctypes
import json
import sys
import time
import urllib.request

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

URL = "http://127.0.0.1:8777"
T0 = time.time()


def api(path, body=None):
    req = urllib.request.Request(URL + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=30))


def say(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def steps(st):
    """Step lines of the current task only (after the last "You:" line)."""
    log = st["log"]
    start = max([i for i, l in enumerate(log) if l.startswith("You:")] or [-1])
    return [l for l in log[start + 1:] if l[:3].strip().rstrip(".").isdigit()]


def wait_for(cond, timeout, every=1.0):
    end = time.time() + timeout
    while time.time() < end:
        st = api("/state")
        if cond(st):
            return st
        time.sleep(every)
    return api("/state")


def check(name, ok):
    print(f"{'PASS' if ok else 'FAIL'}  {name}", flush=True)
    return ok


def finish(timeout=600):
    st = wait_for(lambda s: s["status"] not in ("working", "starting", "paused"), timeout, 2)
    say(f"ended: {st['status']}")
    for l in st["log"][-6:]:
        print("      " + l)
    return st


def human_mouse_nudge():
    """A relative mouse move with dwExtraInfo 0 — not the agent's tag."""
    class MI(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("data", ctypes.c_ulong),
                    ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("extra", ctypes.c_size_t)]

    class IN(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("mi", MI)]       # 40 bytes, as SendInput needs
    for dx in (30, -30):
        e = IN(0, MI(dx, 0, 0, 0x0001, 0, 0))                # MOUSEEVENTF_MOVE
        sent = ctypes.windll.user32.SendInput(1, ctypes.byref(e), ctypes.sizeof(IN))
        assert sent == 1, f"SendInput sent {sent} events (struct size {ctypes.sizeof(IN)})"
        time.sleep(0.05)


def test_pause():
    say(f"task -> {api('/task', {'text': 'Add a UV sphere to the scene.', 'target': 'blender'})}")
    st = wait_for(lambda s: s["status"] == "working" and len(steps(s)) >= 3, 240, 1)
    check("agent is working with steps logged", st["status"] == "working")
    say(f"pause -> {api('/pause', {})}")
    n = len(steps(api("/state")))
    time.sleep(12)
    st = api("/state")
    check("status is paused", st["status"] == "paused")
    check("no new steps during a 12 s pause (one in-flight step may land)",
          len(steps(st)) - n <= 1)
    say(f"resume -> {api('/resume', {})}")
    st = finish()
    check("task finished done after resume", st["status"] == "done")


def test_followup():
    say(f"task -> {api('/task', {'text': 'Now move that sphere up: set its Z location to 2 m.', 'target': 'blender'})}")
    st = finish()
    check("follow-up finished done", st["status"] == "done")


def test_stop():
    say(f"task -> {api('/task', {'text': 'Add a cone, then a torus, then a cylinder, each one in a different place.', 'target': 'blender'})}")
    wait_for(lambda s: s["status"] == "working" and len(steps(s)) >= 4, 240, 1)
    say(f"stop -> {api('/stop', {})}")
    st = finish(120)
    check("status is stopped", st["status"] == "stopped")
    n = len(steps(st))
    time.sleep(8)
    check("no steps after stop", len(steps(api("/state"))) == n)


def test_human():
    say(f"task -> {api('/task', {'text': 'Add a monkey (Suzanne) mesh to the scene.', 'target': 'blender'})}")
    wait_for(lambda s: s["status"] == "working" and len(steps(s)) >= 2, 240, 1)
    time.sleep(2)
    say("nudging the mouse the way a person would (no agent tag)")
    human_mouse_nudge()
    st = wait_for(lambda s: s["status"] == "paused", 10, 0.5)
    check("a person's mouse move paused the agent", st["status"] == "paused")
    print("      " + st["log"][-1])
    say(f"resume -> {api('/resume', {})}")
    st = finish()
    check("task finished done after the takeover", st["status"] == "done")


class _MI(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("data", ctypes.c_ulong),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("extra", ctypes.c_size_t)]


class _KI(ctypes.Structure):
    _fields_ = [("vk", ctypes.c_ushort), ("scan", ctypes.c_ushort),
                ("flags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("extra", ctypes.c_size_t)]


class _U(ctypes.Union):
    _fields_ = [("mi", _MI), ("ki", _KI)]


class _IN(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def _send(*events):
    arr = (_IN * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_IN))
    assert sent == len(events), f"SendInput sent {sent}/{len(events)}"


def person_move(x, y):
    """Your mouse, as RDP delivers it: an absolute move with no agent tag."""
    m = ctypes.windll.user32.GetSystemMetrics
    nx = round((x - m(76)) * 65535 / (m(78) - 1))
    ny = round((y - m(77)) * 65535 / (m(79) - 1))
    e = _IN(0)
    e.u.mi = _MI(nx, ny, 0, 0x8000 | 0x0001 | 0x4000, 0, 0)
    _send(e)
    time.sleep(0.15)


def person_click(x, y):
    person_move(x, y)
    for f in (0x2, 0x4):
        e = _IN(0)
        e.u.mi = _MI(0, 0, 0, f, 0, 0)
        _send(e)
        time.sleep(0.08)
    time.sleep(0.4)


def person_key(vk, up_too=True):
    e = _IN(1)
    e.u.ki = _KI(vk, 0, 0, 0, 0)
    _send(e)
    if up_too:
        e = _IN(1)
        e.u.ki = _KI(vk, 0, 0x2, 0, 0)
        _send(e)
    time.sleep(0.03)


def person_type(text):
    for ch in text:
        r = ctypes.windll.user32.VkKeyScanW(ord(ch))
        vk, shift = r & 0xFF, bool((r >> 8) & 1)
        if shift:
            person_key(0x10, up_too=False)
        person_key(vk)
        if shift:
            e = _IN(1)
            e.u.ki = _KI(0x10, 0, 0x2, 0, 0)
            _send(e)


def test_twomice():
    """While the agent drives: your mouse moves your own pointer only, your
    clicks outside the panel are blocked, your typing lands in the panel's
    box, Ctrl+Enter steers, the panel's Pause button works, Esc twice pauses."""
    task = "Add a cone and then a torus to the scene, each in a different place."
    say(f"task -> {api('/task', {'text': task, 'target': 'blender'})}")
    st = wait_for(lambda s: s["status"] == "working" and len(steps(s)) >= 3, 240, 1)
    r0 = st["router"]
    check("router is hooked and driving", r0["hooked"] and r0["driving"])

    say("person: move to the viewport and click it")
    agent_cursor = api("/state")["router"]["cursor"]
    person_click(560, 470)
    st = api("/state")
    r = st["router"]
    check("not paused by the person's mouse", st["status"] == "working")
    check("your pointer followed your mouse", tuple(r["you"]) == (560, 470))
    check("the real cursor did not follow it", tuple(r["cursor"]) != (560, 470))
    check("the click outside the panel was blocked",
          r["blocked_clicks"] > r0["blocked_clicks"])

    w = st["widgets"]
    say("person: click the panel's box and type")
    person_click(*w["entry"])
    person_type("also make the torus bigger")
    time.sleep(0.6)
    st = api("/state")
    check("typing landed in the panel's box", st["entry"] == "also make the torus bigger")
    check("still working while you type", st["status"] == "working")

    say("person: ctrl+enter to steer")
    person_key(0x11, up_too=False)
    person_key(0x0D)
    e = _IN(1)
    e.u.ki = _KI(0x11, 0, 0x2, 0, 0)
    _send(e)
    time.sleep(0.8)
    st = api("/state")
    check("the message went to the running agent",
          any("while it works" in l for l in st["log"][-6:]) and st["entry"] == "")

    say("person: click the panel's Pause button")
    w = st["widgets"]["pause"]
    person_click(*w)
    st = wait_for(lambda s: s["status"] == "paused", 8, 0.5)
    check("the panel's Pause button worked while the agent drove", st["status"] == "paused")
    time.sleep(0.6)
    st = api("/state")
    c = st["router"]["cursor"]
    check(f"pausing gave the real cursor back to you (at {tuple(c)}, Pause at {tuple(w)})",
          not st["router"]["driving"] and abs(c[0] - w[0]) < 3 and abs(c[1] - w[1]) < 3)
    say(f"resume -> {api('/resume', {})}")
    time.sleep(3)

    say("person: Esc twice")
    person_key(0x1B)
    time.sleep(0.15)
    person_key(0x1B)
    st = wait_for(lambda s: s["status"] == "paused", 8, 0.5)
    check("Esc twice paused it", st["status"] == "paused")
    say(f"resume -> {api('/resume', {})}")
    st = finish()
    check("task finished done", st["status"] == "done")


def test_pauseclick():
    """Clicking the panel's Pause while the agent drives hands you the real
    cursor, at your own pointer."""
    say(f"task -> {api('/task', {'text': 'Add a cone to the scene.', 'target': 'blender'})}")
    wait_for(lambda s: s["status"] == "working" and len(steps(s)) >= 2, 240, 1)
    w = api("/state")["widgets"]["pause"]
    person_click(*w)
    st = wait_for(lambda s: s["status"] == "paused", 8, 0.5)
    check("the panel's Pause button paused it", st["status"] == "paused")
    time.sleep(0.6)
    st = api("/state")
    c = st["router"]["cursor"]
    check(f"the real cursor is back with you at Pause {tuple(w)} (it is at {tuple(c)})",
          abs(c[0] - w[0]) < 3 and abs(c[1] - w[1]) < 3 and not st["router"]["driving"])
    say(f"stop -> {api('/stop', {})}")
    finish(120)


def test_mini():
    """While the agent drives: the minimise button folds the panel into its
    header row, the row's own Pause works, and restoring brings the panel back
    at its size."""
    task = "Work out 12345 x 678 by clicking the Calculator's buttons with the mouse."
    say(f"task -> {api('/task', {'text': task, 'target': 'calculator'})}")
    st = wait_for(lambda s: s["status"] == "working" and len(steps(s)) >= 2, 240, 1)
    full = st["panel_rect"]
    person_click(*st["widgets"]["minimise"])
    st = wait_for(lambda s: s["mini"], 8, 0.5)
    r = st["panel_rect"]
    check("minimise folded the panel while the agent drove",
          st["mini"] and r[3] - r[1] < 70 and abs(r[3] - full[3]) <= 2)
    check("still working after folding", st["status"] == "working")
    person_click(*st["widgets"]["mini_pause"])
    st = wait_for(lambda s: s["status"] == "paused", 8, 0.5)
    check("the folded row's Pause paused it", st["status"] == "paused")
    person_click(*st["widgets"]["minimise"])
    st = wait_for(lambda s: not s["mini"], 8, 0.5)
    r = st["panel_rect"]
    check("restore brought the panel back at its size",
          not st["mini"] and r[3] - r[1] == full[3] - full[1])
    say(f"resume -> {api('/resume', {})}")
    st = finish()
    check("task finished done", st["status"] == "done")


if __name__ == "__main__":
    {"pause": test_pause, "followup": test_followup, "stop": test_stop,
     "human": test_human, "twomice": test_twomice, "pauseclick": test_pauseclick,
     "mini": test_mini}[sys.argv[1]]()
