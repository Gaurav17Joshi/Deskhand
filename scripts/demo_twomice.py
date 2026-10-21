"""Photograph two-mouse mode: the agent works in Blender while a simulated
person moves their own pointer, types into the panel and steers the task.

    .venv\\Scripts\\python scripts\\demo_twomice.py

Start the panel with --show-in-captures first. Frames and the named shots go
to runs/panel/photos/<time>-twomice/.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CUA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(CUA, "repos", "studio"))
import test_panel as T  # noqa: E402
from mcp_server.screen import grab  # noqa: E402

out = os.path.join(CUA, "runs", "panel", "photos", time.strftime("%Y%m%d-%H%M%S") + "-twomice")
os.makedirs(out, exist_ok=True)
frames, stop = [], threading.Event()


def snap(name):
    p = os.path.join(out, name)
    grab().save(p)
    print("shot", name, flush=True)
    return p


def film():
    n = 0
    while not stop.is_set():
        n += 1
        frames.append(snap(f"f{n:03d}.png"))
        stop.wait(2.0)


task = "Add a cone to the scene, then make it twice as tall."
T.say(f"task -> {T.api('/task', {'text': task, 'target': 'blender'})}")
T.wait_for(lambda s: s["status"] == "working" and len(T.steps(s)) >= 3, 240, 1)
threading.Thread(target=film, daemon=True).start()

T.person_move(430, 300)
time.sleep(1.5)
snap("A_two_pointers.png")

w = T.api("/state")["widgets"]
T.person_click(*w["entry"])
T.person_type("also move it 2 m to the left")
time.sleep(1.0)
snap("B_typing_while_it_works.png")

T.person_key(0x11, up_too=False)
T.person_key(0x0D)
e = T._IN(1)
e.u.ki = T._KI(0x11, 0, 0x2, 0, 0)
T._send(e)
time.sleep(1.5)
snap("C_steered.png")

T.person_move(300, 600)
st = T.finish(600)
time.sleep(1.0)
snap("D_done.png")
stop.set()
time.sleep(0.5)

from PIL import Image  # noqa: E402
imgs = [Image.open(p).convert("RGB").resize((960, 600)) for p in frames if os.path.exists(p)]
gif = os.path.join(out, "twomice_in_action.gif")
imgs[0].save(gif, save_all=True, append_images=imgs[1:], duration=600, loop=0, optimize=True)
print(len(imgs), "frames ->", gif)
