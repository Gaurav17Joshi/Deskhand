"""One full perception pass on an image, as the agent would get it.

    .venv\\Scripts\\python scripts\\check_perception.py [image | --screen] [--focus 1.2]

L0 -> L1 + OCR + icons (local servers, scripts/stack.py) -> L2 areas and names
(OpenAI, through StudioApprentice's groups.py / view.py). Prints what each reader found,
the timings, and the outline screen() would return; --focus also writes the
marked close-up the agent would see for that ref to runs/focus_<ref>.png.
Run twice in one process to see the per-region L2 cache at work (--twice).
"""
import os
import sys
import time

for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
HERE = os.path.dirname(os.path.abspath(__file__))
CUA = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import cua_agent  # noqa: E402  (sets paths, config and env)

cua_agent._load_key()

from PIL import Image  # noqa: E402

from perception import view as V  # noqa: E402
from perception.config import load  # noqa: E402
from perception.detect import Detector  # noqa: E402
from perception.icons_remote import RemoteLibrary  # noqa: E402
from perception.live import Live  # noqa: E402
from perception.ocr import Ocr  # noqa: E402
from perception.soft import note, perceive_soft  # noqa: E402

args = sys.argv[1:]
focus = args[args.index("--focus") + 1] if "--focus" in args else None
if "--screen" in args:
    from mcp_server.screen import grab
    img, src = grab(), "the live screen"
else:
    rest = [a for a in args if not a.startswith("--") and a != focus]
    src = rest[0] if rest else os.path.join(
        CUA, "repos", "StudioApprentice-perception", "perception",
        "selftest_data", "blender-shader-editor.jpg")
    img = Image.open(src).convert("RGB")
print(f"image: {src} {img.size}")

c = load()
det, ocr, icons, live = Detector(c), Ocr(c), RemoteLibrary(c), Live()
for n in range(2 if "--twice" in args else 1):
    t0 = time.time()
    p, dropped = perceive_soft(img, det, conf={"l0": 0.25, "l1": 0.35}, ocr=ocr,
                               icons=icons, groups=True, live=live)
    t1 = time.time()
    text = V.outline(p)
    t2 = time.time()
    print(f"\n=== pass {n + 1}: perceive {t1 - t0:.1f}s, outline (names) {t2 - t1:.1f}s")
    print(f"blocking={p.blocking} ({p.reason}); L1 regions {len(p.l1)}, text lines "
          f"{len(p.text)}, panels {len(p.panels)}, icons {len(p.icons)} "
          f"({sum(1 for i in p.icons if i.label)} named), areas {len(p.groups)}")
    if dropped:
        print("DROPPED:", dropped)
    print("\n" + text + note(dropped))
if focus:
    v = V.focus(p, img, focus)
    dest = os.path.join(CUA, "runs", f"focus_{focus}.png")
    v.image.save(dest)
    print(f"\n--- focus({focus!r}) -> {dest} {v.image.size}\n{v.text}")
