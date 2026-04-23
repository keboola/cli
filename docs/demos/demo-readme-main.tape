# Main demo for README.md ("30-second demo" section).
# Shows a typical read-only workflow against a multi-project workspace:
# list projects -> inspect the active one -> browse how to search configs.
# Nothing calls the live Keboola API (all-offline reads from config + help).
#
# Regenerate: make demos (or: vhs docs/demos/demo-readme-main.tape)

Output docs/assets/demo-readme-main.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 720
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

Type "kbagent project list"
Sleep 400ms
Enter
Sleep 3000ms

Type "kbagent project current"
Sleep 400ms
Enter
Sleep 2500ms

Type "kbagent config search --help"
Sleep 400ms
Enter
Sleep 3000ms

Sleep 800ms
