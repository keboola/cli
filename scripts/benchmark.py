#!/usr/bin/env python3
"""Performance benchmark for kbagent MCP tool calls.

Measures time breakdown for:
1. CLI startup overhead (Python + imports)
2. MCP server detection
3. Stdio transport (subprocess per call)
4. HTTP transport (persistent server)
5. Raw MCP call (no CLI overhead)
6. Multi-project parallel execution

Usage:
    python scripts/benchmark.py [--project ALIAS] [--all] [--runs N]

Requires at least one project configured in kbagent.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add src to path for direct imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def timer(label: str):
    """Context manager that prints elapsed time."""

    class Timer:
        def __init__(self):
            self.elapsed = 0.0

        def __enter__(self):
            self._start = time.perf_counter()
            return self

        def __exit__(self, *args):
            self.elapsed = time.perf_counter() - self._start

    return Timer()


def measure_cli_startup() -> float:
    """Measure kbagent CLI startup time (--help, no MCP)."""
    start = time.perf_counter()
    subprocess.run(
        ["kbagent", "--help"],
        capture_output=True,
        timeout=30,
    )
    return time.perf_counter() - start


def measure_import_time() -> float:
    """Measure Python import time for keboola_agent_cli."""
    start = time.perf_counter()
    subprocess.run(
        ["python", "-c", "import keboola_agent_cli"],
        capture_output=True,
        timeout=30,
    )
    return time.perf_counter() - start


def measure_mcp_detection() -> tuple[float, list[str] | None]:
    """Measure MCP server detection time."""
    from keboola_agent_cli.services.mcp_service import detect_mcp_server_command

    start = time.perf_counter()
    cmd = detect_mcp_server_command()
    elapsed = time.perf_counter() - start
    return elapsed, cmd


def measure_cli_tool_call(project: str, tool: str, tool_input: str = "{}") -> tuple[float, bool]:
    """Measure full CLI tool call via subprocess."""
    cmd = ["kbagent", "--json", "tool", "call", tool, "--project", project]
    if tool_input != "{}":
        cmd.extend(["--input", tool_input])

    start = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
    elapsed = time.perf_counter() - start
    success = result.returncode == 0
    return elapsed, success


async def measure_raw_stdio_call(
    project_alias: str, tool: str, tool_input: dict | None = None
) -> tuple[float, float, float, float, bool]:
    """Measure raw MCP stdio call with phase breakdown.

    Returns: (total, spawn_time, init_time, call_time, success)
    """
    from contextlib import AsyncExitStack

    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from keboola_agent_cli.config_store import ConfigStore
    from keboola_agent_cli.services.mcp_service import (
        _build_server_params,
    )

    store = ConfigStore()
    project = store.get_project(project_alias)

    total_start = time.perf_counter()

    # Phase 1: Build params + spawn subprocess
    spawn_start = time.perf_counter()
    params = _build_server_params(project)
    exit_stack = AsyncExitStack()

    read_stream, write_stream = await asyncio.wait_for(
        exit_stack.enter_async_context(stdio_client(params, errlog=subprocess.DEVNULL)),
        timeout=30,
    )
    spawn_time = time.perf_counter() - spawn_start

    # Phase 2: Initialize session
    init_start = time.perf_counter()
    session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
    await asyncio.wait_for(session.initialize(), timeout=30)
    init_time = time.perf_counter() - init_start

    # Phase 3: Call tool
    call_start = time.perf_counter()
    result = await asyncio.wait_for(
        session.call_tool(tool, tool_input or {}),
        timeout=60,
    )
    call_time = time.perf_counter() - call_start

    success = not result.isError
    await exit_stack.aclose()

    total = time.perf_counter() - total_start
    return total, spawn_time, init_time, call_time, success


async def measure_raw_http_call(
    project_alias: str, tool: str, tool_input: dict | None = None
) -> tuple[float, float, float, float, float, bool]:
    """Measure raw MCP HTTP call with phase breakdown.

    Returns: (total, server_ensure_time, connect_time, init_time, call_time, success)
    """
    from contextlib import AsyncExitStack

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    from keboola_agent_cli.config_store import ConfigStore
    from keboola_agent_cli.services.mcp_service import (
        _build_http_headers,
    )
    from keboola_agent_cli.services.mcp_transport import get_server_manager

    store = ConfigStore()
    project = store.get_project(project_alias)

    total_start = time.perf_counter()

    # Phase 1: Ensure server is running
    server_start = time.perf_counter()
    manager = get_server_manager()
    base_url = manager.ensure_running()
    server_time = time.perf_counter() - server_start

    headers = _build_http_headers(project)
    url = f"{base_url}/mcp"

    # Phase 2: Connect HTTP
    connect_start = time.perf_counter()
    exit_stack = AsyncExitStack()
    read_stream, write_stream, _ = await exit_stack.enter_async_context(
        streamablehttp_client(url=url, headers=headers)
    )
    connect_time = time.perf_counter() - connect_start

    # Phase 3: Initialize session
    init_start = time.perf_counter()
    session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
    await asyncio.wait_for(session.initialize(), timeout=30)
    init_time = time.perf_counter() - init_start

    # Phase 4: Call tool
    call_start = time.perf_counter()
    result = await asyncio.wait_for(
        session.call_tool(tool, tool_input or {}),
        timeout=60,
    )
    call_time = time.perf_counter() - call_start

    success = not result.isError
    await exit_stack.aclose()

    total = time.perf_counter() - total_start
    return total, server_time, connect_time, init_time, call_time, success


async def measure_multi_project_stdio(
    projects: list[str], tool: str
) -> tuple[float, dict[str, float]]:
    """Measure parallel multi-project stdio calls."""
    start = time.perf_counter()

    async def _call(alias: str):
        t_start = time.perf_counter()
        try:
            total, _, _, _, success = await measure_raw_stdio_call(alias, tool)
            return alias, total, success
        except Exception:
            return alias, time.perf_counter() - t_start, False

    tasks = [_call(alias) for alias in projects]
    results = await asyncio.gather(*tasks)

    total = time.perf_counter() - start
    per_project = {alias: t for alias, t, _ in results}
    return total, per_project


def get_all_projects() -> list[str]:
    """Get all configured project aliases."""
    from keboola_agent_cli.config_store import ConfigStore

    store = ConfigStore()
    config = store.load()
    return list(config.projects.keys())


def format_time(seconds: float) -> str:
    """Format time nicely."""
    if seconds < 0.01:
        return f"{seconds * 1000:.1f}ms"
    return f"{seconds:.2f}s"


def print_header(title: str):
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_bar(label: str, seconds: float, max_seconds: float = 15.0, width: int = 40):
    """Print a horizontal bar chart line."""
    bar_len = int((seconds / max_seconds) * width)
    bar_len = max(1, min(bar_len, width))
    bar = "#" * bar_len
    print(f"  {label:<25s} {bar:<{width}s} {format_time(seconds)}")


BENCHMARK_TOOLS = [
    ("get_project_info", {}),
    ("get_buckets", {}),
    ("get_tables", {}),
    ("get_configs", {}),
    ("get_jobs", {"limit": 5}),
    ("get_flows", {}),
]


def run_benchmarks(project: str, runs: int = 1, run_multi: bool = False):
    """Run all benchmark suites."""
    print("\nkbagent Performance Benchmark")
    print(f"Date: {time.strftime('%Y-%m-%d %H:%M')}")
    print(f"Project: {project}")
    print(f"Runs per test: {runs}")

    # ---- 1. CLI Startup ----
    print_header("1. CLI Startup Overhead")

    times = []
    for _ in range(runs):
        t = measure_cli_startup()
        times.append(t)
    avg_startup = sum(times) / len(times)
    print(f"  kbagent --help:          {format_time(avg_startup)} (avg of {runs})")

    times = []
    for _ in range(runs):
        t = measure_import_time()
        times.append(t)
    avg_import = sum(times) / len(times)
    print(f"  python import:           {format_time(avg_import)} (avg of {runs})")

    det_time, det_cmd = measure_mcp_detection()
    print(f"  MCP server detection:    {format_time(det_time)}")
    print(f"  Detected command:        {' '.join(det_cmd) if det_cmd else 'NOT FOUND'}")

    # ---- 2. Stdio Transport ----
    print_header("2. Stdio Transport (subprocess per call)")

    stdio_results = []
    for tool_name, tool_input in BENCHMARK_TOOLS:
        tool_times = []
        for _ in range(runs):
            total, spawn, init, call, ok = asyncio.run(
                measure_raw_stdio_call(project, tool_name, tool_input)
            )
            tool_times.append((total, spawn, init, call, ok))

        avg_total = sum(t[0] for t in tool_times) / len(tool_times)
        avg_spawn = sum(t[1] for t in tool_times) / len(tool_times)
        avg_init = sum(t[2] for t in tool_times) / len(tool_times)
        avg_call = sum(t[3] for t in tool_times) / len(tool_times)
        all_ok = all(t[4] for t in tool_times)
        status = "ok" if all_ok else "FAIL"

        stdio_results.append(
            {
                "tool": tool_name,
                "total": avg_total,
                "spawn": avg_spawn,
                "init": avg_init,
                "call": avg_call,
                "status": status,
            }
        )

        input_str = json.dumps(tool_input) if tool_input else "-"
        print(f"\n  {tool_name} ({input_str}) [{status}]")
        print_bar("Subprocess spawn", avg_spawn)
        print_bar("Session init", avg_init)
        print_bar("Tool call (API)", avg_call)
        print_bar("TOTAL", avg_total)

    avg_stdio = sum(r["total"] for r in stdio_results) / len(stdio_results)
    avg_spawn = sum(r["spawn"] for r in stdio_results) / len(stdio_results)
    avg_init = sum(r["init"] for r in stdio_results) / len(stdio_results)
    avg_api = sum(r["call"] for r in stdio_results) / len(stdio_results)

    print("\n  --- Stdio Averages ---")
    print(f"  Total:           {format_time(avg_stdio)}")
    print(f"  Subprocess spawn: {format_time(avg_spawn)}")
    print(f"  Session init:     {format_time(avg_init)}")
    print(f"  API call:         {format_time(avg_api)}")

    # ---- 3. HTTP Transport ----
    print_header("3. HTTP Transport (persistent server)")

    # Set transport to HTTP
    os.environ["KBAGENT_MCP_TRANSPORT"] = "http"

    http_results = []
    for i, (tool_name, tool_input) in enumerate(BENCHMARK_TOOLS):
        tool_times = []
        for _ in range(runs):
            total, server, connect, init, call, ok = asyncio.run(
                measure_raw_http_call(project, tool_name, tool_input)
            )
            tool_times.append((total, server, connect, init, call, ok))

        avg_total = sum(t[0] for t in tool_times) / len(tool_times)
        avg_server = sum(t[1] for t in tool_times) / len(tool_times)
        avg_connect = sum(t[2] for t in tool_times) / len(tool_times)
        avg_init = sum(t[3] for t in tool_times) / len(tool_times)
        avg_call = sum(t[4] for t in tool_times) / len(tool_times)
        all_ok = all(t[5] for t in tool_times)
        status = "ok" if all_ok else "FAIL"

        # Calculate vs stdio
        stdio_total = stdio_results[i]["total"]
        vs_stdio = ((avg_total - stdio_total) / stdio_total) * 100

        http_results.append(
            {
                "tool": tool_name,
                "total": avg_total,
                "server": avg_server,
                "connect": avg_connect,
                "init": avg_init,
                "call": avg_call,
                "status": status,
                "vs_stdio": vs_stdio,
            }
        )

        input_str = json.dumps(tool_input) if tool_input else "-"
        print(f"\n  {tool_name} ({input_str}) [{status}] vs stdio: {vs_stdio:+.0f}%")
        print_bar("Server ensure", avg_server)
        print_bar("HTTP connect", avg_connect)
        print_bar("Session init", avg_init)
        print_bar("Tool call (API)", avg_call)
        print_bar("TOTAL", avg_total)

    avg_http = sum(r["total"] for r in http_results) / len(http_results)
    avg_vs_stdio = sum(r["vs_stdio"] for r in http_results) / len(http_results)

    print("\n  --- HTTP Averages ---")
    print(f"  Total:           {format_time(avg_http)}")
    print(f"  vs Stdio:        {avg_vs_stdio:+.0f}%")

    # Stop the persistent server
    from keboola_agent_cli.services.mcp_transport import get_server_manager

    get_server_manager().stop()
    os.environ["KBAGENT_MCP_TRANSPORT"] = "stdio"

    # ---- 4. CLI Full Call (via subprocess) ----
    print_header("4. Full CLI Call (kbagent tool call via subprocess)")

    cli_results = []
    for tool_name, tool_input in BENCHMARK_TOOLS[:3]:  # Only first 3 to save time
        tool_times = []
        for _ in range(runs):
            elapsed, ok = measure_cli_tool_call(
                project, tool_name, json.dumps(tool_input) if tool_input else "{}"
            )
            tool_times.append((elapsed, ok))

        avg_elapsed = sum(t[0] for t in tool_times) / len(tool_times)
        all_ok = all(t[1] for t in tool_times)
        status = "ok" if all_ok else "FAIL"

        cli_results.append({"tool": tool_name, "total": avg_elapsed, "status": status})
        print(f"  {tool_name:<25s} {format_time(avg_elapsed):>10s}  [{status}]")

    if cli_results:
        avg_cli = sum(r["total"] for r in cli_results) / len(cli_results)
        print(f"\n  Average full CLI call:  {format_time(avg_cli)}")
        cli_overhead = avg_cli - avg_stdio
        print(f"  CLI overhead vs raw:   {format_time(cli_overhead)}")

    # ---- 5. Multi-project parallel ----
    if run_multi:
        print_header("5. Multi-project Parallel (stdio)")
        all_projects = get_all_projects()
        print(f"  Projects: {len(all_projects)}")

        for tool_name in ["get_buckets", "get_tables"]:
            total, per_project = asyncio.run(measure_multi_project_stdio(all_projects, tool_name))
            print(f"\n  {tool_name} x {len(all_projects)} projects")
            print(f"  Wall time:  {format_time(total)}")
            slowest = max(per_project.values())
            fastest = min(per_project.values())
            print(f"  Fastest:    {format_time(fastest)}")
            print(f"  Slowest:    {format_time(slowest)}")

    # ---- Summary ----
    print_header("SUMMARY")
    print(f"\n  {'Metric':<30s} {'Stdio':>10s} {'HTTP':>10s} {'Diff':>10s}")
    print(f"  {'-' * 60}")
    print(
        f"  {'Avg tool call':<30s} {format_time(avg_stdio):>10s} {format_time(avg_http):>10s} {avg_vs_stdio:>+9.0f}%"
    )
    print(f"  {'CLI startup':<30s} {format_time(avg_startup):>10s} {'-':>10s} {'-':>10s}")

    if cli_results:
        print(f"  {'Full CLI call (avg)':<30s} {format_time(avg_cli):>10s} {'-':>10s} {'-':>10s}")

    print("\n  Bottleneck breakdown (stdio avg):")
    print(
        f"  {'CLI startup:':<30s} {format_time(avg_startup)} ({avg_startup / avg_cli * 100:.0f}% of full CLI call)"
        if cli_results
        else ""
    )
    print(
        f"  {'Subprocess spawn:':<30s} {format_time(avg_spawn)} ({avg_spawn / avg_stdio * 100:.0f}% of raw call)"
    )
    print(
        f"  {'Session init:':<30s} {format_time(avg_init)} ({avg_init / avg_stdio * 100:.0f}% of raw call)"
    )
    print(
        f"  {'API call:':<30s} {format_time(avg_api)} ({avg_api / avg_stdio * 100:.0f}% of raw call)"
    )

    # Daemon estimate
    print("\n  Daemon mode estimate:")
    print("  IPC overhead:            ~0.001s")
    print("  Session reuse:           0s (pre-connected)")
    print(f"  API call:                {format_time(avg_api)}")
    print(f"  Estimated total:         ~{format_time(avg_api + 0.01)}")
    print(f"  vs current stdio:        -{((avg_stdio - avg_api - 0.01) / avg_stdio * 100):.0f}%")
    print()


def main():
    parser = argparse.ArgumentParser(description="kbagent performance benchmark")
    parser.add_argument("--project", "-p", help="Project alias to benchmark")
    parser.add_argument("--all", "-a", action="store_true", help="Include multi-project tests")
    parser.add_argument("--runs", "-n", type=int, default=1, help="Runs per test (default: 1)")
    args = parser.parse_args()

    project = args.project
    if not project:
        projects = get_all_projects()
        if not projects:
            print("ERROR: No projects configured. Use 'kbagent project add' first.")
            sys.exit(1)
        project = projects[0]
        print(f"Using first project: {project}")

    run_benchmarks(project, runs=args.runs, run_multi=args.all)


if __name__ == "__main__":
    main()
