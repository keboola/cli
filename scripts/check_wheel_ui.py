"""CI helper: assert whether a built wheel bundles the React SPA.

Used by the Windows wheel-build CI job to verify the issue #320 fixes
end-to-end on a real Windows runner (where no developer machine is needed):

- a normal ``uv build`` must bundle ``_ui_dist/index.html`` (Bug 1: the
  ``npm.cmd`` invocation actually succeeds), and
- a ``KBAGENT_SKIP_UI_BUILD=1`` build must still produce a valid wheel with
  no SPA (Bug 2: the empty ``_ui_dist`` placeholder lets force-include
  resolve instead of crashing the build).

The check is OS-independent, so the same assertion guards local builds too.

Usage:
    python scripts/check_wheel_ui.py --expect-ui [--dist DIR]
    python scripts/check_wheel_ui.py --no-ui     [--dist DIR]
"""

from __future__ import annotations

import argparse
import glob
import sys
import zipfile

# Path of the bundled SPA entry point inside the wheel (zip paths always use
# forward slashes, including on Windows).
UI_MARKER = "keboola_agent_cli/_ui_dist/index.html"


def wheel_bundles_ui(wheel_path: str) -> bool:
    """Return True iff the wheel contains the bundled SPA entry point."""
    with zipfile.ZipFile(wheel_path) as zf:
        return UI_MARKER in zf.namelist()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--expect-ui",
        action="store_true",
        help="fail unless the wheel bundles the SPA (normal build)",
    )
    group.add_argument(
        "--no-ui",
        action="store_true",
        help="fail if the wheel bundles the SPA (CLI-only build)",
    )
    parser.add_argument("--dist", default="dist", help="directory containing the built wheel(s)")
    args = parser.parse_args(argv)

    wheels = sorted(glob.glob(f"{args.dist}/*.whl"))
    if not wheels:
        print(f"ERROR: no wheel found in {args.dist}/", file=sys.stderr)
        return 1

    # Newest by name -- a single build produces one wheel anyway.
    wheel = wheels[-1]
    has_ui = wheel_bundles_ui(wheel)
    print(f"wheel={wheel} bundles_ui={has_ui}")

    if args.expect_ui and not has_ui:
        print(
            f"FAIL: expected '{UI_MARKER}' in the wheel (issue #320 Bug 1 regression).",
            file=sys.stderr,
        )
        return 1
    if args.no_ui and has_ui:
        print(
            f"FAIL: did not expect '{UI_MARKER}' in a CLI-only wheel.",
            file=sys.stderr,
        )
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
