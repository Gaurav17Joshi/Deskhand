"""Publish Deskhand's models to a Hugging Face model repo (maintainers only).

    .venv\\Scripts\\hf auth login                        # once, with a WRITE token
    .venv\\Scripts\\python scripts\\publish_models.py [--repo OWNER/NAME] [--private]
    .venv\\Scripts\\python scripts\\publish_models.py --stage-only   # build the folder, no upload

Stages, in runs/hf_stage/:

    weights/l0/, weights/l1/   checkpoint_best_ema.pth, registry_row.json, ddp_spec.json
    icon-library/<version>/    the version icon-library/live points at
    SHA256SUMS                 every file above
    README.md                  the model card

then creates the repo if needed and uploads the folder. fetch_models.py is the
other end.
"""
import argparse
import hashlib
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STAGE = os.path.join(ROOT, "runs", "hf_stage")

CARD = """---
tags:
- computer-use
- gui-agent
- object-detection
- rf-detr
- blender
---

# Deskhand models

The screen-reading models behind [Deskhand](https://github.com/Gaurav17Joshi/Deskhand),
a computer-use agent for Windows. They come from **StudioApprentice** (Kunal Gupta,
UC San Diego), and are published here with permission so that Deskhand can be set
up on any Windows PC with one command (`setup.ps1`, which runs `scripts/fetch_models.py`).

| file | what it is |
|---|---|
{rows}
| `icon-library/{icons}/` | hand-labelled toolbar icons ({n_icons} icons in {n_panels} panels) for Blender, Unreal Editor, DaVinci Resolve and others |
| `SHA256SUMS` | checksums; `fetch_models.py` checks every file against them |

## The detectors

Both are **RF-DETR small** (31.8M parameters), input {res} px, fine-tuned on
screenshots of creative desktop applications (Unreal Editor, Blender, DaVinci
Resolve, MetaHuman, web tools) and Windows itself.

- **L0** finds what changes how the screen can be used: `blocking` (a modal,
  menu or progress dialog the user must deal with first) and `passive`
  (tooltips, toasts, status text that cannot be clicked).
- **L1** finds the application's regions (`box`): every panel, toolbar, editor
  and window part a person can act on.

`registry_row.json` holds each model's training record (classes in `slot_names`
order, mAP, data size); `ddp_spec.json` its training settings. Load either with
the `rfdetr` package: `RFDETRSmall(pretrain_weights=..., resolution={res})`.
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stage():
    if os.path.exists(STAGE):
        shutil.rmtree(STAGE)
    rows = []
    res = 512
    for tier in ("l0", "l1"):
        src = os.path.join(ROOT, "weights", tier)
        dst = os.path.join(STAGE, "weights", tier)
        os.makedirs(dst)
        for f in ("checkpoint_best_ema.pth", "registry_row.json", "ddp_spec.json"):
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
        reg = json.load(open(os.path.join(src, "registry_row.json")))
        res = json.load(open(os.path.join(src, "ddp_spec.json"))).get("resolution", 512)
        rows.append(f"| `weights/{tier}/` | {tier.upper()} detector `{reg['model_id']}`: "
                    f"classes {reg['slot_names']}, mAP50 {reg['map50']:.3f}, trained on "
                    f"{reg['trained_on'].get('images', '?')} screenshots |")
    live = os.path.realpath(os.path.join(ROOT, "icon-library", "live"))
    version = os.path.basename(live)
    shutil.copytree(live, os.path.join(STAGE, "icon-library", version))
    lib = json.load(open(os.path.join(live, "library.json")))
    n_icons = len(lib.get("icons", {}))
    n_panels = len(lib.get("panels", {})) or len(os.listdir(os.path.join(live, "panels")))
    lines = []
    for dirpath, _, files in os.walk(STAGE):
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, STAGE).replace(os.sep, "/")
            lines.append(f"{sha256(p)}  {rel}")
    with open(os.path.join(STAGE, "SHA256SUMS"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(lines, key=lambda l: l[66:])) + "\n")
    with open(os.path.join(STAGE, "README.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(CARD.format(rows="\n".join(rows), icons=version, n_icons=n_icons,
                            n_panels=n_panels, res=res))
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(STAGE) for f in fs)
    print(f"staged {len(lines)} files, {size / 1e6:.0f} MB, in {STAGE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.environ.get("DESKHAND_MODELS_REPO",
                                                     "GauravJ17/deskhand-models"))
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--stage-only", action="store_true")
    a = ap.parse_args()
    stage()
    if a.stage_only:
        return
    from huggingface_hub import HfApi
    api = HfApi()
    who = api.whoami()["name"]
    print(f"logged in as {who}; uploading to https://huggingface.co/{a.repo}")
    api.create_repo(a.repo, repo_type="model", private=a.private, exist_ok=True)
    api.upload_folder(folder_path=STAGE, repo_id=a.repo, repo_type="model",
                      commit_message="Deskhand models: L0/L1 detectors and icon library")
    print(f"published: https://huggingface.co/{a.repo}")


if __name__ == "__main__":
    main()
