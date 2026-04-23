# Hero demo for README.md
# Target: ~10s, shows the feel of using kbagent.
#
# Regenerate: make demos (or: vhs docs/demos/demo-hero.tape)

Output docs/assets/demo-hero.gif

# bash is more predictable under ttyd than zsh (no rc file surprises)
Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 720
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms
Set CursorBlink false
Set PlaybackSpeed 1.0

# Env prep: PATH for kbagent + short prompt + demo workspace.
# These run during ttyd startup before the first frame is captured.
Env PATH "/Users/padak/github/keboola_agent_cli/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
Env PS1 "$ "

# Give the shell a moment to settle after ttyd boots.
Sleep 700ms

Type "cd /tmp/kbagent-demo"
Sleep 300ms
Enter
Sleep 500ms

Type "kbagent --version"
Sleep 400ms
Enter
Sleep 1500ms

Type "kbagent project list"
Sleep 400ms
Enter
Sleep 3000ms

Type "kbagent config search --help"
Sleep 400ms
Enter
Sleep 2500ms

Sleep 800ms
