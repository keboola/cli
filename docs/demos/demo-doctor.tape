# Demo for docs/TUTORIAL.md §Prerequisites
# Shows `kbagent doctor` run against the live demo workspace:
# config + parseable + connectivity (per project) + CLI version +
# MCP server + conversation id + Claude Code plugin check.
#
# Regenerate: make demos (or: vhs docs/demos/demo-doctor.tape)

Output docs/assets/demo-doctor.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 820
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms
Set CursorBlink false
Set PlaybackSpeed 1.0

Env PATH "/Users/padak/github/keboola_agent_cli/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
Env PS1 "$ "

Sleep 700ms

Type "cd /tmp/kbagent-demo"
Sleep 300ms
Enter
Sleep 500ms

Type "kbagent doctor"
Sleep 400ms
Enter
Sleep 7000ms

Sleep 800ms
