#!/usr/bin/env python3
"""Keep a snapshot of the iTerm2 layout and Claude session ids, and bring it back on launch.

While iTerm2 runs: every INTERVAL seconds, record every window, tab and split pane,
with the folder and (for Claude panes) the Claude session id. Claude Code's own session file for
the Claude running in the tab is the authority for the id. The SessionStart hook (hook.py) supplies the
folder, and is used on its own only when that file is missing. If the two disagree the live file wins and a
WARNING is raised. A Codex pane gets the id from its `codex resume <id>` launch command, otherwise
the newest Codex session with real content in its folder (searching the 30 most recent days that have Codex sessions, at most 300 files).

When iTerm2 starts: wait until the windows stop changing, back up the saved layout if there is one, then
  - if macOS already restored the same layout, type `cd <folder>` and, for Claude and Codex panes,
    `claude --resume <id>` or `codex resume <id>` into each pane that is sitting at a shell prompt
  - if iTerm2 opened a single blank shell window, rebuild the windows, tabs and splits first, then do the same
  - anything else: change nothing, log it, and show a notification
A pane running anything other than a shell is never typed into.

If an update to Claude Code, Codex or iTerm2 stops the script from finding conversation ids, it says so in
restore.log and with a notification instead of failing silently. `--doctor` checks for the same problems on demand.

Flags (run from a terminal, not needed in normal use):
  --once      write one snapshot now and exit
  --dry-run   print what a restore would do, without typing anything
  --selftest  open a throwaway window with splits, check the layout logic, close it
  --doctor    check the setup and the files this script depends on. Read only, works with any Python 3,
              needs no iTerm2 connection
"""
import asyncio
import calendar
import datetime
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

try:
    import iterm2
except ImportError:  # --doctor still works without it
    iterm2 = None

HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, ".claude", "iterm-restore")
STATE = os.environ.get("ITERM_RESTORE_STATE") or DIR  # optional: lets tests keep the snapshot, backups and log elsewhere
HOOKS = os.path.join(DIR, "hooks")
HOOK_ERRORS = os.path.join(DIR, "hook-errors.log")
SNAP = os.path.join(STATE, "layout.json")
BACKUPS = os.path.join(STATE, "backups")
LOG = os.path.join(STATE, "restore.log")
SESSIONS = os.path.join(HOME, ".claude", "sessions")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")

SNAPSHOT_VERSION = 1
INTERVAL = 30
BACKUP_EVERY = 600
KEEP_AUTO_BACKUPS = 10  # periodic copies
KEEP_SAFE_BACKUPS = 10  # copies made at launch or before a shrinking save, pruned separately so periodic ones never push them out
LOG_MAX_BYTES = 200_000
LOG_KEEP_LINES = 500
HOOK_MAX_DAYS = 30
PRUNE_EVERY = 86400  # seconds between hook record cleanups while iTerm2 stays open
UNKNOWN_ROUNDS = 3  # snapshot rounds a Claude tab may have no conversation id before we raise the alarm
START_TOLERANCE = 5  # seconds of slack when matching a process start time
CODEX_DAYS_SCANNED = 30  # only the newest day folders under ~/.codex/sessions are searched
CODEX_MAX_FILES = 300  # and at most this many session files, so the scan cost and the caches have a ceiling
TMP_MAX_AGE = 86400  # leftover layout.json temp files older than this are removed
PS_TIMEOUT = 10  # seconds: a ps that hangs must never stall the snapshot loop
STALE_HOOK_GRACE = 60  # a Claude younger than this may not have written its hook record yet
MIN_CODEX_LINES = 6  # Codex writes a 2-line stub the moment it launches; real conversations are longer
SHELLS = {"zsh", "-zsh", "bash", "-bash", "fish", "-fish", "sh", "-sh"}
SHELL_NAMES = {s.lstrip("-") for s in SHELLS}
STARTING_UP = {None, "", "login"}  # what a brand new pane reports for a moment before its shell takes over
SAFE_ID = re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{7,63}")  # starts with a letter or digit, so it can never read as a flag
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

DRY = "--dry-run" in sys.argv
ONCE = "--once" in sys.argv
SELFTEST = "--selftest" in sys.argv
DOCTOR = "--doctor" in sys.argv

STARTED = {"restored": False}  # restore only once per iTerm2 process, even if the API connection is re-made
_codex_first_lines = {}
_codex_line_counts = {}
_complained = set()


def trim_log():
    """Keep restore.log from growing forever: past LOG_MAX_BYTES, keep only the last LOG_KEEP_LINES lines."""
    try:
        if os.path.getsize(LOG) > LOG_MAX_BYTES:
            with open(LOG) as f:
                lines = f.readlines()[-LOG_KEEP_LINES:]
            tmp = LOG + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(lines)
            os.replace(tmp, LOG)
    except OSError:
        pass


def log(msg):
    if DOCTOR:  # the doctor only reads: it never writes the log
        return
    line = "%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), CONTROL_CHARS.sub(" ", str(msg)))  # a tab title cannot forge a log line
    try:
        os.makedirs(STATE, exist_ok=True)
        trim_log()
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass  # a full disk must not stop the restore or the snapshot loop
    if DRY or ONCE or SELFTEST:
        print(line)


def complain_once(key, msg):
    """Log a problem once per version of the thing that is wrong, not every 30 seconds."""
    if len(_complained) > 1000:
        _complained.clear()
    if key not in _complained:
        _complained.add(key)
        log(msg)


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def recent_line(line, hours):
    """Does this log line start with a timestamp from the last `hours` hours? Lines we cannot date count as recent."""
    try:
        return time.time() - time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S")) < hours * 3600
    except ValueError:
        return True


def notify(msg):
    if DOCTOR:
        return
    subprocess.run(
        ["osascript", "-e", 'display notification %s with title "iTerm2 restore"' % json.dumps(msg)],
        capture_output=True,
    )


