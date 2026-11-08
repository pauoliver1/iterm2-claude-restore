#!/usr/bin/env python3
"""Claude Code SessionStart hook: remember which Claude conversation is in which iTerm2 tab.

Claude Code passes JSON on stdin (session_id, cwd, source, ...). iTerm2 puts the tab's id in
ITERM_SESSION_ID (for example "w0t8p0:417C3B61-..."). We save one small file per tab, so the restore
script can look up "tab -> conversation" without reading any Claude Code internals.

Other programs that read Claude's settings (Grok, Codex) run this hook too, with the same tab id in their environment and a
session id of their own, and so does a headless `claude -p` run started from a terminal tab. None of those is the
conversation the tab shows, so they are ignored: we only record when the nearest agent above us is an ordinary Claude,
or when we cannot tell.

Prints nothing (stdout from a SessionStart hook is added to Claude's context) and always exits 0.
If something goes wrong it writes one line to hook-errors.log, so `restore.py --doctor` can tell you.
"""
import json
import os
import subprocess
import sys
import time

DIR = os.path.join(os.path.expanduser("~"), ".claude", "iterm-restore", "hooks")
ERRORS = os.path.join(os.path.dirname(DIR), "hook-errors.log")
AGENTS = {"claude", "grok", "codex"}
HEADLESS_FLAGS = {"-p", "--print"}


def is_headless(pid):
    """Was this Claude started as `claude -p` or `--print`, with no conversation shown in the terminal?"""
    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return False
    return any(token in HEADLESS_FLAGS for token in out.split()[1:])


def host_agent():
    """What is running this hook: claude, claude-headless, grok, codex, or None if we cannot tell.
    One ps call lists every process, then we walk up the parents in memory and take the nearest agent."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=3).stdout
        table = {}
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3:
                table[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
        pid = os.getppid()
        for _ in range(12):
            if pid not in table:
                return None
            parent, name = table[pid]
            if name in AGENTS:
                return "claude-headless" if name == "claude" and is_headless(pid) else name
            if parent <= 1:
                return None
            pid = parent
    except Exception:
        return None
    return None


def main():
    tab = os.environ.get("ITERM_SESSION_ID", "")
    if ":" not in tab:
        return
    tab_id = tab.split(":", 1)[1]
    if not tab_id or "/" in tab_id:
        return
    if host_agent() not in (None, "claude"):
        return
    data = json.load(sys.stdin)
    session_id = data.get("session_id")
    if not session_id:
        return
    os.makedirs(DIR, exist_ok=True)
    record = {
        "session_id": session_id,
        "cwd": data.get("cwd") or os.getcwd(),
        "source": data.get("source"),
        "saved_at": time.time(),
    }
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")  # set when this Claude runs on another account's config folder
    if config_dir:
        record["config_dir"] = os.path.realpath(os.path.expanduser(config_dir))
    tmp = os.path.join(DIR, "%s.%d.tmp" % (tab_id, os.getpid()))  # per process, so two hooks in one tab never share a temp file
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, os.path.join(DIR, tab_id + ".json"))


def note_error(error):
    try:
        os.makedirs(os.path.dirname(ERRORS), exist_ok=True)
        lines = []
        if os.path.exists(ERRORS) and os.path.getsize(ERRORS) > 20_000:
            with open(ERRORS) as f:
                lines = f.read().splitlines()[-50:]
        with open(ERRORS, "w" if lines else "a") as f:
            f.write("".join(line + "\n" for line in lines))
            f.write("%s %r\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), error))
    except Exception:
        pass


try:
    main()
except Exception as e:
    note_error(e)
sys.exit(0)
