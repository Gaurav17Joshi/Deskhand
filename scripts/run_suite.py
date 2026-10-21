"""Run a list of tasks, each in a fresh app, and tabulate the outcomes.

    .venv\\Scripts\\python scripts\\run_suite.py [blender-basic | apps] [--model M] [--only id,id]

Every task gets its own `cua_agent.py --launch <app> --close` run (a new
Blender with factory settings, a new Chrome, Calculator or File Explorer
window, so tasks cannot leak into each other). The agent's own verdict is
recorded, but it is a claim: the after.png of each run is what to check.

Sharing the machine: if DESKHAND_LOCK names a lock file, the suite only runs
while that file exists, and rewrites it before each task so it never looks
stale. Whoever else uses the desktop agrees to keep off while it is there.

Writes runs/suites/<time>-<suite>-<model>.json and prints a table with each
task's cost and the totals.
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
    # Not Blender: Windows' own apps (one window each) and web apps in the
    # agent's private Chrome. (id, task, app to launch)
    "apps": [
        ("calc", "Use the Calculator to work out 1234 x 5678, and report the result.",
         "calculator"),
        ("folder", "In this folder, create a new folder named Deskhand_test.", "explorer"),
        ("wiki", "On Wikipedia, find the year the Eiffel Tower was completed, and "
                 "report it.", "chrome"),
        ("form", "Open https://httpbin.org/forms/post and fill in the pizza order form: "
                 "customer name Deskhand, telephone 555-0100, e-mail test@example.com, "
                 "size Medium, toppings Bacon and Onion, preferred delivery time 19:30. "
                 "Submit the order, and report what the page shows after submitting.",
         "chrome"),
        ("draw", "Open https://excalidraw.com and draw a rectangle with the text "
                 "Deskhand inside it.", "chrome"),
        ("maps", "On Google Maps, find the driving distance from San Diego to Los "
                 "Angeles, and report it.", "chrome"),
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
    for tid, task, *rest in tasks:
        kind = rest[0] if rest else "blender"
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
                            "--launch", kind, "--close", "--model", a.model,
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
               "tokens_cached": (res.get("usage") or {}).get("cached"),
               "tokens_out": (res.get("usage") or {}).get("output"),
               "perception_usage": res.get("perception_usage"),
               "cost_usd": res.get("cost_usd")}
        rows.append(row)
        json.dump(rows, open(out, "w"), indent=1)
        c = (row["cost_usd"] or {}).get("total")
        print(f"{tid:8s} {row['status']:9s} {row['steps']!s:>3} steps "
              f"{row['seconds']!s:>6}s  ${c if c is not None else 0:.3f}  "
              f"{str(row['reason'])[:100]}", flush=True)
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd")]
    if costs:
        print(f"\ntotal ${sum(c['total'] for c in costs):.3f} "
              f"(agent ${sum(c['agent'] or 0 for c in costs):.3f}, perception "
              f"${sum(c['perception'] for c in costs):.3f}) over {len(costs)} tasks")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