def ps_output(args):
    """Run ps and return its output, or an empty string if it hangs or fails."""
    try:
        return subprocess.run(["ps"] + args, capture_output=True, text=True, timeout=PS_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        complain_once(("ps-failed", " ".join(args)), "ps did not answer (%s), so process information is missing this round" % " ".join(args))
        return ""


def alarm(key, msg):
    """Say it loudly, once: a WARNING line in restore.log and a notification."""
    if key not in _complained:
        complain_once(key, "WARNING: " + msg)
        notify(msg + ". See restore.log")


def safe_id(value):
    """A session id we are willing to type: letters, digits, dash and underscore, never starting with a dash."""
    return isinstance(value, str) and SAFE_ID.fullmatch(value) is not None


def safe_folder(value):
    """A folder we are willing to type: an absolute path with no control characters (Ctrl-C, Esc and the like)."""
    return isinstance(value, str) and value.startswith("/") and not CONTROL_CHARS.search(value)


# ---------- reading the machine ----------

def ps_rows(column):
    """Yield (terminal, text) for each process attached to a terminal. `column` is a ps field: comm or command."""
    out = ps_output(["-axo", "tty=,%s=" % column])
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] != "??":
            yield "/dev/" + parts[0], parts[1].strip()


def tty_commands():
    """Map terminal to the set of program names running on it."""
    found = {}
    for tty, text in ps_rows("comm"):
        found.setdefault(tty, set()).add(os.path.basename(text))
    return found


def codex_resume_ids():
    """Map terminal to the session id in a `codex resume <id>` launch command, when there is one."""
    found = {}
    for tty, text in ps_rows("command"):
        m = re.search(r"/codex\s+resume\s+([0-9a-f]{8}-[0-9a-f-]{27})", text)
        if m:
            found[tty] = m.group(1)
    return found


def parse_ps_time(text):
    """Seconds since 1970 for a time like 'Fri Oct 30 17:19:47 2026' read as if it were UTC, or None."""
    try:
        return calendar.timegm(time.strptime(" ".join(text.split()), "%a %b %d %H:%M:%S %Y"))
    except (ValueError, TypeError, AttributeError):
        return None


def start_matches(recorded, from_ps):
    """Is this still the process Claude recorded, or has its pid been reused by something else?
    Claude records its start time in UTC while ps prints local time, so allow any whole number of quarter hours
    between the two. A start time that is missing or cannot be read counts as a match, so a format change in an
    update can not make every conversation look dead."""
    a, b = parse_ps_time(recorded), parse_ps_time(from_ps)
    if a is None or b is None:
        return True
    gap = abs(a - b) % 900
    return min(gap, 900 - gap) <= START_TOLERANCE


def live_claude_sessions():
    """Every live Claude process known to Claude Code's own session files: [{pid, sessionId, cwd, kind, tty, started}].
    Claude names each file after its process id, so we open <pid>.json for each running process. The cost follows the
    number of processes, never the number of old session files. If Claude Code ever stops naming files that way,
    nothing is found here, and the hook (and --doctor) carry on."""
    pid_info = {}
    out = ps_output(["-axo", "pid=,tty=,lstart="])
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3:
            pid_info[parts[0]] = (parts[1], parts[2])
    found = []
    for pid, info in pid_info.items():
        path = os.path.join(SESSIONS, pid + ".json")
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        if not d.get("sessionId"):
            continue
        if d.get("procStart") is not None and parse_ps_time(d["procStart"]) is None:
            alarm(("procstart-format",), "Claude Code changed how it writes procStart (%r), so a reused process id can no longer be told apart from a live Claude" % (str(d["procStart"])[:40],))
        if parse_ps_time(info[1]) is None:
            alarm(("lstart-format",), "ps printed a start time this script cannot read (%r), so the process start check is off" % (info[1][:40],))
        if not start_matches(d.get("procStart"), info[1]):
            continue
        started = d.get("startedAt")
        found.append({"pid": int(pid), "sessionId": d["sessionId"], "cwd": d.get("cwd"), "kind": d.get("kind"),
                      "tty": None if info[0] == "??" else "/dev/" + info[0],
                      "started": started / 1000 if isinstance(started, (int, float)) else None})
    return found


def live_session_ids():
    return {s["sessionId"] for s in live_claude_sessions()}


def claude_by_tty():
    """Map terminal (e.g. /dev/ttys003) to the live interactive Claude session on it. If two share a terminal, the
    one that started last wins."""
    sessions = sorted((s for s in live_claude_sessions() if s["kind"] == "interactive" and s["tty"]), key=lambda s: s["started"] or 0)
    return {s["tty"]: {"sessionId": s["sessionId"], "cwd": s["cwd"], "started": s["started"]} for s in sessions}


def transcript_exists(session_id):
    """Does Claude have a conversation file for this id? (~/.claude/projects/<folder>/<id>.jsonl)"""
    return safe_id(session_id) and bool(glob.glob(os.path.join(CLAUDE_PROJECTS, "*", session_id + ".jsonl")))


def transcripts_findable():
    """Can we find any Claude conversation file at all? If not (Claude moved them), a missing file means nothing.
    Stops at the first one found and looks at no more than 200 project folders."""
    for folder in list_dir_sorted(CLAUDE_PROJECTS)[:200]:
        try:
            with os.scandir(os.path.join(CLAUDE_PROJECTS, folder)) as entries:
                if any(e.name.endswith(".jsonl") for e in entries):
                    return True
        except OSError:
            continue
    return False


def known_missing(session_id):
    """True only when Claude's conversation files are findable and there is none for this id."""
    return transcripts_findable() and not transcript_exists(session_id)


def tab_of_pid(pid):
    """The iTerm2 tab id (the part of ITERM_SESSION_ID after the colon) in a process's environment, or ''. Only that
    one variable is read, nothing else from the environment is kept or shown."""
    out = ps_output(["eww", "-p", str(pid)])
    m = re.search(r"ITERM_SESSION_ID=\S*?:(\S+)", out)
    return m.group(1) if m else ""


def hook_record(tab_id):
    """What the SessionStart hook saved for this iTerm2 tab, or None."""
    path = os.path.join(HOOKS, tab_id + ".json")
    try:
        with open(path) as f:
            d = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        complain_once((path, mtime(path)), "hook record unreadable, ignoring it: %s (%r)" % (path, e))
        return None
    if not isinstance(d, dict) or not isinstance(d.get("session_id"), str) or not d["session_id"]:
        complain_once((path, mtime(path)), "hook record has the wrong shape, ignoring it: %s" % path)
        return None
    return d


