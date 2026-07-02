#!/usr/bin/env python3
"""
Claude Semaphore — hook emitter.

Claude Code's hooks call this with the new state as the only argument
(WORKING / WAITING / DONE) and pipe the hook JSON on stdin. Instead of the
old single ~/.claude-semaphore/state file, each Claude session gets its own
file under ~/.claude-semaphore/sessions/<session_id>.json so the menu bar app
can show every project at once and, on "needs you", jump back to the right
terminal.

The record captures enough to focus that session's terminal later:
  - the working dir (→ project name shown in the menu)
  - the controlling tty (→ focus the exact tab in Terminal.app / iTerm2)
  - the terminal's bundle id / TERM_PROGRAM (→ activate the right app)

Only stdlib is used, so this stays portable. The expensive bits (finding the
tty, reading the terminal identity) are computed once per session and cached in
the record — later state flips just rewrite the state + timestamp.
"""
import json
import os
import subprocess
import sys
import time

APP_DIR = os.path.expanduser("~/.claude-semaphore")
SESSIONS_DIR = os.path.join(APP_DIR, "sessions")
LEGACY_STATE = os.path.join(APP_DIR, "state")

VALID = {"WORKING", "WAITING", "DONE"}


def find_tty():
    """Controlling terminal of the shell that launched us (walk up a few parents).

    Hooks are spawned as children of Claude Code, so our own stdin/out aren't the
    tty — but the process tree's controlling terminal is, and it's stable for the
    life of the session. Returns e.g. "/dev/ttys003", or "" if headless.
    """
    pid = os.getppid()
    for _ in range(5):
        try:
            out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=1).stdout.strip()
        except Exception:
            return ""
        if out and out not in ("??", "?"):
            return out if out.startswith("/dev/") else "/dev/" + out
        try:
            ppid = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=1).stdout.strip()
            pid = int(ppid)
        except Exception:
            return ""
        if pid <= 1:
            return ""
    return ""


def load_prev(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    state = sys.argv[1] if len(sys.argv) > 1 else "DONE"
    if state not in VALID:
        state = "DONE"

    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}

    sid = str(data.get("session_id") or "default")
    # keep the filename tame regardless of what the id contains
    safe_sid = "".join(c if c.isalnum() or c in "-_." else "_" for c in sid) or "default"
    cwd = data.get("cwd") or os.getcwd()

    os.makedirs(SESSIONS_DIR, exist_ok=True)
    path = os.path.join(SESSIONS_DIR, safe_sid + ".json")
    prev = load_prev(path)

    rec = {
        "state": state,
        "session_id": sid,
        "cwd": cwd,
        "project": os.path.basename(cwd.rstrip("/")) or cwd,
        # terminal identity + tty are stable per session → compute once, then reuse
        "tty": prev.get("tty") or find_tty(),
        "bundle_id": prev.get("bundle_id") or os.environ.get("__CFBundleIdentifier", ""),
        "term_program": prev.get("term_program") or os.environ.get("TERM_PROGRAM", ""),
        "updated": time.time(),
    }

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
    os.replace(tmp, path)  # atomic rename → bumps the dir vnode → app wakes on kqueue

    # Mirror to the legacy single-state file for any older consumer / smooth upgrade.
    try:
        with open(LEGACY_STATE, "w") as f:
            f.write(state)
    except Exception:
        pass


if __name__ == "__main__":
    main()
