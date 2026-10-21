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
its children. `--launch calculator` and `--launch explorer` open one window
of an app Windows itself hosts, and input may only reach that window and its
own dialogs and menus. Clicks or keys aimed at any other window are refused.

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
.venv\Scripts\python scripts\cua_agent.py "Work out 1234 x 5678." --launch calculator --close
.venv\Scripts\python scripts\run_suite.py      # 8 Blender tasks, one fresh Blender each
.venv\Scripts\python scripts\run_suite.py apps # Calculator, File Explorer, 4 web apps
.venv\Scripts\python scripts\stack.py down
```

### Floating panel

Run `Deskhand.cmd`. It starts the perception servers and opens a small
always-on-top panel: pick what the agent works in (a fresh Blender, Chrome,
Calculator or File Explorer, or a window you click), type a task, Send. You
see its thinking and every step live, with the steps, time and cost so far,
and can **Pause**, **Resume** or **Stop** it; follow-up instructions continue
the same conversation.

- **Move, resize, minimise:** drag the header to move the panel and its
  corner to resize it. **Minimise** folds it into one row that still shows the
  status and the current step, with Pause and Stop.
- **Two mice:** while the agent works, the real cursor is its own (orange
  **agent** tag), and your mouse moves your own blue **you** pointer. Your clicks
  reach only the panel, and your typing goes into its box. Sending while it
  works steers the task. **Esc twice** pauses and hands the cursor back to you.
- The agent's cursor **glides** to each target along a slight curve, slow at
  both ends, instead of jumping there; a ring ripples out where it presses.
  `CUA_GLIDE=0` turns the glide off.
- A **yellow frame** surrounds the screen while the agent holds control.
- The panel, frame and pointer are hidden from screen capture, so the agent
  never sees them. If the agent clicks where the panel is, the click passes
  through it to the app while the panel fades. Use `--show-in-captures` to
  record a demo.

Scripts can drive the panel on `127.0.0.1:8777` (`/state`, `/task`, `/stop`,
`/pause`, `/resume`, `/mini`); `scripts/test_panel.py` tests it that way.

Each run writes `runs/agent/<time>-<task>/`: every tool call (`steps.jsonl`),
the pictures the model saw, before/after screenshots and `result.json`.

The session must be **active**: over RDP, keep the client window open, because
a minimised or disconnected session has no screen to see or click.

## Results so far

Every outcome below was checked by eye, not taken from the agent's word.

- **Blender:** 8/8 basic tasks (add a sphere, move, rename, bevel, render
  engine, view, end frame, delete) at about 85 s and 14 steps each.
- **Other apps:** 6/6.

| task | app | result | steps | time | cost |
|---|---|---|---|---|---|
| 1234 x 5678 | Calculator | 7,006,652 | 9 | 42 s | $0.019 |
| New folder Deskhand_test | File Explorer | created | 10 | 49 s | $0.020 |
| Year the Eiffel Tower was completed | Chrome, Wikipedia | 1889 | 20 | 124 s | $0.037 |
| Fill in and submit a pizza order form | Chrome, httpbin.org | all fields echoed back | 26 | 122 s | $0.052 |
| Rectangle with the text Deskhand | Chrome, Excalidraw | drawn | 22 | 112 s | $0.058 |
| Driving distance San Diego to Los Angeles | Chrome, Google Maps | 121 miles via I-5 N | 20 | 158 s | $0.041 |

- **Longer tasks:** saving a brown dog picture from Google Images (49 steps,
  7 min, $0.13); a labelled two-shape diagram with an arrow in Excalidraw
  (78 steps, 7.5 min, $0.39).

**Cost.** Every run records its tokens and cost in `result.json`. The agent
(`gpt-6-sol`, $2 / $0.20 cached / $10 per 1M tokens) is about 95% of it;
perception's area names (`gpt-5.6-luna`) are the rest. Most of the input is
cached, which makes runs 3-7x cheaper, but every picture stays in the
conversation, so long tasks cost more per step.
