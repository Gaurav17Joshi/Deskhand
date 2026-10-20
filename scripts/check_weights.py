"""Load the L0/L1 checkpoints and run them on one image.

    .venv\\Scripts\\python scripts\\check_weights.py [image] [--cpu]

Prints each tier's detections with class names (through the registry's
slot_names, as StudioApprentice's GPU server does) and writes <image>.boxes.png beside the
run dir, so a wrong class mapping or a model that finds nothing shows at once.
Default image: the Blender screenshot the perception self-test ships with.
"""
import json
import os
import sys
import time

if "--cpu" in sys.argv:
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
HERE = os.path.dirname(os.path.abspath(__file__))
CUA = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from PIL import Image, ImageDraw  # noqa: E402

import serve_detect as SD  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
img_path = args[0] if args else os.path.join(
    CUA, "repos", "StudioApprentice-perception", "perception", "selftest_data",
    "blender-shader-editor.jpg")
img = Image.open(img_path).convert("RGB")
print(f"image {img_path} {img.size}")
out = img.copy()
d = ImageDraw.Draw(out)
colour = {"blocking": (255, 138, 76), "passive": (179, 157, 245), "box": (60, 220, 60)}
for tier, conf in (("l0", 0.25), ("l1", 0.35)):
    SD._load(tier)
    model_id = [k for k, v in SD._models.items() if v["entry"]["level"] == tier][0]
    got = SD._models[model_id]
    t0 = time.time()
    det = got["model"].predict(img, threshold=conf)
    dt = time.time() - t0
    preds = SD._format(det, got["entry"])
    print(f"\n{tier} {model_id}: {len(preds)} boxes in {dt:.2f}s "
          f"(class ids seen: {sorted({p['class_id'] for p in preds})})")
    for p in sorted(preds, key=lambda p: -p["confidence"]):
        x0, y0 = p["x"] - p["width"] / 2, p["y"] - p["height"] / 2
        print(f"  {p['class']:9s} {p['confidence']:.2f}  x={x0:.0f} y={y0:.0f} "
              f"w={p['width']:.0f} h={p['height']:.0f}")
        d.rectangle([x0, y0, x0 + p["width"], y0 + p["height"]],
                    outline=colour.get(p["class"], (255, 255, 255)),
                    width=3 if tier == "l1" else 5)
dest = os.path.join(CUA, "runs", "check_weights.png")
os.makedirs(os.path.dirname(dest), exist_ok=True)
out.save(dest)
print(f"\nwrote {dest}")