def list_dir_sorted(path):
    """Every entry name in a folder, sorted. An empty list if the folder cannot be read."""
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def listdir_numbers(path):
    try:
        return sorted((n for n in os.listdir(path) if n.isdigit()), reverse=True)
    except OSError:
        return []


def newest_day_folders(limit):
    """The newest `limit` YYYY/MM/DD folders under ~/.codex/sessions. Only the newest year, month and day names are
    listed, so the cost does not grow with the history."""
    found = []
    for year in listdir_numbers(CODEX_SESSIONS):
        for month in listdir_numbers(os.path.join(CODEX_SESSIONS, year)):
            for day in listdir_numbers(os.path.join(CODEX_SESSIONS, year, month)):
                found.append(os.path.join(CODEX_SESSIONS, year, month, day))
                if len(found) >= limit:
                    return found
    return found


def recent_codex_files():
    """The newest Codex session files: from the CODEX_DAYS_SCANNED most recent day folders, at most CODEX_MAX_FILES.
    File names start with their creation time, so the newest are picked by name and only those are stat'ed."""
    files = [f for d in newest_day_folders(CODEX_DAYS_SCANNED) for f in glob.glob(os.path.join(d, "rollout-*.jsonl"))]
    files = sorted(files, key=os.path.basename, reverse=True)[:CODEX_MAX_FILES]
    return sorted(files, key=mtime, reverse=True)


def newest_codex_sessions(folders):
    """For each folder, the newest interactive Codex session with real content: {folder: session id}.
    Only the CODEX_DAYS_SCANNED most recent day folders and at most CODEX_MAX_FILES files are searched, so a session
    in an older folder is only found when the tab was launched with `codex resume <id>`. The caches never hold more
    than the files searched."""
    files = recent_codex_files()
    keep = set(files)
    for cache in (_codex_first_lines, _codex_line_counts):
        for gone in [p for p in cache if p not in keep]:
            del cache[gone]
    wanted = set(folders)
    found = {}
    for path in files:
        if not wanted:
            break
        if path not in _codex_first_lines:
            try:
                with open(path) as f:
                    meta = json.loads(f.readline()).get("payload", {})
                _codex_first_lines[path] = (meta.get("id"), meta.get("cwd"), meta.get("originator"))
            except Exception:
                _codex_first_lines[path] = (None, None, None)
        sid, cwd, originator = _codex_first_lines[path]
        if not sid or originator != "codex-tui" or cwd not in wanted:
            continue
        stamp = mtime(path)
        cached = _codex_line_counts.get(path)
        if not cached or cached[0] != stamp:  # one entry per file, replaced when the file changes
            try:
                with open(path) as f:
                    _codex_line_counts[path] = (stamp, sum(1 for _ in f))
            except (OSError, ValueError):
                _codex_line_counts[path] = (stamp, 0)
        if _codex_line_counts[path][1] >= MIN_CODEX_LINES:
            found[cwd] = sid
            wanted.discard(cwd)
    return found


def attach_codex(layout):
    """Give each Codex pane that has no session id yet the newest interactive session in its folder.
    Panes whose id came from their launch command are left alone.
    If two such panes share a folder we cannot tell them apart, so we do not guess."""
    codex_panes = [p for w in layout["windows"] for t in w["tabs"] for p in panes(t["root"]) if p.get("is_codex") and not p.get("codex")]
    if not codex_panes:
        return
    per_folder = {}
    for p in codex_panes:
        per_folder.setdefault(p.get("cwd"), []).append(p)
    newest = newest_codex_sessions([f for f, group in per_folder.items() if len(group) == 1])
    for folder, group in per_folder.items():
        if len(group) > 1:
            for p in group:
                p["codex"] = {"sessionId": None, "note": "several Codex tabs share this folder"}
        elif folder in newest:
            group[0]["codex"] = {"sessionId": newest[folder], "via": "newest session in folder"}


# ---------- capturing the layout ----------

def pick_claude(rec, fallback, tab_id):
    """Which Claude conversation is in this tab. rec is the hook record, fallback is Claude's own live session file for
    the interactive process on this terminal, either may be None.
    The live file is the authority for a running tab: the hook record is keyed by the tab, so any other program or
    second Claude run in the same tab (Grok, Codex, a headless `claude -p`) could have written it. Same id: use the
    hook record, it also has the folder. Different ids: use the live file and raise an alarm, unless that Claude is so
    young that its own hook has not fired yet. No live file: the hook record is all there is."""
    if not fallback:
        if not rec:
            return None
        return {"sessionId": rec["session_id"], "cwd": rec.get("cwd"), "via": "hook"}
    if not rec:
        return dict(fallback, via="files")
    if fallback["sessionId"] == rec["session_id"]:
        return {"sessionId": rec["session_id"], "cwd": rec.get("cwd") or fallback.get("cwd"), "via": "hook"}
    started = fallback.get("started")
    key = ("hook-disagrees", tab_id, rec["session_id"])
    if key not in _complained and not (started and time.time() - started <= STALE_HOOK_GRACE):
        reason = ("names a conversation Claude has no file for (another program such as Grok or Codex may have written it)"
                  if known_missing(rec["session_id"]) else
                  "differs from the Claude running in that tab (a second Claude run in the same tab may have written it)")
        alarm(key, "a tab's hook record %s. Using the id from Claude's own session file" % reason)
    return dict(fallback, via="files (hook record disagrees)")


async def capture_node(node, ctx):
    if isinstance(node, iterm2.Session):
        tty = await node.async_get_variable("tty")
        cwd = await node.async_get_variable("path")
        job = await node.async_get_variable("jobName")
        entry = {"type": "pane", "tty": tty, "name": node.name, "cwd": cwd, "job": job, "iterm_id": node.session_id}
        if not tty or not cwd:
            entry["blind"] = True  # iTerm2 gave no terminal or folder: fine for a new pane, a problem if it lasts
        cmds = ctx.get("cmds", {}).get(tty, set())
        fallback = ctx.get("claude", {}).get(tty)
        if "claude" in cmds or fallback:
            claude = pick_claude(hook_record(node.session_id), fallback, node.session_id)
            if claude:
                entry["claude"] = claude
            else:
                entry["claude_unknown"] = True  # Claude is running here but neither source knows which conversation
        elif "codex" in cmds:
            entry["is_codex"] = True
            launched_with = ctx.get("codex_ids", {}).get(tty)
            if launched_with:
                entry["codex"] = {"sessionId": launched_with, "via": "launch command"}
        return entry
    children = [await capture_node(c, ctx) for c in node.children]
    if len(children) == 1:
        return children[0]
    return {"type": "split", "vertical": node.vertical, "children": children}


