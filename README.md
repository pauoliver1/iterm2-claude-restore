# iterm2-claude-restore

Quit iTerm2 or restart your Mac, and everything comes back: the same windows, tabs and split panes, with every Claude Code session (and Codex session) resumed in its own conversation and folder.

After setup you do nothing special. Close iTerm2 any way you like, reopen it, wait about 30 seconds, and your whole workspace is back. A notification tells you how many sessions were resumed.

It is a small Python script that runs inside iTerm2, plus a small Claude Code hook that notes which conversation is open in which tab. Everything stays on your Mac.

## Install it with Claude Code

Give Claude Code this prompt:

```
Install https://github.com/pauoliver1/iterm2-claude-restore for me. Read its README and follow the "Instructions for Claude" section. Adapt each step to my setup. Show me your plan before you change anything, and do not quit iTerm2 yourself.
```

Claude reads the steps below, checks your setup, makes the changes and runs a health check. You do two things: tick one iTerm2 setting if it is off, and quit and reopen iTerm2 once to test.

## Instructions for Claude

You are Claude Code and the user gave you this repository's URL. Follow the steps in order, adapt them to what you find on their machine, and report the result of each step.

**Ground rules**

1. Show the user a short plan first, and wait for their OK before you change anything outside `~/.claude/iterm-restore/`.
2. Before you edit a file, copy it to `<name>.bak-<YYYYMMDD-HHMMSS>` (never overwrite an existing backup) and tell the user where the backup is.
3. Never quit, restart or close iTerm2 yourself. That would end your own session. Ask the user to do it.
4. Do not change the user's `tui` setting or any other Claude Code setting that is not listed below. Changing `tui` while sessions are running can blank them.
5. Never print environment variables, tokens or the contents of credential files.
6. If a check fails and you cannot fix it safely, stop and explain what you found.

**Step 1: check the requirements**

- iTerm2 is installed and is the terminal in use. Run `ls -d /Applications/iTerm.app` and `echo ${ITERM_SESSION_ID:+yes}` (it should print `yes`). Read the version with `defaults read /Applications/iTerm.app/Contents/Info CFBundleShortVersionString`. It was tested on 3.7.4. Warn the user if theirs is older, because that is untested.
- Python 3.7 or newer is available: `python3 --version`. iTerm2 runs the main script with its own Python runtime. The hook and the doctor use the `python3` on the PATH.
- Claude Code is installed: `claude --version`. It was tested on 2.1.310 to 2.1.316.
- Codex is optional. Tabs running `codex` get restored too when it is installed.
- This only works on macOS with iTerm2. Terminal.app, VS Code terminals and tmux control mode (`tmux -CC`) are not supported.

**Step 2: check the iTerm2 settings (read only, the user fixes them in the UI)**

- The Python API must be on. `defaults read com.googlecode.iterm2 EnableAPIServer` should print `1`. If not, ask the user to open iTerm2 `Settings > General > Magic` and tick **Enable Python API**. Menu wording can differ a little between iTerm2 versions.
- Window restoration must be on. `defaults read com.googlecode.iterm2 OpenNoWindowsAtStartup` and `defaults read com.googlecode.iterm2 OpenArrangementAtStartup` should each print `0` or report that the key does not exist. `defaults read NSGlobalDomain NSQuitAlwaysKeepsWindows` should print `1` or report that the key does not exist. If a value is wrong, ask the user to set `Settings > General > Startup > Window restoration policy` to **Use System Window Restoration Setting**, and to turn **off** "Close windows when quitting an application" in macOS `System Settings > Desktop & Dock`. iTerm2 explains why at https://iterm2.com/restoration.html.

**Step 3: install the files**

The files must live in exactly `~/.claude/iterm-restore/`, because the scripts use that path for their saved state.

- If the folder does not exist: `git clone https://github.com/pauoliver1/iterm2-claude-restore ~/.claude/iterm-restore`. If the user has no git, download `restore.py` and `hook.py` from this repository into that folder instead. `tests/check.py` is then missing, so skip the optional test.
- If the folder exists, look at what is in it and ask the user before you change anything.
- Optional: run `python3 ~/.claude/iterm-restore/tests/check.py`. It touches only temporary folders and prints `all checks passed` when everything is fine.
- To update later: `git -C ~/.claude/iterm-restore pull`. The runtime files (saved layout, hook records, backups, logs) are ignored by git.

