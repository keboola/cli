# Demo for docs/TUTORIAL.md §1 (add a single project).
# Shows: command signature via --help, then the resulting `project list`
# in the same workspace. We do not execute `project add` itself during
# the recording so no real token is typed or displayed.
#
# Regenerate: make demos (or: vhs docs/demos/demo-add-project.tape)

Output docs/assets/demo-add-project.gif

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

Type "kbagent project add --help"
Sleep 400ms
Enter
Sleep 3500ms

Type "clear"
Sleep 300ms
Enter
Sleep 200ms

Type "# Example invocation (not executed -- would expose a real token):"
Enter
Sleep 200ms
Type "#   kbagent project add --project prod \\"
Enter
Type "#     --url https://connection.keboola.com --token <YOUR_TOKEN>"
Enter
Sleep 200ms
Type "# After it completes, the project appears in the list:"
Enter
Sleep 400ms

Type "kbagent project list"
Sleep 400ms
Enter
Sleep 3000ms

Sleep 800ms