async def capture(app):
    ctx = {"claude": claude_by_tty(), "cmds": tty_commands(), "codex_ids": codex_resume_ids()}
    windows = []
    for w in app.terminal_windows:
        tabs = []
        for t in w.tabs:
            tabs.append({"root": await capture_node(t.root, ctx)})
        windows.append({"tabs": tabs})
    layout = {"windows": windows}
    attach_codex(layout)
    return layout


def panes(node):
    if node["type"] == "pane":
        yield node
    else:
        for c in node["children"]:
            yield from panes(c)


def all_panes(layout):
    return [p for w in layout["windows"] for t in w["tabs"] for p in panes(t["root"])]


def resumable_count(layout):
    """Panes a restore could resume: a Claude pane, or a Codex pane with a known session id."""
    return sum(1 for p in all_panes(layout) if "claude" in p or (p.get("codex") or {}).get("sessionId"))


def pane_total(layout):
    return len(all_panes(layout))


def shrank(new, old):
    """Does `new` have fewer resumable panes or fewer panes in total than `old`? A closing window looks like this."""
    return resumable_count(new) < resumable_count(old) or pane_total(new) < pane_total(old)


def shape(node):
    if node["type"] == "pane":
        return "p"
    return ["s", bool(node["vertical"]), [shape(c) for c in node["children"]]]


def layout_shape(layout):
    return [[shape(t["root"]) for t in w["tabs"]] for w in layout["windows"]]


def is_blank_window(layout):
    """One window with one tab with one pane that is just sitting at a shell prompt."""
    windows = layout["windows"]
    if len(windows) != 1 or len(windows[0]["tabs"]) != 1:
        return False
    root = windows[0]["tabs"][0]["root"]
    return root["type"] == "pane" and root.get("job") in SHELLS and "claude" not in root and not root.get("is_codex")


# ---------- the saved snapshot ----------

def valid_node(node):
    if not isinstance(node, dict):
        return False
    if any(key in node and not isinstance(node[key], dict) for key in ("claude", "codex")):
        return False
    if node.get("type") == "pane":
        return True
    children = node.get("children")
    return (node.get("type") == "split" and isinstance(node.get("vertical"), bool)
            and isinstance(children, list) and bool(children) and all(valid_node(c) for c in children))


def valid_layout(d):
    return (isinstance(d, dict) and isinstance(d.get("windows"), list)
            and all(isinstance(w, dict) and isinstance(w.get("tabs"), list)
                    and all(isinstance(t, dict) and valid_node(t.get("root")) for t in w["tabs"]) for w in d["windows"]))


def load_snapshot():
    """(layout, problem) without logging. problem is None when the file is fine or simply missing, otherwise
    (kind, text) where kind is unreadable (maybe a passing glitch) or invalid (corrupt, wrong shape, newer version)."""
    try:
        with open(SNAP) as f:
            d = json.load(f)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, ("unreadable", "layout.json cannot be read right now: %r" % (e,))
    except ValueError as e:
        return None, ("invalid", "layout.json is not valid JSON: %r" % (e,))
    if not valid_layout(d):
        return None, ("invalid", "layout.json has the wrong shape")
    version = d.get("version", 1)
    if not isinstance(version, int) or version > SNAPSHOT_VERSION:
        return None, ("invalid", "layout.json was written by a newer version of this script")
    return d, None


def read_snapshot():
    d, problem = load_snapshot()
    if problem:
        complain_once((SNAP, mtime(SNAP), problem[0]), "%s, ignoring it" % problem[1])
    return d


def stable_signature(layout):
    """What identifies a layout for the save rules: its shape, the folder of each pane and which conversation is in
    which tab. Titles and foreground jobs change all the time and are left out."""
    return json.dumps([layout_shape(layout), sorted(
        (p.get("iterm_id") or "", p.get("cwd") or "",
         (p.get("claude") or {}).get("sessionId") or (p.get("codex") or {}).get("sessionId") or "")
        for p in all_panes(layout))], sort_keys=True)


def backup_names():
    return glob.glob(os.path.join(BACKUPS, "layout-*.json"))


def backup_group(path):
    """auto for periodic copies. Every other copy (launch, prev, rejected, older names) is a safety copy."""
    return "auto" if path.endswith("-auto.json") else "safe"


def prune_backups():
    """Periodic copies and safety copies are pruned separately, KEEP_AUTO_BACKUPS and KEEP_SAFE_BACKUPS each, oldest
    first by file time, so frequent periodic copies never push out a safety copy."""
    for group, keep in (("auto", KEEP_AUTO_BACKUPS), ("safe", KEEP_SAFE_BACKUPS)):
        names = sorted((p for p in backup_names() if backup_group(p) == group), key=mtime)
        for old in names[:-keep]:
            try:
                os.remove(old)
            except OSError:
                pass


def backup_layout(layout, tag):
    """Keep a dated copy of a layout in backups/. Tags: auto (periodic), launch and prev (safety copies)."""
    os.makedirs(BACKUPS, exist_ok=True)
    name = datetime.datetime.now().strftime("layout-%Y%m%d-%H%M%S-") + tag + ".json"
    with open(os.path.join(BACKUPS, name), "w") as f:
        json.dump(layout, f, indent=1)
    prune_backups()


def keep_rejected_copy():
    """layout.json exists but could not be used (corrupt, wrong shape, newer version). Keep a copy before replacing it."""
    try:
        os.makedirs(BACKUPS, exist_ok=True)
        shutil.copyfile(SNAP, os.path.join(BACKUPS, datetime.datetime.now().strftime("layout-%Y%m%d-%H%M%S-") + "rejected.json"))
        prune_backups()
        return True
    except OSError:
        return False