**Step 4: install the launcher**

iTerm2 runs every script in its AutoLaunch folder when it starts. Find the right folder:

- The default is `~/Library/Application Support/iTerm2/Scripts/AutoLaunch/`. On iTerm2 3.7.4, `~/.config/iterm2/AppSupport` is a symlink to `~/Library/Application Support/iTerm2`, so both paths are the same folder.
- If the user already has AutoLaunch scripts, run `ps -axo command | grep AutoLaunch` and use the folder they are running from.
- If the folder does not exist, create it.

Create `claude_restore.py` there with exactly this content. If a file with that name already exists and differs, show the difference and ask.

```python
#!/usr/bin/env python3
# Launcher only. The real script lives in ~/.claude/iterm-restore/restore.py
import os
import runpy

runpy.run_path(os.path.expanduser("~/.claude/iterm-restore/restore.py"), run_name="__main__")
```

iTerm2 may ask to install its Python runtime or to allow the script. The user should accept. This prompt flow is from memory and was not tested on a fresh Mac.

**Step 5: register the Claude Code hook**

The hook tells the script which conversation is in which tab. It goes in the user's global Claude Code settings, `~/.claude/settings.json`.

1. If the file does not exist, start from `{}`. Otherwise back it up (ground rule 2). If it is not valid JSON, stop and ask.
2. Under `hooks`, add this object to the `SessionStart` list. Keep every existing hook and every other key. If the file has no `hooks` key, or `hooks` has no `SessionStart` list, create them with the same nesting (`hooks`, then `SessionStart`, then the object below). If any existing command already contains `iterm-restore/hook.py`, do nothing.

```json
{
  "hooks": [
    {
      "type": "command",
      "command": "<PYTHON> <HOME>/.claude/iterm-restore/hook.py",
      "timeout": 10
    }
  ]
}
```

   Replace `<PYTHON>` with the full path printed by `command -v python3`, and `<HOME>` with the full path of the user's home folder. Use full paths, not `~`, because `~` inside a hook command was not checked.
3. Write the file back with the same indentation, then show the user the difference.

Only Claude sessions that start after this are recorded by the hook. Sessions already running are still found through Claude Code's own session files.

**Step 6: start the script**

The script starts when iTerm2 starts, so the user has to quit and reopen iTerm2 once (`⌘Q`). Tell them this ends their running terminal sessions, and that Claude sessions can be resumed with `claude --resume`. Wait for their OK, then ask them to do it. It saves your layout every 30 seconds, so nothing is saved until it has run for about half a minute. That means a quit and reopen within the first half minute may restore nothing, so wait a minute before Step 7 and treat a later quit as the real test.

**Step 7: verify with the doctor**

Run `python3 ~/.claude/iterm-restore/restore.py --doctor`. It changes nothing, works with any Python 3 and does not need iTerm2 to be open. Expect OK lines for the hook, the session files, the saved layout and the running script. If iTerm2 is closed it reports the launcher as INFO or WARN instead of the running script. The saved layout is a WARN until the script has run for about 30 seconds. A "no hook records yet" warning is normal until the user opens a new Claude tab. Then open a new iTerm2 tab, start `claude`, and check that `~/.claude/iterm-restore/hooks/` now has a new `.json` file. See "Reading the doctor" below for what each warning means.

**Step 8: the real test (the user does it)**

Ask the user to open a few tabs and splits with Claude sessions that each have at least one message, wait a minute, press `⌘Q`, reopen iTerm2 and wait about half a minute. The windows, tabs and splits should be back, each Claude tab resumed in its own conversation, with a notification. If anything is off, read `~/.claude/iterm-restore/restore.log` and run the doctor again.

**Step 9: report**

Tell the user what you changed (files created, the `settings.json` difference and its backup), what is left (the real test), how to update and uninstall, and the caveats under "Limits and caveats".

