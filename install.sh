#!/bin/sh
# kbagent bootstrap installer.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | sh
#
# Installs the kbagent CLI from a PREBUILT wheel attached to the latest GitHub
# release -- no source build. This is the fast path for issue #353: building
# from `git+` recompiles the bundled React SPA via npm on every install, which
# takes 2-4 minutes on WSL2. A prebuilt wheel is a few-seconds download instead.
#
# Requirements: `uv` (the install backend) and `curl`. The guide installs uv a
# couple of steps before this. If no wheel asset exists for the latest release
# yet (older releases predate the release workflow), this falls back to the
# `git+` source build so the install still succeeds.
#
# Env knobs:
#   KBAGENT_NO_SERVER=1       install CLI-only (skip the [server] extras: FastAPI/
#                             uvicorn for `kbagent serve`). Default bundles them so
#                             `kbagent serve --ui` works out of the box.
#   KBAGENT_INSTALL_VERBOSE=1 show the full `uv tool install` output (the resolved
#                             dependency list) instead of a single progress line.
#                             Also enabled with the --verbose / -v argument.
#   NO_COLOR=1                disable ANSI colour (https://no-color.org).

set -eu

REPO="keboola/cli"
PKG="keboola-cli"
DIST="keboola_cli" # normalized distribution name used in the wheel filename

# ---------------------------------------------------------------------------
# Presentation helpers (issue: first-run UX -- "quiet the wall + brand + next
# steps"). Everything here writes to stderr, same as info(), so a caller that
# redirects stdout (e.g. `| tee`) still sees the human-facing output and the
# stdout stream stays clean.
# ---------------------------------------------------------------------------

# Verbose: env var OR --verbose/-v argument (supports `... | sh -s -- --verbose`).
VERBOSE=0
case "${KBAGENT_INSTALL_VERBOSE:-}" in 1 | true | yes | on) VERBOSE=1 ;; esac
for _arg in "$@"; do
  case "$_arg" in
  -v | --verbose) VERBOSE=1 ;;
  esac
done

# Colour only on a terminal and when NO_COLOR is unset.
if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$(printf '\033[1m')
  DIM=$(printf '\033[2m')
  BLUE=$(printf '\033[38;5;39m')
  GREEN=$(printf '\033[32m')
  RED=$(printf '\033[31m')
  RESET=$(printf '\033[0m')
else
  BOLD='' DIM='' BLUE='' GREEN='' RED='' RESET=''
fi

# Use Unicode glyphs only under a UTF-8 locale; fall back to ASCII otherwise so
# the banner never renders as mojibake on a legacy terminal.
UTF8=0
case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in *UTF-8* | *utf-8* | *UTF8* | *utf8*) UTF8=1 ;; esac
if [ "$UTF8" -eq 1 ]; then CHECK='✓'; CROSS='✗'; else CHECK='OK'; CROSS='x'; fi

LOG_FILE=$(mktemp 2>/dev/null || echo "/tmp/kbagent-install.$$.log")

# Fractional sleep is not guaranteed by POSIX; probe once and fall back to 1s
# so the spinner never busy-loops on a shell whose `sleep` rejects "0.1".
if sleep 0.1 2>/dev/null; then SPIN_DELAY=0.1; else SPIN_DELAY=1; fi

info() { printf '%s\n' "$*" >&2; }

banner() {
  printf '\n' >&2
  if [ "$UTF8" -eq 1 ]; then
    printf '%s\n' "${BLUE}   ██╗  ██╗███████╗██████╗  ██████╗  ██████╗ ██╗      █████╗ ${RESET}" >&2
    printf '%s\n' "${BLUE}   ██║ ██╔╝██╔════╝██╔══██╗██╔═══██╗██╔═══██╗██║     ██╔══██╗${RESET}" >&2
    printf '%s\n' "${BLUE}   █████╔╝ █████╗  ██████╔╝██║   ██║██║   ██║██║     ███████║${RESET}" >&2
    printf '%s\n' "${BLUE}   ██╔═██╗ ██╔══╝  ██╔══██╗██║   ██║██║   ██║██║     ██╔══██║${RESET}" >&2
    printf '%s\n' "${BLUE}   ██║  ██╗███████╗██████╔╝╚██████╔╝╚██████╔╝███████╗██║  ██║${RESET}" >&2
    printf '%s\n' "${BLUE}   ╚═╝  ╚═╝╚══════╝╚═════╝  ╚═════╝  ╚═════╝ ╚══════╝╚═╝  ╚═╝${RESET}" >&2
  else
    printf '%s\n' "${BLUE}   K E B O O L A   C L I${RESET}" >&2
  fi
  printf '%s\n' "${DIM}           the agent-native command line for your data platform${RESET}" >&2
  printf '\n' >&2
}

# Spinner shown while a long step runs (TTY only).
_spin() {
  _spid=$1
  _smsg=$2
  _chars='|/-\'
  _i=0
  while kill -0 "$_spid" 2>/dev/null; do
    _i=$(((_i + 1) % 4))
    _c=$(printf '%s' "$_chars" | cut -c $((_i + 1)))
    printf '\r  %s %s ' "$_smsg" "$_c" >&2
    sleep "$SPIN_DELAY"
  done
}

