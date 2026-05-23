"""Dump the kbagent serve OpenAPI schema to a JSON file.

Builds the FastAPI app in-memory (no uvicorn boot, no port binding) so the
schema can be generated hermetically in CI / pre-commit / local dev.

Usage:
    python scripts/dump_openapi.py [--output PATH]

Default output: web/frontend/src/api/openapi.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "web" / "frontend" / "src" / "api" / "openapi.json"


def dump_schema(output: Path) -> None:
    # Lazy import so this script can be inspected without installing the package.
    from keboola_agent_cli.server import create_app

    # auth_token is required by the factory but irrelevant for schema dumping.
    # A dummy value keeps the schema clean (the bearer scheme is described in
    # `_build_custom_openapi`, not via the actual token).
    app = create_app(auth_token="dummy-token-for-schema-dump")
    schema = app.openapi()

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"OpenAPI schema written to {output} ({len(schema.get('paths', {}))} paths)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output file (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args(argv)
    dump_schema(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