## Uninstall

1. Delete `claude_restore.py` from the AutoLaunch folder.
2. Remove the `SessionStart` entry whose command contains `iterm-restore/hook.py` from `~/.claude/settings.json`.
3. Delete `~/.claude/iterm-restore/`.
4. Restart iTerm2.

## How it works

- **The hook.** Claude Code runs it each time a session starts. It saves one small file per iTerm2 tab, "this tab has this conversation". It ignores other programs that run the same hook in the same tab (Grok, Codex) and headless `claude -p` runs, because those are not the conversation the tab shows.
- **The script.** While iTerm2 is open it saves your layout every 30 seconds: every window, tab and split, the folder of each, and which conversation is in each. The conversation id comes from Claude Code's own session file for the Claude running in that tab. The hook record supplies the folder, and is used alone if that file is missing. If they disagree, Claude's own file wins and you get a warning.
- **At launch.** The script waits for the windows to settle and backs up the saved layout. If macOS already brought your tabs back, it types `cd <folder> && claude --resume <id>` into each Claude tab that is back at a shell prompt, and just `cd <folder>` into plain shell tabs. If iTerm2 opened one blank window instead, it rebuilds your windows, tabs and splits first. If what is on screen matches neither case, it changes nothing and tells you.
- **Codex.** A Codex tab is resumed with `codex resume <id>`. The id comes from the tab's launch command, otherwise from the newest Codex session with real content in that folder.
- **Safety.** It never types into a tab running anything other than a shell. It never resumes a Claude conversation that is already running somewhere else, and it checks the process start time so a reused process number cannot fool it. It only types absolute folder paths without control characters, and session ids made of letters, digits, dashes and underscores that do not start with a dash, all quoted. It checks that Claude has a conversation file for an id before typing `claude --resume`.
- **A restart kills running programs.** Nothing keeps Claude alive across a reboot. The script restarts each conversation with `claude --resume`, so you land back where you left off.
- **Cleanup.** The saved layout is rewritten only when something real changes, and is about 6 KB. A layout that gets smaller, such as a closed tab, is saved only after it stays smaller for one more round, and the old one is backed up first. It keeps at most 20 older copies: up to 10 safety copies and up to 10 periodic ones. `restore.log` is trimmed to its last 500 lines past 200 KB. Hook records older than 30 days for tabs that no longer exist are deleted. `hook-errors.log` is trimmed to its last 50 lines past 20 KB.
- **It says so when something breaks.** If an update to Claude Code, Codex or iTerm2 changes what it depends on, the script writes a WARNING to `restore.log` and shows a notification instead of failing silently.

## Several Claude accounts

Skip this unless you run more than one Claude account, each with its own config folder through `CLAUDE_CONFIG_DIR` (for example a `claude-work` command that sets `CLAUDE_CONFIG_DIR=~/.claude-work` and then runs `claude`). Without it, every tab resumes with plain `claude --resume` as described above.

List each extra folder and the command that starts Claude on it in `~/.claude/iterm-restore/accounts.json`. Your default `~/.claude` with `claude` is always included and needs no entry:

```json
{"accounts": [{"config_dir": "~/.claude-work", "command": "claude-work"}]}
```

- The hook records which config folder each tab's Claude ran on, and the script reads that account's session and conversation files. Each tab then resumes with its own command, for example `cd <folder> && claude-work --resume <id>`.
- `command` must be a plain program name on your `PATH`: letters, digits, dots, dashes and underscores, no spaces or slashes. It is typed into your terminal, so anything else is ignored.
- A tab whose Claude ran on a folder the file does not list gets only `cd <folder>`, never a guessed command.
- `--doctor` lists the extra accounts and warns about an entry it cannot use or a command it cannot find. The file is listed in `.gitignore`.
- Tabs opened before you add the file still resume on the right account while their Claude is running, because Claude's own session file in that folder says which one it is.

## Reading the doctor

`python3 ~/.claude/iterm-restore/restore.py --doctor` prints OK, INFO or WARN for each thing the tool depends on.