def write_snapshot(layout, previous, force_backup=False):
    """Save the layout. The previous one is backed up first when it had any panes and the new one is smaller
    (a safety copy), when forced, or when the newest periodic copy is older than BACKUP_EVERY (a periodic copy).
    A layout.json that exists but was rejected is copied aside first. Returns True if it had to do that."""
    rejected = False
    if previous and pane_total(previous) > 0:
        if force_backup or shrank(layout, previous):
            backup_layout(previous, "prev")
        else:
            newest = max([mtime(p) for p in glob.glob(os.path.join(BACKUPS, "layout-*-auto.json"))] or [0])
            if time.time() - newest > BACKUP_EVERY:
                backup_layout(previous, "auto")
    elif previous is None and os.path.exists(SNAP):
        rejected = keep_rejected_copy()
        if not rejected:
            raise OSError("could not keep a copy of the layout.json that was rejected, so it was not replaced")
    out = dict(layout)
    out["version"] = SNAPSHOT_VERSION
    out["saved_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    tmp = "%s.%d.tmp" % (SNAP, os.getpid())  # per process, so --once and the loop never share a temp file
    os.makedirs(STATE, exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, SNAP)
    return rejected


def prune_tmp():
    """Remove layout.json temp files left behind by a crash."""
    for path in glob.glob(SNAP + ".*tmp"):
        if time.time() - mtime(path) > TMP_MAX_AGE:
            try:
                os.remove(path)
            except OSError:
                pass


def prune_hooks(layout):
    """Delete hook records older than HOOK_MAX_DAYS whose tab no longer exists."""
    live = {p.get("iterm_id") for p in all_panes(layout)}
    cutoff = time.time() - HOOK_MAX_DAYS * 86400
    removed = 0
    for path in glob.glob(os.path.join(HOOKS, "*.json")) + glob.glob(os.path.join(HOOKS, "*.tmp")):
        tab_id = os.path.splitext(os.path.basename(path))[0]
        try:
            if tab_id not in live and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        log("pruned %d old hook records" % removed)


# ---------- restoring ----------

def resume_plan(pane):
    """(command, kind) to type into a pane. kind is claude, codex or cd. ("", None) when there is nothing safe to type."""
    claude = pane.get("claude") or {}
    cwd = claude.get("cwd") or pane.get("cwd")
    if cwd and not safe_folder(cwd):
        return "", None
    parts, kind = [], None
    if cwd:
        parts.append("cd " + shlex.quote(cwd))
        kind = "cd"
    if claude:
        if safe_id(claude.get("sessionId")):
            parts.append("claude --resume " + shlex.quote(claude["sessionId"]))
            kind = "claude"
    elif safe_id((pane.get("codex") or {}).get("sessionId")):
        parts.append("codex resume " + shlex.quote(pane["codex"]["sessionId"]))
        kind = "codex"
    return " && ".join(parts), kind


def resume_command(pane):
    """The command alone, for dry runs and tests. A dry run shows the plan before the live checks are made."""
    return resume_plan(pane)[0]


async def run_in_pane(session, pane, typed, stats=None):
    """Type the resume command into a pane that is sitting at a shell prompt. Returns claude, codex, cd or None."""
    cmd, kind = resume_plan(pane)
    if not cmd:
        log("skip %s: nothing safe to type" % pane.get("name"))
        return None
    job = None
    for _ in range(10):  # a brand new pane may not have started its shell yet: wait up to 5 seconds
        job = await session.async_get_variable("jobName")
        if job not in STARTING_UP:
            break
        await asyncio.sleep(0.5)
    if job in STARTING_UP and stats is not None:
        stats["unknown_job"] += 1
    if job not in SHELLS:
        if job not in STARTING_UP and stats is not None:
            names = {n.lstrip("-") for n in stats["cmds"].get(await session.async_get_variable("tty"), set())}
            if names and names <= SHELL_NAMES:
                stats["shell_misnamed"] += 1  # ps sees only a shell here, but iTerm2 names the job something else
        log("skip %s: %s" % (pane.get("name"), "already running %s" % job if job else "could not tell what it is running"))
        return None
    claude_id = (pane.get("claude") or {}).get("sessionId")
    if kind == "claude" and known_missing(claude_id):
        # No conversation file: a Claude tab that has had no messages yet, or an id that is not Claude's. Resuming would
        # fail either way, so restore the folder only. Foreign ids were already alarmed about when the tab was recorded.
        log("note %s: Claude has no conversation file for id %s (a tab with no messages has none), so only the folder is restored" % (pane.get("name"), claude_id[:8]))
        cmd, kind = resume_plan(dict(pane, claude=None, cwd=(pane["claude"].get("cwd") or pane.get("cwd"))))
        claude_id = None
        if not cmd:
            log("skip %s: nothing safe to type" % pane.get("name"))
            return None
    if pane.get("claude") and kind != "claude" and claude_id:  # only an id that failed the safe_id check gets here
        log("note %s: the saved Claude session id looks unusual, so only the folder is restored" % pane.get("name"))
    if kind == "claude" and (claude_id in typed or claude_id in live_session_ids()):
        log("skip %s: that conversation is already running in another terminal" % pane.get("name"))
        return None
    log("%s: %s" % (pane.get("name"), cmd))
    if not DRY:
        await session.async_send_text(cmd + "\r")
        await asyncio.sleep(0.7)
    if kind == "claude":
        typed.add(claude_id)
    return kind


async def fill_tab_from_snapshot(tab_node, session):
    """Split `session` into the snapshot's pane tree; return [(session, pane)] for the leaves."""
    if tab_node["type"] == "pane":
        return [(session, tab_node)]
    sessions = [session]
    for _ in tab_node["children"][1:]:
        sessions.append(await sessions[-1].async_split_pane(vertical=tab_node["vertical"]))
    leaves = []
    for child, s in zip(tab_node["children"], sessions):
        leaves += await fill_tab_from_snapshot(child, s)
    return leaves


def current_leaves(tab):
    out = []

    def walk(node):
        if isinstance(node, iterm2.Session):
            out.append(node)
        else:
            for c in node.children:
                walk(c)

    walk(tab.root)
    return out


async def wait_until_stable(app):
    """macOS restores windows a few at a time. Wait until the window and tab layout stops changing."""
    last, steady = None, 0
    for _ in range(10):
        current = layout_shape(await capture(app))
        steady = steady + 1 if current == last else 0
        last = current
        if steady >= 2:
            return
        await asyncio.sleep(2)


async def restore(connection, app, snap):
    n = resumable_count(snap)
    cur = await capture(app)
    done = {"claude": 0, "codex": 0, "cd": 0}
    typed = set()
    stats = {"unknown_job": 0, "shell_misnamed": 0, "cmds": tty_commands()}

    async def handle(session, pane):
        kind = await run_in_pane(session, pane, typed, stats)
        if kind:
            done[kind] += 1

    if layout_shape(cur) == layout_shape(snap):
        log("mode: match (macOS already restored the layout), %d resumable panes" % n)
        for w, sw in zip(app.terminal_windows, snap["windows"]):
            for t, st in zip(w.tabs, sw["tabs"]):
                for s, p in zip(current_leaves(t), panes(st["root"])):
                    await handle(s, p)
    elif is_blank_window(cur):
        log("mode: rebuild (one blank shell window), %d resumable panes" % n)
        if DRY:
            for p in all_panes(snap):
                log("would open: %s" % (resume_command(p) or p.get("name")))
            return done
        for wi, sw in enumerate(snap["windows"]):
            window = app.terminal_windows[0] if wi == 0 else await iterm2.Window.async_create(connection)
            for ti, st in enumerate(sw["tabs"]):
                tab = window.tabs[0] if ti == 0 else await window.async_create_tab()
                for s, p in await fill_tab_from_snapshot(st["root"], tab.current_session):
                    await handle(s, p)
    else:
        log("mode: skip (current layout differs from snapshot, not touching anything)")
        notify("Layout differs from the saved one. Nothing was resumed. See restore.log")
        return done
    if n > 0 and not (done["claude"] or done["codex"]) and not DRY:
        if stats["unknown_job"]:
            alarm(("nothing-resumed",), "Nothing was resumed because iTerm2 did not say what %d panes are running. An iTerm2 update may have changed the jobName variable" % stats["unknown_job"])
        elif stats["shell_misnamed"]:
            alarm(("nothing-resumed-names",), "Nothing was resumed because iTerm2 names the job of %d shell panes in a way this script does not recognize. An iTerm2 update may have changed the jobName variable" % stats["shell_misnamed"])
    log("done: %d Claude and %d Codex sessions resumed, %d shells moved to their folder" % (done["claude"], done["codex"], done["cd"]))
    if (done["claude"] or done["codex"]) and not DRY:
        notify("Resumed %d Claude and %d Codex sessions" % (done["claude"], done["codex"]))
    return done


async def selftest(connection):
    """Build a throwaway window from a fake layout, check the shape round-trips, then close it."""
    pane = {"type": "pane", "name": "selftest"}
    fake = [
        {"type": "split", "vertical": True, "children": [pane, {"type": "split", "vertical": False, "children": [pane, pane]}]},
        {"type": "split", "vertical": False, "children": [pane, pane]},
        pane,
    ]
    await iterm2.async_get_app(connection)
    window = await iterm2.Window.async_create(connection)
    try:
        for i, node in enumerate(fake):
            tab = window.tabs[0] if i == 0 else await window.async_create_tab()
            await fill_tab_from_snapshot(node, tab.current_session)
        await asyncio.sleep(2)
        app = await iterm2.async_get_app(connection)
        mine = [w for w in app.terminal_windows if w.window_id == window.window_id][0]
        got = [shape(await capture_node(t.root, {})) for t in mine.tabs]  # empty ctx: shape only
        want = [shape(n) for n in fake]
        jobs = []
        for t in mine.tabs:
            for s in current_leaves(t):
                jobs.append(await s.async_get_variable("jobName"))
        log("selftest want: %s" % json.dumps(want))
        log("selftest got:  %s" % json.dumps(got))
        log("selftest shape match: %s" % (got == want))
        log("selftest fresh-shell jobNames: %s, all shells or still starting up: %s" % (jobs, all(j in SHELLS or j in STARTING_UP for j in jobs)))
    finally:
        await window.async_close(force=True)


# ---------- health check ----------

def doctor():
    """Check the setup and the files this script depends on. Prints OK, INFO and WARN lines and returns 1 if there is
    any WARN. It only reads: it never writes a file, never notifies, and needs no connection to iTerm2."""
    warnings = []

    def say(level, msg):
        print("%-4s %s" % (level, msg))
        if level == "WARN":
            warnings.append(msg)

    say("INFO", "Python %s, iterm2 module %s" % (sys.version.split()[0], getattr(iterm2, "__version__", "unknown") if iterm2
        else "not importable here (fine for --doctor, but restoring needs iTerm2's own Python)"))

    registered = False
    for name in ("settings.json", "settings.local.json"):
        try:
            with open(os.path.join(HOME, ".claude", name)) as f:
                registered = registered or "iterm-restore/hook.py" in f.read()
        except OSError:
            pass
    say("OK" if registered else "WARN", "the SessionStart hook is registered in ~/.claude/settings.json" if registered
        else "the SessionStart hook is not registered in ~/.claude/settings.json or settings.local.json (see step 5 of the guide)")

    records = glob.glob(os.path.join(HOOKS, "*.json"))
    newest_record = max([mtime(p) for p in records] or [0])
    if records:
        say("OK", "%d hook records, newest %.1f days old" % (len(records), (time.time() - newest_record) / 86400))
    else:
        say("WARN", "no hook records yet: open a new Claude tab and check again")
    all_live = live_claude_sessions()
    live = [s for s in all_live if s["kind"] == "interactive" and s["started"]]
    if registered and live and newest_record < max(s["started"] for s in live) - 60:
        say("WARN", "a Claude session started after the newest hook record. If it was in iTerm2, the hook may not be firing (a session started outside iTerm2 can also cause this)")
    readable = 0
    for s in live:
        if time.time() - s["started"] <= STALE_HOOK_GRACE:
            continue  # too young to have written its own hook record
        tab = tab_of_pid(s["pid"])
        if not tab or "/" in tab:
            continue
        readable += 1
        try:
            with open(os.path.join(HOOKS, tab + ".json")) as f:
                recorded = json.load(f).get("session_id")
        except Exception:
            continue
        if recorded and recorded != s["sessionId"]:
            say("WARN", "a running Claude (tab %s) is conversation %s but the hook record for that tab says %s. Another program, such as Grok, Codex or a second Claude in the same tab, may be writing hook records. A restore would use Claude's own file" % (tab[:8], str(s["sessionId"])[:8], str(recorded)[:8]))
    if live and not readable:
        say("INFO", "could not read the tab of any running Claude from its environment (macOS may hide it), so the hook record check was skipped")
    for s in live:
        name = os.path.basename(ps_output(["-o", "comm=", "-p", str(s["pid"])]).strip())
        if name and name != "claude":
            say("WARN", "a running Claude (process %d) is named %s, not claude, so the hook cannot tell a headless run from the interactive one" % (s["pid"], name))
            break
    if all_live and any(s["tty"] for s in all_live) and not live:
        say("WARN", "Claude is running in a terminal but no session file says kind interactive (kinds seen: %s), so Claude's own file cannot pick the conversation" % ", ".join(sorted({str(s["kind"]) for s in all_live})))
    if all_live and not transcripts_findable():
        say("WARN", "Claude is running but no conversation files can be found under ~/.claude/projects (Claude may have moved them), so the check that stops a wrong id being typed is off")
    try:
        with open(HOOK_ERRORS) as f:
            errors = f.read().strip().splitlines()
    except OSError:
        errors = []
    recent_errors = [line for line in errors if recent_line(line, 7 * 24)]
    if recent_errors:
        say("WARN", "the hook logged %d errors in the last 7 days, last: %s" % (len(recent_errors), recent_errors[-1][:150]))

    sessions = sorted(glob.glob(os.path.join(SESSIONS, "*.json")), key=mtime, reverse=True)[:5]
    if not sessions:
        say("INFO", "no Claude session files found, so only the hook can say which conversation is in which tab")
    else:
        needed = {"pid", "sessionId", "kind", "cwd", "procStart"}
        missing, unparsable = set(), False
        for path in sessions:
            try:
                with open(path) as f:
                    d = json.load(f)
                missing |= needed - set(d)
                unparsable = unparsable or (d.get("procStart") is not None and parse_ps_time(d["procStart"]) is None)
            except Exception:
                missing |= needed
        say("WARN" if missing else "OK", ("Claude Code's session files no longer have: %s (the fallback is unreliable, only the hook works)" % ", ".join(sorted(missing)))
            if missing else "Claude Code's session files have the fields this script reads")
        if unparsable:
            say("WARN", "Claude Code's procStart time can no longer be read, so a reused process id cannot be told apart from a live Claude")
    unnamed = []
    for line in ps_output(["-axo", "pid=,tty=,comm="]).splitlines():
        parts = line.split(None, 2)  # only Claudes attached to a terminal: helper processes have none
        if len(parts) == 3 and parts[1] != "??" and os.path.basename(parts[2].strip()) == "claude" and not os.path.exists(os.path.join(SESSIONS, parts[0] + ".json")):
            unnamed.append(parts[0])
    if unnamed:
        say("WARN", "Claude is running (process %s) but ~/.claude/sessions/%s.json does not exist, so Claude Code may have renamed its session files. The fallback is off, the hook still works" % (unnamed[0], unnamed[0]))
    own_start = ps_output(["-o", "lstart=", "-p", str(os.getpid())])
    if parse_ps_time(own_start) is None:
        say("WARN", "ps prints process start times in a format this script cannot read: %r" % own_start.strip()[:40])

    codex_files = recent_codex_files()
    if not os.path.isdir(CODEX_SESSIONS):
        say("INFO", "no ~/.codex/sessions folder, so Codex tabs only get their folder restored unless they were launched with an id")
    elif not codex_files:
        if any(f.endswith(".jsonl") for _, _, files in os.walk(CODEX_SESSIONS) for f in files):
            say("WARN", "Codex has session files, but none in the folder layout this script searches (YYYY/MM/DD/rollout-*.jsonl). Codex may have changed it")
        else:
            say("INFO", "no Codex sessions yet")
    else:
        try:
            with open(codex_files[0]) as f:
                payload = json.loads(f.readline()).get("payload", {})
            absent = {"id", "cwd", "originator"} - set(payload)
        except Exception:
            absent = {"id", "cwd", "originator"}
        labels = set()
        for path in codex_files[:20]:
            try:
                with open(path) as f:
                    labels.add(json.loads(f.readline()).get("payload", {}).get("originator"))
            except Exception:
                pass
        if absent:
            say("WARN", "Codex session files no longer have: %s (Codex tabs will only get their folder restored)" % ", ".join(sorted(absent)))
        elif "codex-tui" not in labels:
            say("WARN", "none of the 20 newest Codex sessions is labeled codex-tui. If you use Codex in the terminal, Codex may have changed how it labels sessions")
        else:
            say("OK", "Codex session files have the fields this script reads")

    snap, problem = load_snapshot()
    if problem:
        say("WARN", problem[1])
    elif snap:
        say("OK", "saved layout: %d windows, %d panes, %d resumable, saved %s" % (len(snap["windows"]), pane_total(snap), resumable_count(snap), snap.get("saved_at", "at an unknown time")))
    else:
        say("WARN", "there is no saved layout yet")

    processes = ps_output(["-axo", "command="])
    iterm_open = bool(re.search(r"/iTerm2(\s|$)", processes)) or "iTerm.app/Contents/MacOS" in processes
    script_running = "claude_restore.py" in processes
    if script_running:
        say("OK", "the script is running inside iTerm2")
    else:
        launchers = [os.path.join(HOME, "Library", "Application Support", "iTerm2", "Scripts", "AutoLaunch", "claude_restore.py")]
        launchers += glob.glob(os.path.join(HOME, ".config", "iterm2", "AppSupport", "Scripts", "AutoLaunch", "claude_restore.py"))
        custom = subprocess.run(["defaults", "read", "com.googlecode.iterm2", "PrefsCustomFolder"], capture_output=True, text=True).stdout.strip()
        if custom:
            launchers.append(os.path.join(os.path.expanduser(custom), "AppSupport", "Scripts", "AutoLaunch", "claude_restore.py"))
        found = any(os.path.exists(p) for p in launchers)
        if iterm_open:
            say("WARN", "iTerm2 is open but the script is not running (%s, and the Python API must be on)" %
                ("the launcher is in an AutoLaunch folder" if found else "claude_restore.py was not found in an AutoLaunch folder"))
        else:
            say("INFO", "iTerm2 is not open, so the script is not running (launcher %s)" % ("found" if found else "not found in the usual AutoLaunch folders"))

    names = backup_names()
    say("INFO", "%d backups (%d periodic, %d safety)" % (len(names), sum(backup_group(p) == "auto" for p in names), sum(backup_group(p) == "safe" for p in names)))
    try:
        with open(LOG) as f:
            lines = f.read().splitlines()
        bad = [x for x in lines if (" failed" in x or "WARNING" in x) and recent_line(x, 24)]
        say("WARN" if bad else "OK", ("%d problems in restore.log in the last 24 hours, last: %s" % (len(bad), bad[-1][:150])) if bad
            else "restore.log has no problems in the last 24 hours (%d KB)" % (os.path.getsize(LOG) // 1024))
    except OSError:
        say("INFO", "no restore.log yet")

    print("\n%s" % ("%d warning(s). Fix those first." % len(warnings) if warnings else "All good."))
    return 1 if warnings else 0


# ---------- main loop ----------

async def main(connection):
    if SELFTEST:
        await selftest(connection)
        return
    app = await iterm2.async_get_app(connection)

    if ONCE:
        try:
            layout = await capture(app)
            if not layout["windows"]:
                log("--once: iTerm2 has no windows, snapshot left as it was")
                return
            prune_hooks(layout)
            previous, problem = load_snapshot()
            if problem and problem[0] == "unreadable":
                log("--once: %s, left it alone" % problem[1])
                return
            if previous and stable_signature(previous) == stable_signature(layout):
                log("snapshot unchanged: %d windows, %d resumable panes" % (len(layout["windows"]), resumable_count(layout)))
                return
            write_snapshot(layout, previous, force_backup=True)
            log("snapshot written: %d windows, %d resumable panes" % (len(layout["windows"]), resumable_count(layout)))
        except Exception as e:
            log("--once failed: %r" % (e,))
        return

    if DRY:
        snap = read_snapshot()
        if not snap:
            log("no snapshot yet")
        else:
            await restore(connection, app, snap)
        return

    if not STARTED["restored"]:
        STARTED["restored"] = True
        # Launch: wait for windows (macOS restoration or the default one) to settle, then restore.
        for _ in range(30):
            if app.terminal_windows:
                break
            await asyncio.sleep(0.5)
        try:
            await wait_until_stable(app)
            snap, problem = load_snapshot()
            if problem:
                alarm(("launch-snapshot", problem[0]), "The saved layout could not be used (%s), so nothing was restored" % problem[1])
            if snap and snap["windows"]:
                backup_layout(snap, "launch")  # whatever happens next, the pre-launch snapshot survives
                await restore(connection, app, snap)
            else:
                log("no usable snapshot, starting fresh")
            prune_hooks(await capture(await iterm2.async_get_app(connection)))
            prune_tmp()
        except Exception as e:
            log("launch restore failed: %r" % (e,))
            notify("Restore failed. See restore.log")

    # Then keep the snapshot fresh. A shrinking layout (fewer resumable panes or fewer panes) is only accepted if it
    # persists one more round, and the previous snapshot is backed up before it is replaced, so a half-closed iTerm2
    # during quit or a skipped restore cannot destroy a good snapshot.
    pending, unknown_rounds, blind_rounds, last_prune = None, 0, 0, time.time()
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            app = await iterm2.async_get_app(connection)
            if not app.terminal_windows:
                continue
            layout = await capture(app)
            unknown = sum(1 for p in all_panes(layout) if p.get("claude_unknown"))
            unknown_rounds = unknown_rounds + 1 if unknown else 0
            if unknown_rounds == UNKNOWN_ROUNDS:
                alarm(("unknown-claude", unknown), "Cannot tell which Claude conversation is in %d tabs, neither the hook nor Claude Code's session files say. Run restore.py --doctor" % unknown)
            blind = sum(1 for p in all_panes(layout) if p.get("blind"))
            blind_rounds = blind_rounds + 1 if blind else 0
            if blind_rounds == UNKNOWN_ROUNDS:
                alarm(("blind", blind), "iTerm2 gave no terminal or folder for %d tabs. An iTerm2 update may have renamed its tty or path variable" % blind)
            if time.time() - last_prune > PRUNE_EVERY:
                prune_hooks(layout)
                prune_tmp()
                last_prune = time.time()
            previous, problem = load_snapshot()
            if problem:
                complain_once((SNAP, mtime(SNAP), problem[0]), "%s, ignoring it" % problem[1])
                if problem[0] == "unreadable":
                    continue  # maybe a passing glitch: never overwrite what we could not read
            if previous and stable_signature(previous) == stable_signature(layout):
                pending = None
                continue
            if previous and shrank(layout, previous):
                sig = stable_signature(layout)
                if pending != sig:
                    pending = sig
                    continue
            pending = None
            if write_snapshot(layout, previous):
                alarm(("rejected", mtime(SNAP)), "The saved layout could not be used, so a copy was kept in backups/ before it was replaced")
        except Exception as e:
            complain_once(("loop", repr(e)), "snapshot failed: %r" % (e,))


if DOCTOR:
    sys.exit(doctor())
elif iterm2 is None:
    sys.exit("restore.py needs the iterm2 Python module. Run it with iTerm2's own Python (see the guide), or use --doctor.")
elif DRY or ONCE or SELFTEST:
    iterm2.run_until_complete(main, retry=False)
else:
    iterm2.run_forever(main)
