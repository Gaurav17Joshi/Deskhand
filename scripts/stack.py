"""Start, check and stop the local perception servers.

What StudioApprentice's GPU server runs as containers, run here as three
processes on the local GPU, each on the port and HTTP contract the perception
client expects (see ../perception.json):

    :8100  detect   L0 + L1 (scripts/serve_detect.py)
    :8300  ocr      PP-OCR, StudioApprentice's server/ocr/ocr_app.py, models from rapidocr
    :8400  icons    perception.serve_icons over ../icon-library

    .venv\\Scripts\\python scripts\\stack.py up        # start what is not running
    .venv\\Scripts\\python scripts\\stack.py status
    .venv\\Scripts\\python scripts\\stack.py down

Logs go to runs/stack/<name>.log. Everything binds to 127.0.0.1 only.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CUA = os.path.dirname(HERE)
PY = sys.executable
PERC = os.path.join(CUA, "repos", "StudioApprentice-perception")
LOGS = os.path.join(CUA, "runs", "stack")
PIDS = os.path.join(LOGS, "pids.json")
TOKEN = json.load(open(os.path.join(CUA, "perception.json")))["ocr_token"]


def _rapidocr_models():
    import rapidocr_onnxruntime
    return os.path.join(os.path.dirname(rapidocr_onnxruntime.__file__), "models")


def services():
    models = _rapidocr_models()
    return {
        "detect": {
            "port": 8100,
            "cmd": [PY, os.path.join(HERE, "serve_detect.py")],
            "env": {},
            "cwd": CUA,
        },
        "ocr": {
            "port": 8300,
            "cmd": [PY, "-m", "uvicorn", "ocr_app:app", "--host", "127.0.0.1",
                    "--port", "8300", "--log-level", "warning"],
            "env": {"OCR_TOKEN": TOKEN,
                    "OCR_MODEL_PATH": os.path.join(models, "ch_PP-OCRv4_rec_infer.onnx"),
                    "OCR_DET_MODEL_PATH": os.path.join(models, "ch_PP-OCRv4_det_infer.onnx"),
                    "OCR_CHARSET_PATH": "",
                    # keeps the spaces between words that ch-v4 drops; not in
                    # StudioApprentice's handoff, fetched from RapidOCR's model hub
                    "OCR_EN_MODEL_PATH": os.path.join(CUA, "models-ocr",
                                                      "en_PP-OCRv3_rec_infer.onnx"),
                    "OCR_GPU_MEM_LIMIT_MB": "2048", "SERVE_GPU": "0"},
            "cwd": os.path.join(PERC, "server", "ocr"),
            "auth": True,
        },
        "icons": {
            "port": 8400,
            "cmd": [PY, "-m", "uvicorn", "perception.serve_icons:app",
                    "--host", "127.0.0.1", "--port", "8400",
                    "--log-level", "warning"],
            "env": {"PERC_ICON_ROOT": os.path.join(CUA, "icon-library"),
                    "PERC_ICON_TOKEN": TOKEN, "PERC_ICON_GPUS": "0",
                    "PERC_ICON_PER_GPU": "1"},
            "cwd": PERC,
            "auth": True,
        },
    }


def health(name, svc, timeout=2):
    req = urllib.request.Request(f"http://127.0.0.1:{svc['port']}/health")
    if svc.get("auth"):
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:                              # noqa: BLE001
        return {"down": f"{type(e).__name__}: {e}"[:160]}


def _pids():
    try:
        return json.load(open(PIDS))
    except Exception:                                   # noqa: BLE001
        return {}


def up(only=None, wait=240):
    os.makedirs(LOGS, exist_ok=True)
    pids = _pids()
    svcs = services()
    started = []
    for name, svc in svcs.items():
        if only and name not in only:
            continue
        if "down" not in health(name, svc):
            print(f"{name:7s} already up on :{svc['port']}")
            continue
        env = {**os.environ, **svc["env"], "PYTHONUNBUFFERED": "1"}
        log = open(os.path.join(LOGS, f"{name}.log"), "ab")
        flags = 0x00000200 | 0x08000000      # new process group, no console
        p = subprocess.Popen(svc["cmd"], cwd=svc["cwd"], env=env, stdout=log,
                             stderr=subprocess.STDOUT, creationflags=flags)
        pids[name] = p.pid
        started.append(name)
        print(f"{name:7s} starting (pid {p.pid}), log runs/stack/{name}.log")
    json.dump(pids, open(PIDS, "w"))
    end = time.time() + wait
    for name in started:
        svc = svcs[name]
        while time.time() < end:
            h = health(name, svc)
            if "down" not in h:
                print(f"{name:7s} up: {json.dumps(h)[:200]}")
                break
            time.sleep(2)
        else:
            print(f"{name:7s} NOT up after {wait}s — see runs/stack/{name}.log")


def status():
    for name, svc in services().items():
        print(f"{name:7s} :{svc['port']}  {json.dumps(health(name, svc))[:220]}")


def down():
    for name, pid in _pids().items():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
        print(f"{name:7s} stopped (pid {pid})")
    if os.path.exists(PIDS):
        os.remove(PIDS)


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"up": lambda: up(sys.argv[2:] or None), "status": status,
     "down": down}[what]()
