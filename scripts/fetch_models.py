"""Download Deskhand's models into this folder.

    .venv\\Scripts\\python scripts\\fetch_models.py [--repo OWNER/NAME] [--from-dir DIR]

From the Hugging Face model repo ($DESKHAND_MODELS_REPO, default
Gaurav17Joshi/deskhand-models), laid out as publish_models.py writes it:

    weights/l0/, weights/l1/   checkpoint_best_ema.pth, registry_row.json, ddp_spec.json
    icon-library/<version>/    the icon library
    SHA256SUMS                 every file above

and from RapidOCR's public repo the English recogniser, into models-ocr/.
Every file is checked against SHA256SUMS before it is put in place. The
icon library lands in icon-library/versions/<version>, with icon-library/live
a junction to it, as the icon server expects.

--from-dir copies from a local folder with the same layout instead (offline
machines, or testing before a publish).
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_REPO = os.environ.get("DESKHAND_MODELS_REPO", "Gaurav17Joshi/deskhand-models")
OCR_REPO, OCR_FILE = "SWHL/RapidOCR", "PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(src):
    sums = os.path.join(src, "SHA256SUMS")
    if not os.path.exists(sums):
        raise SystemExit(f"no SHA256SUMS in {src}")
    bad, n = [], 0
    for line in open(sums, encoding="utf-8"):
        want, _, rel = line.strip().partition("  ")
        if not rel:
            continue
        p = os.path.join(src, *rel.split("/"))
        if not os.path.exists(p) or sha256(p) != want:
            bad.append(rel)
        n += 1
    if bad:
        raise SystemExit(f"{len(bad)} of {n} files failed their checksum: {bad[:5]}")
    print(f"checksums ok ({n} files)")


def junction(link, target):
    if os.path.lexists(link):
        if os.path.isdir(link) and not os.path.islink(link) and not _is_junction(link):
            raise SystemExit(f"{link} exists and is a real folder; move it away first")
        os.rmdir(link)                                # removes the junction only
    subprocess.run(["cmd", "/c", "mklink", "/J", link, target], check=True,
                   capture_output=True)


def _is_junction(p):
    try:
        return bool(os.readlink(p))
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--from-dir", default=None)
    a = ap.parse_args()

    if a.from_dir:
        src = os.path.abspath(a.from_dir)
        print(f"models from {src}")
    else:
        from huggingface_hub import snapshot_download
        print(f"models from https://huggingface.co/{a.repo}")
        src = snapshot_download(a.repo, repo_type="model",
                                local_dir=os.path.join(ROOT, "runs", "hf_models"))
    verify(src)

    for tier in ("l0", "l1"):
        dst = os.path.join(ROOT, "weights", tier)
        os.makedirs(dst, exist_ok=True)
        for f in os.listdir(os.path.join(src, "weights", tier)):
            shutil.copy2(os.path.join(src, "weights", tier, f), os.path.join(dst, f))
    print("weights -> weights/l0, weights/l1")

    libs = sorted(os.listdir(os.path.join(src, "icon-library")))
    version = libs[-1]
    vdir = os.path.join(ROOT, "icon-library", "versions", version)
    if os.path.exists(vdir):
        shutil.rmtree(vdir)
    shutil.copytree(os.path.join(src, "icon-library", version), vdir)
    junction(os.path.join(ROOT, "icon-library", "live"), vdir)
    print(f"icon library {version} -> icon-library/versions/{version} (live)")

    ocr = os.path.join(ROOT, "models-ocr", os.path.basename(OCR_FILE))
    if not os.path.exists(ocr):
        from huggingface_hub import hf_hub_download
        got = hf_hub_download(OCR_REPO, OCR_FILE,
                              local_dir=os.path.join(ROOT, "runs", "hf_ocr"))
        os.makedirs(os.path.dirname(ocr), exist_ok=True)
        shutil.copy2(got, ocr)
    print(f"English OCR model -> models-ocr/{os.path.basename(OCR_FILE)}")
    print("models ready")


if __name__ == "__main__":
    sys.exit(main())