- **Hook not registered:** redo Step 5.
- **No hook records, or the hook may not be firing:** open a new Claude tab in iTerm2 and run it again, then look at `~/.claude/iterm-restore/hook-errors.log`. A Claude started outside iTerm2 can also cause this.
- **A running Claude's tab record names a different conversation:** another program or a second Claude in that tab wrote it. A restore uses Claude's own file, so nothing is lost.
- **Claude or Codex session files changed:** the hook still works for Claude, and Codex tabs only get their folder back unless they were launched with `codex resume <id>`. The script needs an update.
- **Process start time can no longer be read:** the reused-process check is off. Restores stay safe because it fails open, but the script needs an update.
- **A running Claude is not named `claude`, no session file says it is interactive, or no conversation files can be found:** Claude Code changed. The hook still records, but the protections that rely on those names and files are weaker. The script needs an update.
- **Script not running:** check the launcher is in the AutoLaunch folder and that the Python API is on, then restart iTerm2.
- **Problems in `restore.log`:** read its last lines. "No saved layout yet" only means wait a minute and run it again.

## Verified and not verified

Tested on one Apple silicon Mac: macOS 27, iTerm2 3.7.4, Claude Code 2.1.310 to 2.1.316, Python 3.12 (iTerm2's runtime) and 3.14 (the doctor).

Verified on that Mac:

- After `⌘Q` and reopen, and after a full Mac restart, macOS restored the tabs and splits and the script resumed every Claude session and the Codex tabs (11 Claude and 2 Codex sessions in the latest run). This worked with Claude Code's `"tui": "default"` screen mode, and the 2026-10-31 restore (13 resumable tabs) ran with `"tui": "fullscreen"`.
- Without the hook's check, a real Grok run in a tab overwrote that tab's record and a restart then resumed the wrong id. A real headless `claude -p` run in the same tab overwrote the record too. The current hook ignores both.
- The process start-time check recognized all running Claude sessions.
- The automated checks pass (`python3 tests/check.py`), including simulated corrupt files, newer file versions, unreadable start times, closing windows and runaway Codex history.

Not verified:

- **These install steps have not yet been followed on another person's Mac.** They were written from a working setup.
- The "rebuild" path, used when iTerm2 opens one blank window instead of restoring your tabs. It passed a self-test and a test that typed into brand new panes in a throwaway window, but it has not run on a real set of tabs.
- Whether Claude's own session file follows `/clear` or `/fork` inside a running tab. The script trusts that file over the hook, so if it lags, a restore resumes the conversation from before the clear.
- What happens after a real Claude Code, Codex or iTerm2 update that changes what the script reads. Tests simulate that, but no such update has happened yet.
- Whether `codex resume <id>` continues the same conversation file or starts a copy. Guides disagree.
- Other programs that run Claude's hooks are not recognized by name, though their ids still lose to Claude's own file. Only Grok has been tried for real.

## Limits and caveats

- **It reads undocumented files.** Claude Code's session files in `~/.claude/sessions/` and Codex's under `~/.codex/sessions/` are not a documented interface, so an update could change them. The hook is an official Claude Code feature and is the fallback.
- A tab you open in the last 30 seconds before quitting may be missed.
- Codex matching without a launch id is a best guess: the newest session with content in that folder, from the 30 most recent days that have sessions. Two Codex tabs in one folder without launch ids only get their folder back.
- It types commands into your terminal tabs when iTerm2 opens. Read `restore.py` first if that matters to you.
- Not affiliated with iTerm2, Anthropic, OpenAI or xAI.

## Files

- `restore.py`: the script iTerm2 runs. It also has `--once`, `--dry-run`, `--selftest` and `--doctor`.
- `hook.py`: the Claude Code `SessionStart` hook.
- `tests/check.py`: the automated checks. They touch only temporary folders.
- Created at runtime in `~/.claude/iterm-restore/`: `layout.json`, `hooks/`, `backups/`, `restore.log` and `hook-errors.log`.
- Optional, written by you: `accounts.json`, only if you run several Claude accounts (see above).

## License

MIT. See `LICENSE`.
