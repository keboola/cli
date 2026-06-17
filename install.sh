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
#   KBAGENT_NO_SERVER=1   install CLI-only (skip the [server] extras: FastAPI/
#                         uvicorn for `kbagent serve`). Default bundles them so
#                         `kbagent serve --ui` works out of the box.

set -eu

REPO="keboola/cli"
PKG="keboola-cli"
DIST="keboola_cli" # normalized distribution name used in the wheel filename

info() { printf '%s\n' "$*" >&2; }

# --- preconditions --------------------------------------------------------
if ! command -v uv >/dev/null 2>&1; then
  info "error: 'uv' was not found on PATH. Install it first, then re-run:"
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
info "Resolving latest ${PKG} release..."
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
    info "Installing prebuilt wheel v${version} (no build)..."
    if uv tool install --force "${spec} @ ${wheel_url}"; then
      installed=1
    else
      info "Prebuilt wheel install failed; falling back to source build."
    fi
  else
    info "No prebuilt wheel for v${version} yet; falling back to source build."
  fi
else
  info "Could not resolve the latest version; falling back to source build."
fi

if [ "$installed" -ne 1 ]; then
  info "Building from source via git+ (this can take a few minutes on WSL)..."
  uv tool install --force "${spec} @ git+https://github.com/${REPO}"
fi

# --- verify ---------------------------------------------------------------
info ""
if command -v kbagent >/dev/null 2>&1; then
  info "Done. $(kbagent --version 2>/dev/null || echo 'kbagent installed')"
else
  info "Done. Restart your shell, then run: kbagent --version"
fi
