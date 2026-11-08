"""Behavior checks for iterm2-claude-restore. Run: python3 tests/check.py

Exit code 1 on any failure. Touches only temporary folders, never your real settings or snapshot.
Works with any Python 3, it does not need the iterm2 module. A few checks that need a real fake parent
process are skipped (and say so) on Pythons where that cannot work."""
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import shutil
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
failures = []
# the checks must not depend on which Claude account started them, nor on the user's own accounts file
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ["ITERM_RESTORE_ACCOUNTS"] = os.path.join(tempfile.mkdtemp(), "no-accounts.json")


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ((" :: " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


restore_src = open(os.path.join(BASE, "restore.py")).read().rstrip("\n")
hook_src = open(os.path.join(BASE, "hook.py")).read().rstrip("\n")
readme = open(os.path.join(BASE, "README.md")).read()
check("no em or en dashes in the README or scripts", not any(c in (readme + restore_src + hook_src) for c in (chr(0x2014), chr(0x2013))))

# hook.py: silent, exit 0, writes only inside its folder, in a throwaway HOME
with tempfile.TemporaryDirectory() as home:
    env = dict(os.environ, HOME=home, ITERM_SESSION_ID="w0t0p0:ABC-123")
    r = subprocess.run([sys.executable, os.path.join(BASE, "hook.py")], input=json.dumps({"session_id": "s1", "cwd": "/x", "source": "startup"}),
                       capture_output=True, text=True, env=env)
    out_dir = os.path.join(home, ".claude", "iterm-restore", "hooks")
    check("hook exits 0 and prints nothing", r.returncode == 0 and r.stdout == "" and r.stderr == "", r.stdout + r.stderr)
    check("hook wrote the tab record", os.path.exists(os.path.join(out_dir, "ABC-123.json")))
    check("hook records no config folder when CLAUDE_CONFIG_DIR is unset", "config_dir" not in json.load(open(os.path.join(out_dir, "ABC-123.json"))))
    alt = os.path.join(home, "alt-config")
    os.makedirs(alt)
    os.symlink(alt, os.path.join(home, "alt-link"))
    subprocess.run([sys.executable, os.path.join(BASE, "hook.py")], input=json.dumps({"session_id": "s1", "cwd": "/x"}), capture_output=True, text=True,
                   env=dict(env, CLAUDE_CONFIG_DIR=os.path.join(home, "alt-link")))
    check("hook records the real config folder when CLAUDE_CONFIG_DIR is set", json.load(open(os.path.join(out_dir, "ABC-123.json"))).get("config_dir") == os.path.realpath(alt))
    for bad in ("w0t0p0:../../escape", "w0t0p0:a/b", "w0t0p0:..", "nocolon", ""):
        env2 = dict(env, ITERM_SESSION_ID=bad)
        r = subprocess.run([sys.executable, os.path.join(BASE, "hook.py")], input=json.dumps({"session_id": "s2"}), capture_output=True, text=True, env=env2)
        escaped = [p for p, _, fs in os.walk(home) for f in fs if not p.startswith(out_dir)]
        check("hook safe for tab id %r" % bad, r.returncode == 0 and r.stdout == "" and not escaped, str(escaped))
    r = subprocess.run([sys.executable, os.path.join(BASE, "hook.py")], input="not json", capture_output=True, text=True, env=env)
    check("hook survives garbage input", r.returncode == 0 and r.stdout == "")

# restore.py helpers: load everything above the launcher lines, no iTerm2 connection
head = restore_src.split("\nif DOCTOR:")[0]
assert "run_forever" not in head, "the harness must never start the iTerm2 loop"
with tempfile.TemporaryDirectory() as state:
    os.environ["ITERM_RESTORE_STATE"] = state
    ns = {"__name__": "qa"}
    exec(compile(head, "restore_head", "exec"), ns)
    uid = "6106b975-2b1f-4cbb-8e5b-9d1a1f0e6b11"
    nasty = "/tmp/a b; touch /tmp/pwned && $(id) `id` 'q\"x"
    cmd = ns["resume_command"]({"cwd": nasty, "claude": {"sessionId": uid, "cwd": nasty}})
    check("folder and claude id each stay one safe token", shlex.split(cmd) == ["cd", nasty, "&&", "claude", "--resume", uid], cmd)
    cmd2 = ns["resume_command"]({"cwd": "/tmp", "codex": {"sessionId": uid}})
    check("codex id is one safe token", shlex.split(cmd2) == ["cd", "/tmp", "&&", "codex", "resume", uid], cmd2)
    check("unsafe claude id is never typed", "claude" not in ns["resume_command"]({"cwd": "/tmp", "claude": {"sessionId": "abc; rm -rf ~"}}))
    check("unsafe codex id is never typed", "codex" not in ns["resume_command"]({"cwd": "/tmp", "codex": {"sessionId": "x; y"}}))
    check("non-string id is never typed", ns["resume_command"]({"cwd": "/tmp", "claude": {"sessionId": 123}}) == "cd /tmp")
    for ctrl in ("/tmp/a\x03b", "/tmp/a\x1b[2Jb", "/tmp/a\nb", "/tmp/a\x7fb"):
        check("control characters in a folder refuse the whole command %r" % ctrl, ns["resume_command"]({"cwd": ctrl, "claude": {"sessionId": uid}}) == "")
    for flagish in ("--dangerously-skip-permissions", "-abcdefgh", "--resume-me-please"):
        check("flag-shaped id %r is never typed" % flagish, "claude" not in ns["resume_command"]({"cwd": "/tmp", "claude": {"sessionId": flagish}}) and ns["safe_id"](flagish) is False)
        check("flag-shaped codex id %r is never typed" % flagish, "codex" not in ns["resume_command"]({"cwd": "/tmp", "codex": {"sessionId": flagish}}))
    check("a normal uuid is accepted", ns["safe_id"](uid))
    for rel in ("relative/path", "-evil", "~", "./x"):
        check("non-absolute folder %r refuses the command" % rel, ns["resume_command"]({"cwd": rel, "claude": {"sessionId": uid}}) == "")
    check("cd only when there is no id", ns["resume_command"]({"cwd": "/tmp"}) == "cd /tmp")
    check("empty command when nothing known", ns["resume_command"]({}) == "")
    check("kind is reported", ns["resume_plan"]({"cwd": "/tmp", "claude": {"sessionId": uid}})[1] == "claude" and ns["resume_plan"]({"cwd": "/tmp"})[1] == "cd")

    # log trimming
    log = os.path.join(state, "restore.log")
    open(log, "w").write("x" * 100 + "\n" + ("line\n" * 100000))
    ns["trim_log"]()
    n = sum(1 for _ in open(log))
    check("log trims to 500 lines", n <= 501, str(n))

    # layout helpers
    pane = lambda **kw: dict({"type": "pane"}, **kw)
    two = {"windows": [{"tabs": [{"root": {"type": "split", "vertical": True, "children": [pane(claude={"sessionId": uid}), pane()]}}]}]}
    check("resumable_count counts claude panes", ns["resumable_count"](two) == 1)
    cx = {"windows": [{"tabs": [{"root": pane(codex={"sessionId": uid})}]}]}
    check("a codex-only layout is resumable", ns["resumable_count"](cx) == 1)
    check("codex pane without an id is not resumable", ns["resumable_count"]({"windows": [{"tabs": [{"root": pane(codex={"sessionId": None})}]}]}) == 0)
    check("shape is stable", ns["layout_shape"](two) == [[["s", True, ["p", "p"]]]])
    blank = {"windows": [{"tabs": [{"root": pane(job="zsh")}]}]}
    check("a lone idle shell is a blank window", ns["is_blank_window"](blank))
    check("a lone busy pane is not a blank window", not ns["is_blank_window"]({"windows": [{"tabs": [{"root": pane(job="vim")}]}]}))
    check("a lone pane with unknown job is not a blank window", not ns["is_blank_window"]({"windows": [{"tabs": [{"root": pane(job=None)}]}]}))
    check("a lone claude pane is not a blank window", not ns["is_blank_window"]({"windows": [{"tabs": [{"root": pane(job="zsh", claude={"sessionId": uid})}]}]}))
    check("two windows are not a blank window", not ns["is_blank_window"]({"windows": blank["windows"] * 2}))

    # snapshot validation and backups
    for name, bad in (("empty list", []), ("no windows key", {"x": 1}), ("window without tabs", {"windows": [{}]}),
                      ("tab without root", {"windows": [{"tabs": [{}]}]}), ("split without children", {"windows": [{"tabs": [{"root": {"type": "split"}}]}]}),
                      ("string", "x")):
        open(ns["SNAP"], "w").write(json.dumps(bad))
        check("read_snapshot rejects %s" % name, ns["read_snapshot"]() is None)
    open(ns["SNAP"], "w").write("{not json")
    check("read_snapshot rejects corrupt JSON", ns["read_snapshot"]() is None)
    check("corrupt snapshot is logged", "layout.json" in open(log).read())
    os.remove(ns["SNAP"])
    check("missing snapshot is None and quiet", ns["read_snapshot"]() is None)
    ns["write_snapshot"](two, None)
    check("a written snapshot reads back valid", ns["read_snapshot"]() is not None)
    smaller = {"windows": [{"tabs": [{"root": pane()}]}]}
    before = len(glob.glob(os.path.join(state, "backups", "*.json")))
    ns["write_snapshot"](smaller, ns["read_snapshot"]())
    after = len(glob.glob(os.path.join(state, "backups", "*.json")))
    check("a drop in resumable panes backs up the old snapshot first", after == before + 1, "%d -> %d" % (before, after))
    # backups: periodic and safety copies are pruned separately
    bdir = os.path.join(state, "backups")
    for f in glob.glob(os.path.join(bdir, "*.json")):
        os.remove(f)
    ns["backup_layout"](two, "launch")
    safe_before = sorted(glob.glob(os.path.join(bdir, "*-launch.json")))
    for i in range(30):
        ns["backup_layout"](two, "auto")
        os.rename(max(glob.glob(os.path.join(bdir, "*-auto.json")), key=os.path.getmtime), os.path.join(bdir, "layout-20200101-0000%02d-auto.json" % i))
    check("periodic copies are capped at 10", len(glob.glob(os.path.join(bdir, "*-auto.json"))) <= 10)
    check("a safety copy survives 30 periodic ones", safe_before and all(os.path.exists(p) for p in safe_before))
    for i in range(30):
        ns["backup_layout"](two, "prev%02d" % i)
    check("safety copies are capped at 10", len([p for p in glob.glob(os.path.join(bdir, "*.json")) if ns["backup_group"](p) == "safe"]) <= 10)

    # a closing window shrinks the layout even when nothing in it was resumable
    shell = lambda: pane(job="zsh")
    three = {"windows": [{"tabs": [{"root": shell()}, {"root": shell()}, {"root": shell()}]}]}
    two_shells = {"windows": [{"tabs": [{"root": shell()}, {"root": shell()}]}]}
    check("losing a plain shell tab counts as shrinking", ns["shrank"](two_shells, three))
    check("same size is not shrinking", not ns["shrank"](three, three))
    check("growing is not shrinking", not ns["shrank"](three, two_shells))
    check("losing a resumable pane counts as shrinking", ns["shrank"](smaller, two))
    for f in glob.glob(os.path.join(bdir, "*.json")):
        os.remove(f)
    ns["write_snapshot"](three, None)
    ns["write_snapshot"](two_shells, ns["read_snapshot"]())
    check("the shrunk-from snapshot is among the safety copies", any(os.path.basename(p).endswith("-prev.json") for p in glob.glob(os.path.join(bdir, "*.json"))))

    # pid reuse: Claude records UTC, ps prints local time
    sm = ns["start_matches"]
    check("same start time matches", sm("Fri Oct 30 16:19:47 2026", "Fri Oct 30 16:19:47 2026"))
    check("start time one hour apart (UTC vs local) matches", sm("Fri Oct 30 16:19:47 2026", "Fri Oct 30 17:19:47 2026"))
    check("start time five and a half hours apart matches", sm("Fri Oct 30 16:19:47 2026", "Fri Oct 30 21:49:47 2026"))
    check("a different process (7 minutes apart) does not match", not sm("Fri Oct 30 16:19:47 2026", "Fri Oct 30 16:26:47 2026"))
    check("a different process (3h07 apart) does not match", not sm("Fri Oct 30 16:19:47 2026", "Fri Oct 30 19:26:47 2026"))
    check("padded day numbers parse", sm("Fri Oct  3 16:19:47 2026", "Fri Oct  3 17:19:47 2026"))
    check("missing start time counts as a match", sm(None, "Fri Oct 30 16:19:47 2026"))
    check("unreadable start time counts as a match", sm("garbage", "Fri Oct 30 16:19:47 2026") and sm(12345, "x"))

    # snapshot version
    open(ns["SNAP"], "w").write(json.dumps({"windows": [], "version": 99}))
    check("a snapshot from a newer version is ignored", ns["read_snapshot"]() is None)
    open(ns["SNAP"], "w").write(json.dumps({"windows": [], "version": "x"}))
    check("a non-numeric version is ignored", ns["read_snapshot"]() is None)
    ns["write_snapshot"](two, None)
    check("written snapshots carry the current version", json.load(open(ns["SNAP"]))["version"] == ns["SNAPSHOT_VERSION"])

    # Codex search window and caches have a ceiling
    import time as _time
    codex_dir = os.path.join(state, "codex")
    for day in range(40):
        d = os.path.join(codex_dir, "2026", "09", "%02d" % (day + 1))
        os.makedirs(d)
        for n in range(10):
            open(os.path.join(d, "rollout-x%d.jsonl" % n), "w").write("{}\n")
    ns["CODEX_SESSIONS"] = codex_dir
    files = ns["recent_codex_files"]()
    days_seen = {os.path.basename(os.path.dirname(f)) for f in files}
    check("the Codex search stops at the newest 30 day folders", len(days_seen) <= ns["CODEX_DAYS_SCANNED"] and "01" not in days_seen, str(sorted(days_seen))[:80])
    check("the Codex search stops at the file ceiling", len(files) <= ns["CODEX_MAX_FILES"])
    ns["_codex_first_lines"].update({"/nonexistent/%d" % i: (None, None, None) for i in range(6000)})
    ns["newest_codex_sessions"]([])
    check("the Codex caches only keep files in the search window", len(ns["_codex_first_lines"]) <= ns["CODEX_MAX_FILES"])
    for i in range(1500):
        ns["complain_once"](("k", i), "x")
    check("the complaint memory is bounded", len(ns["_complained"]) <= 1001)

    # loud once, and only once
    heard = []
    ns["notify"] = lambda m: heard.append(m)
    ns["alarm"](("t", "once"), "something broke")
    ns["alarm"](("t", "once"), "something broke")
    check("an alarm notifies exactly once", len(heard) == 1)
    check("an alarm writes a WARNING line", "WARNING: something broke" in open(log).read())

    # which source wins for a tab
    pick = ns["pick_claude"]
    now = _time.time()
    live = {"sessionId": "live-aaaaaaaa", "cwd": "/x", "started": now - 1000}
    fresh = {"session_id": "hook-bbbbbbbb", "cwd": "/y", "saved_at": now - 500}
    stale = {"session_id": "hook-bbbbbbbb", "cwd": "/y", "saved_at": now - 5000}
    same_id = {"session_id": "live-aaaaaaaa", "cwd": "/hookfolder", "saved_at": now - 500}
    r = pick(same_id, live, "t0")
    check("when hook and live file agree the hook record is used, it has the folder", r["sessionId"] == "live-aaaaaaaa" and r["via"] == "hook" and r["cwd"] == "/hookfolder")
    check("a different id from the hook loses to the live file, even when the hook record is newer", pick(fresh, live, "t1")["sessionId"] == "live-aaaaaaaa")
    check("a different id from an older hook record loses to the live file", pick(stale, live, "t2")["sessionId"] == "live-aaaaaaaa")
    check("a disagreeing hook record raises an alarm", any("a tab's hook record" in m for m in heard))
    check("without a hook record the live file is used", pick(None, live, "t3")["sessionId"] == "live-aaaaaaaa")
    check("without a live file the hook record is used", pick(fresh, None, "t4")["sessionId"] == "hook-bbbbbbbb")
    check("with neither source there is no answer", pick(None, None, "t5") is None)
    check("a hook record with no usable time is only used when it agrees or there is no live file", pick({"session_id": "hook-bbbbbbbb", "saved_at": "x"}, None, "t6")["sessionId"] == "hook-bbbbbbbb")

    # snapshot problems are classified, never logged by the reader, and a rejected file is kept
    open(ns["SNAP"], "w").write("{not json")
    check("corrupt JSON is invalid", ns["load_snapshot"]()[1][0] == "invalid")
    open(ns["SNAP"], "w").write(json.dumps({"windows": [], "version": 99}))
    check("a newer version is invalid", ns["load_snapshot"]()[1][0] == "invalid")
    os.remove(ns["SNAP"])
    check("a missing file is not a problem", ns["load_snapshot"]() == (None, None))
    os.mkdir(ns["SNAP"])
    check("a file that cannot be read right now is unreadable, not invalid", ns["load_snapshot"]()[1][0] == "unreadable")
    os.rmdir(ns["SNAP"])
    open(ns["SNAP"], "w").write("{not json")
    rejected_before = len(glob.glob(os.path.join(bdir, "*-rejected.json")))
    check("replacing a rejected file reports it", ns["write_snapshot"](two, None) is True)
    kept = glob.glob(os.path.join(bdir, "*-rejected.json"))
    check("the rejected file was copied aside first", len(kept) == rejected_before + 1 and open(kept[-1]).read() == "{not json" or any(open(k).read() == "{not json" for k in kept))
    check("a normal save does not report a rejected file", ns["write_snapshot"](two, ns["read_snapshot"]()) is False)

    # titles and jobs do not count as change, folders and conversations do
    sig = ns["stable_signature"]
    a = {"windows": [{"tabs": [{"root": pane(iterm_id="A", cwd="/a", name="spinner 1", job="zsh", claude={"sessionId": uid})}]}]}
    b = {"windows": [{"tabs": [{"root": pane(iterm_id="A", cwd="/a", name="spinner 2", job="claude", claude={"sessionId": uid})}]}]}
    c = {"windows": [{"tabs": [{"root": pane(iterm_id="A", cwd="/b", name="x", job="zsh", claude={"sessionId": uid})}]}]}
    d = {"windows": [{"tabs": [{"root": pane(iterm_id="A", cwd="/a", name="x", job="zsh", claude={"sessionId": uid + "2"})}]}]}
    check("a changing title or job does not change the signature", sig(a) == sig(b))
    check("a different folder changes the signature", sig(a) != sig(c))
    check("a different conversation changes the signature", sig(a) != sig(d))
    e = {"windows": [{"tabs": [{"root": pane(iterm_id="A", cwd="/a", name="x", job="zsh", claude={"sessionId": uid, "config_dir": "/other"})}]}]}
    check("learning a tab's account changes the signature, so it gets saved", sig(a) != sig(e))

    # a rejected C1 control character
    check("C1 control characters are refused in folders", ns["resume_command"]({"cwd": "/tmp/a\u0085b", "claude": {"sessionId": uid}}) == "")

    # Claude session files are found by process id, a start time that cannot be read still works but says so
    ns["SESSIONS"] = os.path.join(state, "sessions")
    os.makedirs(ns["SESSIONS"])
    mine = os.path.join(ns["SESSIONS"], "%d.json" % os.getpid())
    json.dump({"pid": os.getpid(), "sessionId": "live-cccccccc", "kind": "interactive", "cwd": "/x", "procStart": "garbage in a new format"}, open(mine, "w"))
    json.dump({"pid": 999999, "sessionId": "other-eeeeeeee", "kind": "interactive", "cwd": "/x"}, open(os.path.join(ns["SESSIONS"], "999999.json"), "w"))
    json.dump({"pid": os.getpid(), "sessionId": "stray-ffffffff", "kind": "interactive", "cwd": "/x"}, open(os.path.join(ns["SESSIONS"], "not-a-pid-name.json"), "w"))
    heard.clear()
    found = ns["live_claude_sessions"]()
    check("an unreadable procStart still counts the session as live", any(s["sessionId"] == "live-cccccccc" for s in found))
    check("an unreadable procStart raises an alarm", any("procStart" in m for m in heard))
    check("a file for a process that is not running is ignored", not any(s["sessionId"] == "other-eeeeeeee" for s in found))
    check("a file not named after a running process id is never opened", not any(s["sessionId"] == "stray-ffffffff" for s in found))
    os.utime(mine, (0, 0))
    check("a session file untouched for a long time still counts (a long idle Claude keeps its old file)", any(s["sessionId"] == "live-cccccccc" for s in ns["live_claude_sessions"]()))
    real_live = ns["live_claude_sessions"]
    ns["live_claude_sessions"] = lambda: [
        {"sessionId": "old-gggggggg", "cwd": "/a", "kind": "interactive", "tty": "/dev/ttys9", "started": 100},
        {"sessionId": "new-hhhhhhhh", "cwd": "/a", "kind": "interactive", "tty": "/dev/ttys9", "started": 200}]
    check("when two Claudes share a terminal the newer one wins", ns["claude_by_tty"]()["/dev/ttys9"]["sessionId"] == "new-hhhhhhhh")
    ns["live_claude_sessions"] = real_live

    # saved layouts with the wrong inner shape are rejected, so they can never break the signature
    for name, bad in (("split without vertical", {"windows": [{"tabs": [{"root": {"type": "split", "children": [pane(), pane()]}}]}]}),
                      ("claude as a string", {"windows": [{"tabs": [{"root": pane(claude="x")}]}]}),
                      ("codex as a list", {"windows": [{"tabs": [{"root": pane(codex=[1])}]}]})):
        open(ns["SNAP"], "w").write(json.dumps(bad))
        check("a layout with a %s is invalid" % name, ns["load_snapshot"]()[1][0] == "invalid")
    os.remove(ns["SNAP"])

    # a rejected file that cannot be copied aside is not replaced
    open(ns["SNAP"], "w").write("{not json")
    blocker = os.path.join(state, "blocker")
    open(blocker, "w").write("x")
    real_backups = ns["BACKUPS"]
    ns["BACKUPS"] = os.path.join(blocker, "backups")
    try:
        ns["write_snapshot"](two, None)
        raised = False
    except OSError:
        raised = True
    ns["BACKUPS"] = real_backups
    check("a rejected layout.json that cannot be copied aside is left alone", raised and open(ns["SNAP"]).read() == "{not json")
    os.remove(ns["SNAP"])

    # temp files are per process and leftovers are cleaned up
    ns["write_snapshot"](two, None)
    check("a save leaves no temp file behind", not glob.glob(ns["SNAP"] + ".*tmp"))
    stale_tmp = ns["SNAP"] + ".4242.tmp"
    open(stale_tmp, "w").write("x")
    os.utime(stale_tmp, (0, 0))
    fresh_tmp = ns["SNAP"] + ".4243.tmp"
    open(fresh_tmp, "w").write("x")
    ns["prune_tmp"]()
    check("an old leftover temp file is removed", not os.path.exists(stale_tmp))
    check("a recent temp file is kept", os.path.exists(fresh_tmp))
    os.remove(fresh_tmp)

    # the Codex day folder listing never touches old history
    for m in range(1, 13):
        for d in range(1, 29):
            os.makedirs(os.path.join(codex_dir, "2025", "%02d" % m, "%02d" % d))
    listed = []
    real_listdir = os.listdir
    os.listdir = lambda path: (listed.append(path), real_listdir(path))[1]
    try:
        folders = ns["newest_day_folders"](30)
    finally:
        os.listdir = real_listdir
    check("the newest day folders come from the newest months", len(folders) == 30 and all("/2026/" in f for f in folders))
    check("old years are never listed", not any(p.rstrip("/").endswith(("2025",) ) or "/2025/" in p for p in listed[1:]), str(listed)[:200])
    check("only a handful of folders are listed", len(listed) <= 5, str(len(listed)))

    # shell panes that iTerm2 names in a way we do not recognize raise a count, busy panes do not
    import asyncio as _asyncio

    class FakeSession:
        def __init__(self, job, tty):
            self.job, self.tty, self.typed = job, tty, []

        async def async_get_variable(self, name):
            return {"jobName": self.job, "tty": self.tty}[name]

        async def async_send_text(self, text):
            self.typed.append(text)

    class FakeAsyncio:
        async def sleep(self, seconds):
            return None

    ns["asyncio"] = FakeAsyncio()
    ns["DRY"] = False
    cmds = {"/dev/ttyA": {"zsh"}, "/dev/ttyB": {"claude", "zsh"}, "/dev/ttyC": {"-bash"}}

    async def drive(job, tty):
        stats = {"unknown_job": 0, "shell_misnamed": 0, "cmds": cmds}
        s = FakeSession(job, tty)
        kind = await ns["run_in_pane"](s, {"name": "t", "cwd": "/tmp"}, set(), stats)
        return kind, stats, s

    kind, stats, s = _asyncio.run(drive("zsh (~)", "/dev/ttyA"))
    check("a renamed shell job is skipped, counted and nothing is typed", kind is None and stats["shell_misnamed"] == 1 and not s.typed)
    kind, stats, s = _asyncio.run(drive("2.1.314", "/dev/ttyB"))
    check("a busy pane is skipped without being counted as a renamed shell", kind is None and stats["shell_misnamed"] == 0 and not s.typed)
    kind, stats, s = _asyncio.run(drive("-bash", "/dev/ttyC"))
    check("a plain shell is typed into", kind == "cd" and s.typed == ["cd /tmp\r"])
    kind, stats, s = _asyncio.run(drive(None, "/dev/ttyA"))
    check("a job that never appears is counted as unknown", kind is None and stats["unknown_job"] == 1 and not s.typed)
    ns["asyncio"] = _asyncio


    # the incident: Grok runs the same hook in the same tab and writes its own session id
    projects = os.path.join(state, "claude-projects")
    os.makedirs(os.path.join(projects, "-some-folder"))
    open(os.path.join(projects, "-some-folder", "live-aaaaaaaa.jsonl"), "w").write("{}\n")
    ns["CLAUDE_PROJECTS"] = projects
    heard.clear()
    foreign = {"session_id": "grok-iiiiiiii", "cwd": "/y", "saved_at": now - 10}  # newer than the process, so the old rule trusted it
    r = pick(foreign, live, "t7")
    check("a hook record Claude has no conversation file for loses to Claude's own file", r["sessionId"] == "live-aaaaaaaa", str(r))
    check("and it raises an alarm naming the likely cause", any("Grok or Codex" in m for m in heard))
    open(os.path.join(projects, "-some-folder", "hook-bbbbbbbb.jsonl"), "w").write("{}\n")
    heard.clear()
    r = pick({"session_id": "hook-bbbbbbbb", "cwd": "/y", "saved_at": now - 10}, live, "t8")
    check("a second Claude in the same tab (its id has a conversation file too) does not win over the live file", r["sessionId"] == "live-aaaaaaaa", str(r))
    check("and it raises an alarm about a second Claude", any("second Claude" in m for m in heard))
    ns["CLAUDE_PROJECTS"] = os.path.join(state, "no-such-folder")
    check("when no conversation files can be found at all the live file still wins", pick(foreign, live, "t9")["sessionId"] == "live-aaaaaaaa")
    ns["CLAUDE_PROJECTS"] = projects
    young = {"sessionId": "live-aaaaaaaa", "cwd": "/x", "started": now - 5}
    heard.clear()
    r = pick({"session_id": "hook-bbbbbbbb", "cwd": "/y", "saved_at": now - 3600}, young, "t10")
    check("a Claude that just started is not called stale, the hook may not have fired yet", r["sessionId"] == "live-aaaaaaaa" and not heard, str(heard))

    ns["asyncio"] = FakeAsyncio()
    ns["live_session_ids"] = lambda: set()

    async def drive_claude(session_id):
        s = FakeSession("zsh", "/dev/ttyA")
        stats = {"unknown_job": 0, "shell_misnamed": 0, "cmds": {}}
        kind = await ns["run_in_pane"](s, {"name": "t", "cwd": "/tmp", "claude": {"sessionId": session_id, "cwd": "/tmp"}}, set(), stats)
        return kind, s

    heard.clear()
    kind, s = _asyncio.run(drive_claude("grok-iiiiiiii"))
    check("an id with no conversation file is never typed as claude --resume", kind == "cd" and s.typed == ["cd /tmp\r"], str(s.typed))
    check("a tab with no conversation file does not raise an alarm", not any("conversation file" in m for m in heard))
    check("but it leaves a note in the log", "has no conversation file for id grok-iii" in open(log).read())
    s2 = FakeSession("zsh", "/dev/ttyA")
    kind = _asyncio.run(ns["run_in_pane"](s2, {"name": "t", "claude": {"sessionId": "grok-iiiiiiii"}}, set(), {"unknown_job": 0, "shell_misnamed": 0, "cmds": {}}))
    check("with no folder either, nothing is typed, not even a bare Enter", kind is None and s2.typed == [])
    kind, s = _asyncio.run(drive_claude("live-aaaaaaaa"))
    check("an id with a conversation file is typed", kind == "claude" and s.typed == ["cd /tmp && claude --resume live-aaaaaaaa\r"], str(s.typed))
    ns["asyncio"] = _asyncio

    # the doctor spots a running Claude whose tab record names a different conversation
    import io, contextlib
    import time as _t
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=dict(os.environ, ITERM_SESSION_ID="w0t0p0:DOCTAB-1"))  # not a system binary, so ps can show its environment
    _t.sleep(0.5)
    if ns["tab_of_pid"](child.pid) != "DOCTAB-1":
        print("note: ps cannot show this Python's environment, using a stand-in for the tab lookup")
        ns["tab_of_pid"] = lambda pid: "DOCTAB-1"
    try:
        hooks_dir = os.path.join(state, "hooks")
        os.makedirs(hooks_dir)
        json.dump({"session_id": "grok-iiiiiiii", "saved_at": now}, open(os.path.join(hooks_dir, "DOCTAB-1.json"), "w"))
        ns["HOOKS"] = hooks_dir
        ns["live_claude_sessions"] = lambda: [{"pid": child.pid, "sessionId": "live-aaaaaaaa", "cwd": "/x", "kind": "interactive", "tty": None, "started": now - 1000}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns["doctor"]()
        out = buf.getvalue()
        check("the doctor warns when a tab's hook record disagrees with the running Claude", "DOCTAB-1" in out and "says grok-iii" in out and "WARN" in out, out[-500:])
        json.dump({"session_id": "live-aaaaaaaa", "saved_at": now}, open(os.path.join(hooks_dir, "DOCTAB-1.json"), "w"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns["doctor"]()
        check("and stays quiet when they agree", "says grok-iii" not in buf.getvalue() and "the hook record for that tab" not in buf.getvalue())
    finally:
        child.kill()

    # round 2 additions: ps that hangs, a pick without debug leftovers, young Claudes, kinds, names, missing conversation files
    class HangingSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired
        SubprocessError = subprocess.SubprocessError

        @staticmethod
        def run(*a, **k):
            raise subprocess.TimeoutExpired(a, 1)

    real_sub = ns["subprocess"]
    ns["subprocess"] = HangingSubprocess
    try:
        check("a ps that hangs gives an empty answer instead of stalling", ns["ps_output"](["-axo", "pid="]) == "")
    finally:
        ns["subprocess"] = real_sub
    check("a hanging ps is logged", "ps did not answer" in open(log).read())
    check("pick_claude leaves no unused debug field behind", "hook_said" not in pick(fresh, live, "t11"))

    child2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], env=dict(os.environ, ITERM_SESSION_ID="w0t0p0:DOCTAB-2"))
    _t.sleep(0.5)
    if ns["tab_of_pid"](child2.pid) != "DOCTAB-2":
        ns["tab_of_pid"] = lambda pid: "DOCTAB-2"
    try:
        hooks2 = os.path.join(state, "hooks2")
        os.makedirs(hooks2)
        json.dump({"session_id": "grok-iiiiiiii", "saved_at": now}, open(os.path.join(hooks2, "DOCTAB-2.json"), "w"))
        ns["HOOKS"] = hooks2

        def doctor_out(entries):
            ns["live_claude_sessions"] = lambda: entries
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                ns["doctor"]()
            return buffer.getvalue()

        entry = {"pid": child2.pid, "sessionId": "live-aaaaaaaa", "cwd": "/x", "kind": "interactive", "tty": "/dev/ttys9", "started": now - 1000}
        ns["CLAUDE_PROJECTS"] = projects
        check("the doctor mismatch warning fires for an old enough Claude", "says grok-iii" in doctor_out([entry]))
        check("the doctor skips a Claude younger than the grace period", "says grok-iii" not in doctor_out([dict(entry, started=now - 5)]))
        check("the doctor warns when a running Claude is not named claude", "is named" in doctor_out([entry]) and "not claude" in doctor_out([entry]))
        out = doctor_out([dict(entry, kind="headless")])
        check("the doctor warns when no session file says interactive", "no session file says kind interactive" in out and "headless" in out)
        ns["CLAUDE_PROJECTS"] = os.path.join(state, "no-such-folder")
        check("the doctor warns when no conversation files can be found", "no conversation files can be found" in doctor_out([entry]))
        ns["CLAUDE_PROJECTS"] = projects
        check("and stays quiet about that when they can", "no conversation files can be found" not in doctor_out([entry]))
    finally:
        child2.kill()

    # several Claude accounts: each tab resumes on the config folder it ran on (accounts.json, see the README)
    ans = {"__name__": "qa_accounts"}
    exec(compile(head, "restore_head", "exec"), ans)
    ans["notify"] = lambda m: None
    work_dir = os.path.join(state, "acct-work")
    pers_real = os.path.join(state, "acct-personal-real")
    pers_link = os.path.join(state, "acct-personal")
    for d in (work_dir, pers_real):
        os.makedirs(os.path.join(d, "sessions"))
        os.makedirs(os.path.join(d, "projects", "-f"))
    os.symlink(pers_real, pers_link)
    pr = os.path.realpath(pers_real)
    ans["SESSIONS"] = os.path.join(work_dir, "sessions")
    ans["CLAUDE_PROJECTS"] = os.path.join(work_dir, "projects")
    acc_file = os.path.join(state, "accounts.json")
    ans["ACCOUNTS_FILE"] = acc_file
    write_accounts = lambda obj: open(acc_file, "w").write(obj if isinstance(obj, str) else json.dumps(obj))
    personal = {"config_dir": pers_link, "command": "claude-personal"}
    pid_uid = "pers-jjjjjjjj"

    check("no accounts file: only the default account", [a["command"] for a in ans["accounts"]()] == ["claude"])
    check("no accounts file: a recorded personal folder is ignored and claude resumes as before",
          shlex.split(ans["resume_command"]({"cwd": "/tmp", "claude": {"sessionId": uid, "config_dir": pr}})) == ["cd", "/tmp", "&&", "claude", "--resume", uid])
    write_accounts({"accounts": [personal]})
    check("an accounts entry loads", [a["command"] for a in ans["accounts"]()] == ["claude", "claude-personal"])
    check("a symlinked config folder matches its target", ans["account_for"](pers_real)["command"] == "claude-personal" and ans["account_for"](pers_link)["command"] == "claude-personal")
    cmd_p = ans["resume_command"]({"cwd": "/tmp", "claude": {"sessionId": uid, "config_dir": pr}})
    check("a personal pane resumes with its own command, as safe tokens", shlex.split(cmd_p) == ["cd", "/tmp", "&&", "claude-personal", "--resume", uid], cmd_p)
    check("a pane with no config folder resumes with claude", "&& claude --resume" in ans["resume_command"]({"cwd": "/tmp", "claude": {"sessionId": uid}}))
    check("a pane on the default folder resumes with claude", "&& claude --resume" in ans["resume_command"]({"cwd": "/tmp", "claude": {"sessionId": uid, "config_dir": work_dir}}))
    for odd in ("/somewhere/else", 123, "relative/path", "/tmp/a\x1bb", ["x"], ""):
        check("an unknown or malformed config folder %r restores the folder only" % (odd,), ans["resume_plan"]({"cwd": "/tmp", "claude": {"sessionId": uid, "config_dir": odd}}) == ("cd /tmp", "cd"))
    for bad in ("claude; rm -rf ~", "-x", "a b", "claude\n", "", 7, None, "/usr/local/bin/claude", "$(id)", "x" * 65):
        write_accounts({"accounts": [{"config_dir": pers_link, "command": bad}]})
        check("an unsafe command %r is never loaded" % (bad,)[:60], len(ans["accounts"]()) == 1)
    for garbage in ("{not json", "[]", '{"accounts": "x"}', '{"accounts": [1, null]}', '{"accounts": [{"command": "claude-personal"}]}'):
        write_accounts(garbage)
        check("a malformed accounts file %r is ignored" % garbage, len(ans["accounts"]()) == 1)
    check("a bad accounts file is logged", "accounts file" in open(log).read())
    write_accounts({"accounts": [{"config_dir": work_dir, "command": "claude-dup"}, personal, {"config_dir": pers_real, "command": "claude-other"}]})
    check("entries for the default folder or a folder already listed are skipped, the first one wins", [a["command"] for a in ans["accounts"]()] == ["claude", "claude-personal"])
    write_accounts({"accounts": [personal]})

    # live sessions, the double-typing guard and the tab lookup cover every account
    child3 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        json.dump({"pid": os.getpid(), "sessionId": "work-kkkkkkkk", "kind": "interactive", "cwd": "/w"}, open(os.path.join(work_dir, "sessions", "%d.json" % os.getpid()), "w"))
        json.dump({"pid": child3.pid, "sessionId": pid_uid, "kind": "interactive", "cwd": "/p"}, open(os.path.join(pers_real, "sessions", "%d.json" % child3.pid), "w"))
        _t.sleep(0.3)
        found = {s["sessionId"]: s for s in ans["live_claude_sessions"]()}
        check("live sessions are found in every account", "work-kkkkkkkk" in found and pid_uid in found, str(sorted(found)))
        check("a live session on an extra account carries its folder, a default one does not",
              found.get(pid_uid, {}).get("config_dir") == pr and "config_dir" not in found.get("work-kkkkkkkk", {"config_dir": 1}))
        check("the double-typing guard sees every account", {"work-kkkkkkkk", pid_uid} <= ans["live_session_ids"]())
    finally:
        child3.kill()
    real_live_a = ans["live_claude_sessions"]
    ans["live_claude_sessions"] = lambda: [{"sessionId": pid_uid, "cwd": "/p", "kind": "interactive", "tty": "/dev/ttysP", "started": 100, "config_dir": pr}]
    check("the tab lookup keeps the account", ans["claude_by_tty"]()["/dev/ttysP"].get("config_dir") == pr)
    ans["live_claude_sessions"] = real_live_a

    # conversation files are looked up in the tab's own account
    for folder, sid in ((pers_real, pid_uid), (work_dir, "work-kkkkkkkk"), (work_dir, "both-llllllll"), (pers_real, "both-llllllll")):
        open(os.path.join(folder, "projects", "-f", sid + ".jsonl"), "w").write("{}\n")
    check("a personal conversation is found in its own account", not ans["known_missing"](pid_uid, pr))
    check("and not in the default account", ans["known_missing"](pid_uid))
    check("a work conversation is not found in the personal account", ans["known_missing"]("work-kkkkkkkk", pr) and not ans["known_missing"]("work-kkkkkkkk"))
    check("the same id in both accounts resolves in each", not ans["known_missing"]("both-llllllll") and not ans["known_missing"]("both-llllllll", pr))
    check("a folder no account lists counts as missing", ans["known_missing"](pid_uid, "/somewhere/else"))

    # which account a tab is on: the live file decides, like the conversation id
    apick = ans["pick_claude"]
    t_now = _time.time()
    live_p = {"sessionId": pid_uid, "cwd": "/p", "started": t_now - 1000, "config_dir": pr}
    live_w = {"sessionId": "work-kkkkkkkk", "cwd": "/w", "started": t_now - 1000}
    check("pick: the live file's account is kept", apick(None, live_p, "a1").get("config_dir") == pr)
    check("pick: a hook record agreeing with a personal live file keeps the account", apick({"session_id": pid_uid, "cwd": "/p", "config_dir": pr}, live_p, "a2").get("config_dir") == pr)
    check("pick: a hook record claiming another account loses to the live file", "config_dir" not in apick({"session_id": "work-kkkkkkkk", "cwd": "/w", "config_dir": pr}, live_w, "a3"))
    check("pick: an old hook record with no folder takes the account from the live file", apick({"session_id": pid_uid, "cwd": "/p"}, live_p, "a3b").get("config_dir") == pr)
    check("pick: with no live file the hook record's folder is used", apick({"session_id": pid_uid, "cwd": "/p", "config_dir": pr}, None, "a4").get("config_dir") == pr)
    check("pick: a record without a folder means the default account", "config_dir" not in apick({"session_id": pid_uid, "cwd": "/p"}, None, "a5"))
    check("pick: a non-string folder in a hook record is dropped", "config_dir" not in apick({"session_id": pid_uid, "cwd": "/p", "config_dir": ["x"]}, None, "a6"))

    # typing into a pane, end to end
    ans["asyncio"] = FakeAsyncio()
    ans["DRY"] = False

    async def drive_account(claude, running=()):
        ans["live_session_ids"] = lambda: set(running)
        s = FakeSession("zsh", "/dev/ttyA")
        kind = await ans["run_in_pane"](s, {"name": "t", "cwd": "/tmp", "claude": dict(claude, cwd="/tmp")}, set(), {"unknown_job": 0, "shell_misnamed": 0, "cmds": {}})
        return kind, s.typed

    got = _asyncio.run(drive_account({"sessionId": pid_uid, "config_dir": pr}))
    check("a personal tab is resumed with claude-personal", got == ("claude", ["cd /tmp && claude-personal --resume %s\r" % pid_uid]), str(got))
    got = _asyncio.run(drive_account({"sessionId": "work-kkkkkkkk"}))
    check("a work tab is resumed with claude", got == ("claude", ["cd /tmp && claude --resume work-kkkkkkkk\r"]), str(got))
    got = _asyncio.run(drive_account({"sessionId": pid_uid, "config_dir": pr}, [pid_uid]))
    check("a personal conversation already running in another tab is not typed again", got == (None, []), str(got))
    got = _asyncio.run(drive_account({"sessionId": pid_uid, "config_dir": "/somewhere/else"}))
    check("a tab on a folder no account lists gets its folder only", got == ("cd", ["cd /tmp\r"]), str(got))
    check("and the log says why without repeating the folder", "does not list" in open(log).read() and "/somewhere/else" not in open(log).read())
    ans["asyncio"] = _asyncio

    # the doctor knows the extra account and reads its session files
    def doctor_accounts():
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ans["doctor"]()
        return buffer.getvalue()

    for f in glob.glob(os.path.join(work_dir, "sessions", "*.json")):
        os.remove(f)
    json.dump({"pid": 1, "sessionId": pid_uid, "kind": "interactive", "cwd": "/p", "procStart": "Fri Oct 30 16:19:47 2026"}, open(os.path.join(pers_real, "sessions", "1.json"), "w"))
    out = doctor_accounts()
    check("the doctor lists the extra account", "extra Claude account" in out and "claude-personal" in out, out[-400:])
    check("the doctor reads session files from every account", "no Claude session files found" not in out)
    write_accounts({"accounts": [{"config_dir": pers_link, "command": "bad cmd"}]})
    out = doctor_accounts()
    check("the doctor warns about an unusable accounts entry", "not usable" in out and "WARN" in out)
    write_accounts({"accounts": [{"config_dir": pers_link, "command": "no-such-command-qa"}]})
    check("the doctor warns when an account's command is not on PATH", "not on PATH" in doctor_accounts())
    os.remove(acc_file)
    check("the doctor says nothing about accounts when there is no file", "account" not in doctor_accounts().lower().replace("accounts file does not list", ""))

# the hook only records for Claude: walk a fake process table
hook_src = open(os.path.join(BASE, "hook.py")).read().split("\ntry:\n    main()")[0]
hns = {"__name__": "hook_qa"}
exec(compile(hook_src, "hook_head", "exec"), hns)


def fake_ps(table, commands):
    class Result:
        def __init__(self, text):
            self.stdout = text

    class Fake:
        @staticmethod
        def run(args, **kw):
            if args[:3] == ["ps", "-axo", "pid=,ppid=,comm="]:
                return Result("".join("%d %d %s\n" % (pid, parent, name) for pid, (parent, name) in table.items()))
            if args[:3] == ["ps", "-o", "command="]:
                return Result(commands.get(int(args[-1]), "") + "\n")
            raise AssertionError("unexpected ps call %r" % (args,))
    return Fake


real_getppid = os.getppid
os.getppid = lambda: 100
try:
    for label, table, commands, want in (
            ("Claude running the hook", {100: (90, "sh"), 90: (1, "/Users/x/.local/bin/claude")}, {90: "claude --resume abc"}, "claude"),
            ("a headless Claude run with -p", {100: (90, "sh"), 90: (1, "claude")}, {90: "claude -p review this"}, "claude-headless"),
            ("a headless Claude run with --print", {100: (90, "sh"), 90: (1, "claude")}, {90: "claude --print review this"}, "claude-headless"),
            ("Grok started by a Claude command", {100: (90, "sh"), 90: (80, "grok"), 80: (70, "python3"), 70: (60, "bash"), 60: (1, "claude")}, {60: "claude"}, "grok"),
            ("Codex running the hook", {100: (90, "sh"), 90: (1, "codex")}, {}, "codex"),
            ("a process tree with no agent in it", {100: (90, "sh"), 90: (1, "bash")}, {}, None),
            ("a ps that answers nothing", {}, {}, None)):
        hns["subprocess"] = fake_ps(table, commands)
        check("host_agent: %s" % label, hns["host_agent"]() == want, str(hns["host_agent"]()))

    class Broken:
        @staticmethod
        def run(*a, **k):
            raise OSError("ps is gone")
    hns["subprocess"] = Broken
    check("host_agent: a ps that fails means unknown, not a crash", hns["host_agent"]() is None)
finally:
    os.getppid = real_getppid

# end to end: run the real hook under a process literally named grok, then claude
# (a copy of Python with that file name is the fake parent: copied macOS system binaries get killed, Python does not)
import shutil
with tempfile.TemporaryDirectory() as work:
    home = os.path.join(work, "home")
    os.makedirs(home)
    payload = os.path.join(work, "payload.json")
    open(payload, "w").write(json.dumps({"session_id": "agent-session-1", "cwd": "/x", "source": "new"}))
    cases = (("grok", False, []), ("claude", True, []), ("claude", False, ["-p"]), ("codex", False, []))
    os.makedirs(os.path.join(work, "probe"))
    probe = os.path.join(work, "probe", "python-copy")
    shutil.copy(sys.executable, probe)
    # the copy must run AND keep its file name as the process name (Homebrew's python3 re-launches Python.app, which renames it)
    probe_run = subprocess.run([probe, "-c", "import os, subprocess; print(subprocess.run(['ps', '-o', 'comm=', '-p', str(os.getpid())], capture_output=True, text=True).stdout.strip().split('/')[-1])"],
                               env=dict(os.environ, PYTHONHOME=sys.base_prefix), capture_output=True, text=True)
    if probe_run.stdout.strip() != "python-copy":
        print("skip the end to end hook checks: a copy of this Python cannot run here under its own name (got %r)" % probe_run.stdout.strip())
        cases = ()
    for number, (name, recorded, extra) in enumerate(cases):
        os.makedirs(os.path.join(work, "p%d" % number))  # one folder each: macOS kills a signed binary copied over one it already ran
        fake = os.path.join(work, "p%d" % number, name)
        shutil.copy(sys.executable, fake)
        code = ("import os, subprocess, sys\n"
                "r = subprocess.run([%r, '-I', %r], stdin=open(%r))\n"
                "me = subprocess.run(['ps', '-o', 'comm=', '-p', str(os.getpid())], capture_output=True, text=True).stdout.strip().split('/')[-1]\n"
                "print('hook exit', r.returncode, 'parent named', me)\n") % (sys.executable, os.path.join(BASE, "hook.py"), payload)
        r = subprocess.run([fake, "-c", code] + extra, env=dict(os.environ, HOME=home, PYTHONHOME=sys.base_prefix, ITERM_SESSION_ID="w0t0p0:E2E-TAB"), capture_output=True, text=True, timeout=60)
        name = name + (" -p" if extra else "")
        check("end to end: the fake %s parent really ran and the hook exited 0" % name, "hook exit 0 parent named %s" % name.split()[0] in r.stdout, (r.stdout + r.stderr)[-200:])
        rec = os.path.join(home, ".claude", "iterm-restore", "hooks", "E2E-TAB.json")
        wrote = os.path.exists(rec)
        check("end to end: the hook %s a record under a process named %s" % ("writes" if recorded else "ignores", name), wrote == recorded)
        if wrote:
            os.remove(rec)

# hook errors are recorded, and the doctor runs without iTerm2
with tempfile.TemporaryDirectory() as home:
    env = dict(os.environ, HOME=home, ITERM_SESSION_ID="w0t0p0:ERR-1")
    subprocess.run([sys.executable, os.path.join(BASE, "hook.py")], input="not json", capture_output=True, text=True, env=env)
    check("a hook failure is written to hook-errors.log", os.path.exists(os.path.join(home, ".claude", "iterm-restore", "hook-errors.log")))
def tree(root):
    out = {}
    for p, _, fs in os.walk(root):
        for f in fs:
            full = os.path.join(p, f)
            try:
                st = os.stat(full)
                out[full] = (st.st_size, st.st_mtime)
            except OSError:
                pass
    return out


bad_state = tempfile.mkdtemp()
open(os.path.join(bad_state, "layout.json"), "w").write("{not json")
open(os.path.join(bad_state, "restore.log"), "w").write("old line\n")
watched = [bad_state, os.path.join(os.path.expanduser("~"), ".claude", "sessions")]  # where a stray write by the doctor would land
before = [tree(w) for w in watched]
r = subprocess.run([sys.executable, os.path.join(BASE, "restore.py"), "--doctor"], capture_output=True, text=True, env=dict(os.environ, ITERM_RESTORE_STATE=bad_state))
after = [tree(w) for w in watched]
check("--doctor runs and reports", r.returncode in (0, 1) and "Python" in r.stdout and ("All good" in r.stdout or "warning" in r.stdout), r.stdout[-300:] + r.stderr[-300:])
check("--doctor changes no file anywhere it looks, even with a corrupt layout.json", before == after)
check("--doctor warns about the corrupt layout.json", "layout.json" in r.stdout and "WARN" in r.stdout)
plain = shutil.which("python3")
has_iterm2 = plain and subprocess.run([plain, "-c", "import iterm2"], capture_output=True).returncode == 0
if plain and not has_iterm2:
    r2 = subprocess.run([plain, "-I", os.path.join(BASE, "restore.py"), "--doctor"], capture_output=True, text=True, env=dict(os.environ, ITERM_RESTORE_STATE=tempfile.mkdtemp()))
    check("--doctor works with a Python that has no iterm2 module", r2.returncode in (0, 1) and "not importable here" in r2.stdout, r2.stdout[-300:] + r2.stderr[-300:])
    r3 = subprocess.run([plain, "-I", os.path.join(BASE, "restore.py"), "--once"], capture_output=True, text=True, timeout=60)
    check("restoring without the iterm2 module explains itself and exits", r3.returncode != 0 and "iterm2" in (r3.stdout + r3.stderr))
else:
    print("skip the no-iterm2 checks: python3 on this machine has the iterm2 module (or there is no python3)")

print("FAILED: %s" % failures if failures else "all checks passed")
sys.exit(1 if failures else 0)
