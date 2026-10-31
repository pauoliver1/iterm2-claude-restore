# Instructions for Claude Code

If the user asked you to install this project, follow the "Instructions for Claude" section of `README.md`, step by step.

Key rules, repeated here because they matter most:

- Show the user a plan first and get their OK before changing anything outside `~/.claude/iterm-restore/`.
- Back up every file you edit.
- Never quit or restart iTerm2 yourself. Ask the user to do it.
- Do not change the user's `tui` setting.
- Never print environment variables or credentials.

If you are working on this code instead, run `python3 tests/check.py` before and after your change. It touches only temporary folders.