# run_step "Message" cmd args...
# Quiet by default: runs the command with output captured to $LOG_FILE and shows
# a spinner, then a single ✓/✗ line. In verbose mode (or when stderr is not a
# TTY, e.g. CI), it streams the full command output instead. Returns the
# command's exit code. MUST be called inside an `if`/`&&`/`||` so `set -e` does
# not abort on an expected failure (e.g. the wheel->source fallback).
run_step() {
  _msg=$1
  shift
  if [ "$VERBOSE" -eq 1 ] || [ ! -t 2 ]; then
    info "$_msg..."
    "$@"
    return $?
  fi
  "$@" >"$LOG_FILE" 2>&1 &
  _pid=$!
  _spin "$_pid" "$_msg"
  if wait "$_pid"; then _rc=0; else _rc=$?; fi
  if [ "$_rc" -eq 0 ]; then
    printf '\r  %s%s%s %s            \n' "$GREEN" "$CHECK" "$RESET" "$_msg" >&2
  else
    printf '\r  %s%s%s %s            \n' "$RED" "$CROSS" "$RESET" "$_msg" >&2
  fi
  return $_rc
}

dump_log() {
  [ -f "$LOG_FILE" ] || return 0
  info ""
  info "Full output:"
  sed 's/^/    /' "$LOG_FILE" >&2 2>/dev/null || cat "$LOG_FILE" >&2
}

cleanup() { rm -f "$LOG_FILE" 2>/dev/null || true; }
trap cleanup EXIT

banner

# --- preconditions --------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  info "${RED}error:${RESET} 'uv' was not found on PATH. Install it first, then re-run:"
  info "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  info "  source \$HOME/.local/bin/env   # or restart your shell"
  exit 1
fi

# Pick the install spec. [server] pulls in FastAPI/uvicorn so `kbagent serve`
# (REST + MCP + UI) works; KBAGENT_NO_SERVER=1 opts out for a lighter install.
if [ "${KBAGENT_NO_SERVER:-}" = "1" ]; then
  spec="$PKG"
else
  spec="${PKG}[server]"
fi

# --- resolve the latest release version -----------------------------------
# Read the redirect target of /releases/latest instead of calling the GitHub
# API -- no token, no 60-req/h rate limit. The effective URL after following
# redirects looks like https://github.com/keboola/cli/releases/tag/v0.59.0.
info "${DIM}Resolving latest ${PKG} release...${RESET}"
final_url=$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
  "https://github.com/${REPO}/releases/latest" 2>/dev/null || true)
version=$(printf '%s' "$final_url" | sed -n 's#.*/releases/tag/v\{0,1\}##p')

# --- install --------------------------------------------------------------
installed=0
if [ -n "$version" ]; then
  wheel_url="https://github.com/${REPO}/releases/download/v${version}/${DIST}-${version}-py3-none-any.whl"
  # Confirm the asset exists before committing to it (a release may predate the
  # wheel-publishing workflow and have no asset attached).
  if curl -fsSL -I "$wheel_url" >/dev/null 2>&1; then
    if run_step "Installing kbagent v${version} (prebuilt wheel)" \
      uv tool install --force "${spec} @ ${wheel_url}"; then
      installed=1
    else
      info "${DIM}Prebuilt wheel install failed; falling back to source build.${RESET}"
    fi
  else
    info "${DIM}No prebuilt wheel for v${version} yet; falling back to source build.${RESET}"
  fi
else
  info "${DIM}Could not resolve the latest version; falling back to source build.${RESET}"
fi

if [ "$installed" -ne 1 ]; then
  if run_step "Building kbagent from source (this can take a few minutes on WSL)" \
    uv tool install --force "${spec} @ git+https://github.com/${REPO}"; then
    installed=1
  fi
fi

if [ "$installed" -ne 1 ]; then
  info ""
  info "${RED}Install failed.${RESET} Re-run with KBAGENT_INSTALL_VERBOSE=1 for details, or see:"
  info "  https://github.com/${REPO}/issues"
  dump_log
  exit 1
fi

# --- summary + next steps -------------------------------------------------
ver_str=$(kbagent --version 2>/dev/null || echo "kbagent installed")
bin_path=$(command -v kbagent 2>/dev/null || echo "~/.local/bin/kbagent")

info ""
printf '  %s%s%s %s%s%s  →  %s\n' "$GREEN" "$CHECK" "$RESET" "$BOLD" "$ver_str" "$RESET" "$bin_path" >&2
printf '  %sno sudo required · keboola-mcp-server bundled & auto-updating%s\n' "$DIM" "$RESET" >&2
info ""
printf '  %sNext steps%s\n' "$BOLD" "$RESET" >&2
printf '    %skbagent project add%s --project myproject \\\n' "$BOLD" "$RESET" >&2
printf '        --url https://connection.keboola.com --token YOUR_TOKEN   %s# connect a project%s\n' "$DIM" "$RESET" >&2
printf '    %skbagent --help%s     %s# see everything you can do%s\n' "$BOLD" "$RESET" "$DIM" "$RESET" >&2
printf '    %skbagent doctor%s     %s# verify your setup%s\n' "$BOLD" "$RESET" "$DIM" "$RESET" >&2
info ""

if ! command -v kbagent >/dev/null 2>&1; then
  info "${DIM}Note: open a new shell (or 'source \$HOME/.local/bin/env') so 'kbagent' is on PATH.${RESET}"
