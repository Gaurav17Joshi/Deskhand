"""Photograph the panel at work: a task through the control port, a screenshot
every ~1.5 s, one pause in the middle, and an animated GIF at the end.

    .venv\\Scripts\\python scripts\\demo_capture.py "task text" [--target blender]

Start the panel with --show-in-captures first, or the screenshots will not
contain it (that is the point of the exclusion). Frames go to
runs/panel/photos/<time>/.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CUA = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(CUA, "repos", "studio"))
from mcp_server.screen import grab  # noqa: E402

URL = "http://127.0.0.1:8777"
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def api(path, body=None):
    req = urllib.request.Request(URL + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None)
    return json.load(urllib.request.urlopen(req, timeout=30))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("task")
    ap.add_argument("--target", default="blender")
    ap.add_argument("--pause-after", type=int, default=12, help="frames before the pause")
    a = ap.parse_args()
    out = os.path.join(CUA, "runs", "panel", "photos", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(out, exist_ok=True)
    print("task ->", api("/task", {"text": a.task, "target": a.target}), flush=True)
    frames, n, paused_done, t0 = [], 0, False, time.time()

    def shot(tag):
        nonlocal n
        n += 1
        p = os.path.join(out, f"{n:03d}-{tag}.png")
        grab().save(p)
        frames.append(p)
        return p

    while time.time() - t0 < 900:
        st = api("/state")
        s = st["status"]
        if s in ("working", "paused", "starting"):
            shot(s)
            if s == "working" and not paused_done and n >= a.pause_after:
                print("pause ->", api("/pause", {}), flush=True)
                time.sleep(1.0)
                shot("paused")
                time.sleep(2.0)
                shot("paused")
                print("resume ->", api("/resume", {}), flush=True)
                paused_done = True
        elif n and s not in ("idle",):
            time.sleep(1.0)
            shot(s)
            break
        time.sleep(1.5)
    st = api("/state")
    print("final:", st["status"], "|", (st["log"] or [""])[-2:], flush=True)
    from PIL import Image
    imgs = [Image.open(p).convert("RGB").resize((960, 600)) for p in frames]
    gif = os.path.join(out, "panel_in_action.gif")
    imgs[0].save(gif, save_all=True, append_images=imgs[1:], duration=700, loop=0,
                 optimize=True)
    print(f"{len(frames)} frames + {gif}")


if __name__ == "__main__":
    main()
