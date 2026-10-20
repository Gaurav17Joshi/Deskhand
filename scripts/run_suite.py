"""Run a list of tasks, each in a fresh Blender, and tabulate the outcomes.

    .venv\\Scripts\\python scripts\\run_suite.py [suite name] [--model M] [--only id,id]

Every task gets its own `cua_agent.py --launch --close` run (a new Blender
with factory settings, so tasks cannot leak into each other). The agent's own
verdict is recorded, but it is a claim: the after.png of each run is what to
check.

Sharing the machine: if DESKHAND_LOCK names a lock file, the suite only runs
while that file exists, and rewrites it before each task so it never looks
stale. Whoever else uses the desktop agrees to keep off while it is there.

Writes runs/suites/<time>-<suite>.json and prints a table.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CUA = os.path.dirname(HERE)
LOCK = os.environ.get("DESKHAND_LOCK")

SUITES = {
    "blender-basic": [
        ("sphere", "Add a UV sphere to the scene."),
        ("loc", "Set the default cube's X location to 2 m."),
        ("rename", "Rename the default cube to Box."),
        ("bevel", "Add a Bevel modifier to the default cube and set its Amount to 0.2 m."),
        ("cycles", "Set the render engine to Cycles."),
        ("top", "Change the 3D viewport to the top orthographic view."),
        ("frames", "Set the scene's end frame to 120."),
        ("delete", "Delete the default cube."),
    ],
}


def touch_lock(note):
    if not LOCK or not os.path.exists(LOCK):
        return
    end = time.strftime("%H:%M", time.localtime(time.time() + 20 * 60))
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write("# Desktop held by Deskhand\n\n"
                "- **What:** computer-use agent tests in their own app window "
                "(maximised). Real mouse/keyboard input, only into that process.\n"
                f"- **Now:** {note}\n"
                f"- **Updated:** {time.strftime('%Y-%m-%d %H:%M')}; expected end of "
                f"this stretch about {end}. Deleted when done.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("suite", nargs="?", default="blender-basic")
    ap.add_argument("--model", default="gpt-6-sol")
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--only", default="")
    ap.add_argument("--max-steps", type=int, default=40)
    a = ap.parse_args()
    tasks = SUITES[a.suite]
    if a.only:
        keep = set(a.only.split(","))
        tasks = [t for t in tasks if t[0] in keep]
    out_dir = os.path.join(CUA, "runs", "suites")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{a.suite}-{a.model}.json")
    os.makedirs(os.path.join(CUA, "runs", "agent"), exist_ok=True)
    rows = []
    for tid, task in tasks:
        if LOCK and not os.path.exists(LOCK):
            print(f"desktop lock {LOCK} is gone - stopping the suite")
            break
        sys.path.insert(0, HERE)
        from cua_agent import session_state
        if session_state() != "active":
            print(f"session is {session_state()} (RDP disconnected?) - stopping the suite")
            break
        touch_lock(f"suite {a.suite}, task {tid}")
        before = set(os.listdir(os.path.join(CUA, "runs", "agent")))
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.join(HERE, "cua_agent.py"), task,
                            "--launch", "--close", "--model", a.model,
                            "--effort", a.effort, "--max-steps", str(a.max_steps)],
                           cwd=CUA, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        new = sorted(set(os.listdir(os.path.join(CUA, "runs", "agent"))) - before)
        run_dir = os.path.join(CUA, "runs", "agent", new[-1]) if new else None
        res = {}
        if run_dir and os.path.exists(os.path.join(run_dir, "result.json")):
            res = json.load(open(os.path.join(run_dir, "result.json")))
        row = {"id": tid, "task": task, "run": run_dir,
               "status": (res.get("ended") or {}).get("status", f"crashed (exit {p.returncode})"),
               "reason": (res.get("ended") or {}).get("reason", p.stderr[-400:]),
               "steps": res.get("steps"), "seconds": res.get("seconds",
                                                          round(time.time() - t0, 1)),
               "tokens_in": (res.get("usage") or {}).get("input"),
               "tokens_out": (res.get("usage") or {}).get("output")}
        rows.append(row)
        json.dump(rows, open(out, "w"), indent=1)
        print(f"{tid:8s} {row['status']:9s} {row['steps']!s:>3} steps "
              f"{row['seconds']!s:>6}s  {str(row['reason'])[:110]}", flush=True)
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
