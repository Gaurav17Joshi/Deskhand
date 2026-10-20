"""L0 + L1 detectors on this machine's GPU, behind StudioApprentice's HTTP contract.

`perception.detect.Detector` POSTs to `/infer/object_detection` with a
`model_id`; on StudioApprentice's GPU server that is the trainer container (`trainer_app.py`). This
serves the same route from one process holding both checkpoints, so the
perception code runs unchanged with its endpoints pointed at localhost.

Class names come from each model's registry row (`slot_names`), mapped exactly
as `trainer_app._format` does it.

    .venv\\Scripts\\python scripts\\serve_detect.py          # :8100
"""
import base64
import io
import json
import logging
import os
import threading
import time
import uuid

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS = os.environ.get("CUA_WEIGHTS", os.path.join(HERE, "..", "weights"))
PORT = int(os.environ.get("CUA_DETECT_PORT", "8100"))

log = logging.getLogger("serve_detect")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_models = {}          # model_id -> {"model", "entry", "lock"}


def _load(tier):
    d = os.path.join(WEIGHTS, tier)
    entry = json.load(open(os.path.join(d, "registry_row.json")))
    spec = json.load(open(os.path.join(d, "ddp_spec.json")))
    import rfdetr
    cls = {"rfdetr-nano": "RFDETRNano", "rfdetr-small": "RFDETRSmall",
           "rfdetr-medium": "RFDETRMedium", "rfdetr-base": "RFDETRBase",
           "rfdetr-large": "RFDETRLarge"}[entry["model_type"]]
    ckpt = os.path.join(d, os.path.basename(entry["checkpoint_path"]))
    t0 = time.time()
    m = getattr(rfdetr, cls)(pretrain_weights=ckpt,
                             resolution=int(spec.get("resolution", 512)))
    try:
        m.optimize_for_inference()
    except Exception as e:                          # not fatal, only slower
        log.warning("%s: optimize_for_inference failed: %s", tier, e)
    log.info("%s: %s loaded in %.1fs (%s)", tier, entry["model_id"],
             time.time() - t0, entry["slot_names"])
    _models[entry["model_id"]] = {"model": m, "entry": entry,
                                  "lock": threading.Lock()}


def _format(det, entry):
    """Same as trainer_app._format: class ids -> names through slot_names."""
    slot_names = entry.get("slot_names")
    xyxy = getattr(det, "xyxy", [])
    confs = getattr(det, "confidence", None)
    if confs is None:
        confs = [1.0] * len(xyxy)
    class_ids = getattr(det, "class_id", None)
    out = []
    for i, ((x1, y1, x2, y2), cf) in enumerate(zip(xyxy, confs)):
        cid = int(class_ids[i]) if class_ids is not None else 0
        if slot_names and 0 <= cid < len(slot_names):
            name = slot_names[cid]
        else:
            name = (entry.get("classes") or ["box"])[0]
        out.append({"x": float((x1 + x2) / 2), "y": float((y1 + y2) / 2),
                    "width": float(x2 - x1), "height": float(y2 - y1),
                    "confidence": float(cf), "class": name, "class_id": cid,
                    "detection_id": str(uuid.uuid4())})
    return out


app = FastAPI()


@app.on_event("startup")
def _startup():
    for tier in ("l0", "l1"):
        _load(tier)


@app.get("/health")
def health():
    import torch
    return {"status": "ok", "models": sorted(_models),
            "cuda": torch.cuda.is_available()}


@app.post("/infer/object_detection")
def infer(body: dict):
    t0 = time.perf_counter()
    got = _models.get(body.get("model_id"))
    if got is None:
        return JSONResponse({"message": f"unknown model_id {body.get('model_id')!r};"
                                        f" have {sorted(_models)}"}, status_code=404)
    try:
        img = Image.open(io.BytesIO(base64.b64decode(body["image"]["value"]))).convert("RGB")
    except Exception as e:                           # noqa: BLE001
        return JSONResponse({"message": f"bad image: {e}"}, status_code=400)
    conf = body.get("confidence")
    with got["lock"]:
        det = got["model"].predict(img, threshold=float(conf) if conf is not None else 0.0)
    w, h = img.size
    return {"inference_id": str(uuid.uuid4()), "time": time.perf_counter() - t0,
            "image": {"width": w, "height": h},
            "predictions": _format(det, got["entry"])}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
