# Deskhand

A computer-use agent that works the Windows machine it runs on. Give it a task
in plain words ("Add a UV sphere to the scene", "save a picture of a brown dog
from Google Images into this folder") and it looks at the screen, clicks and
types until the task is done.

## Demo

**Video:** [the agent downloads VS Code and installs extensions on it, in about
10 minutes](https://drive.google.com/file/d/1jukmMj0_ECdQMZ_0fl48cMe23N8UBxRk/view)

![An agent run in Blender: the agent saved the modelled chair scene as chair1.blend in 15 steps](assets/chair_blender_ex.png)

*An agent run in Blender, in StudioApprentice's Studio app: asked to save the
open chair scene under a new name, the agent saved it as `chair1.blend` in 15
steps without changing the scene.*

## How it works

It is built on [StudioApprentice](https://github.com/KunalMGupta) perception
and tools:

```
screenshot
  -> perception: L0/L1 detectors (RF-DETR), OCR (PP-OCR), icon matching
                 run as three local servers, on the GPU if there is one (scripts/stack.py)
  -> areas and region names: an OpenAI model (StudioApprentice's groups.py / view.py)
  -> tools: screen / focus / viewport / click / drag / scroll / type_text / key
            (studio's mcp_server, run in-process)
  -> brain: OpenAI Responses API, gpt-6-sol by default (scripts/cua_agent.py)
  -> input: real Windows mouse and keyboard events
```

**Safety:** with `--launch`, the agent starts its own Blender or Chrome
(Chrome with a throwaway profile), and input may only reach that process and
its children. Clicks or keys aimed at any other window are refused.

## Setup (any Windows 10/11 PC)

You need [Python 3.12](https://www.python.org/downloads/),
[git](https://git-scm.com/download/win), an OpenAI API key and about 8 GB of disk
(mostly PyTorch).
An NVIDIA GPU is optional: without one, the screen reading runs on the CPU,
several times slower. Then, in this folder:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

It creates `.venv` (PyTorch for your GPU, or the CPU build), installs the
packages, clones StudioApprentice perception at the tested commit into
`repos/`, downloads the models from
[GauravJ17/deskhand-models](https://huggingface.co/GauravJ17/deskhand-models)
(checking every file's checksum), asks for your OpenAI key (kept in `.env`,
which git ignores), and checks that the models load. Run it again any time;
it skips what is done. Then start Deskhand with **`Deskhand.cmd`**.

Blender and Chrome are found wherever they are installed. To use a different
copy, set `DESKHAND_BLENDER` or `DESKHAND_CHROME` to its `.exe`. Any display
scaling and several monitors are fine. Remote Desktop works as long as the
RDP window stays open: a minimised or disconnected session has no screen.

What lives outside git: `weights/`, `icon-library/` and `models-ocr/` (from
Hugging Face), `repos/` (from GitHub), `.venv/`, `runs/` and `.env`.
`vendor/studio/` holds the three StudioApprentice studio files Deskhand uses
(see `vendor/README.md`).

## Use

```powershell
.venv\Scripts\python scripts\stack.py up       # start the perception servers
.venv\Scripts\python scripts\cua_agent.py "Add a UV sphere to the scene." --launch blender --close
.venv\Scripts\python scripts\cua_agent.py "Search Google Images for dog and save a brown dog to C:\tmp as dog" --launch chrome --close
.venv\Scripts\python scripts\run_suite.py      # 8 Blender tasks, one fresh Blender each
.venv\Scripts\python scripts\stack.py down
```

### Floating panel

Run `Deskhand.cmd`. It starts the perception servers and opens a small
always-on-top panel: pick what the agent works in (a fresh Blender, a fresh
Chrome, or a window you click), type a task, Send. You see its thinking and
every step live, and can **Pause**, **Resume** or **Stop** it; follow-up
instructions continue the same conversation.

- **Two mice:** while the agent works, the real cursor is its own (orange
  **agent** tag), and your mouse moves your own blue **you** pointer. Your clicks
  reach only the panel, and your typing goes into its box. Sending while it
  works steers the task. **Esc twice** pauses and hands the cursor back to you.
- A **yellow frame** surrounds the screen while the agent holds control.
- The panel, frame and pointer are hidden from screen capture, so the agent
  never sees them. Use `--show-in-captures` to record a demo.

Scripts can drive the panel on `127.0.0.1:8777` (`/state`, `/task`, `/stop`,
`/pause`, `/resume`); `scripts/test_panel.py` tests it that way.

Each run writes `runs/agent/<time>-<task>/`: every tool call (`steps.jsonl`),
the pictures the model saw, before/after screenshots and `result.json`.

The session must be **active**: over RDP, keep the client window open, because
a minimised or disconnected session has no screen to see or click.

## Results so far

8/8 basic Blender tasks (each checked by eye) at about 85 s and 14 steps
each, and one Chrome download task (49 steps, 7 min). Long tasks are still
expensive, because every picture stays in the conversation.
