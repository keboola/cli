"""Per-model pricing tables and cost calculation for AI agent runs.

Used by the agent-runs persistence path to attach a ``$`` figure and a
token breakdown to every persisted run, so the UI can show users the
cost of each scheduled-agent invocation without having to dig into the
raw event stream.

Pricing rates are quoted in **USD per 1,000,000 tokens** (USD/MTok).
Numbers reflect Anthropic's published pricing as of January 2026 and
follow the four-bucket structure that ``stream-json`` reports per
assistant turn:

- ``input_tokens``                  -- regular prompt tokens this turn.
- ``output_tokens``                 -- assistant-generated tokens this turn.
- ``cache_creation_input_tokens``   -- prompt tokens that *populated* the
                                       prompt cache (5-min TTL); billed
                                       at ~1.25x the input rate.
- ``cache_read_input_tokens``       -- prompt tokens served from a warm
                                       cache; billed at ~0.10x the input
                                       rate. This is where caching pays off.

When the live event stream surfaces ``total_cost_usd`` (claude does, in
the ``result`` event), prefer that authoritative figure; the manual
calculation here is the fallback for streams that do not.
"""

from __future__ import annotations

from typing import Any

# Per-model rates in USD per 1M tokens. Buckets:
#   input  | output | cache_create | cache_read
# Source: anthropic.com/pricing as of 2026-01.
PRICING_USD_PER_MTOK: dict[str, dict[str, float]] = {
    # Claude Opus 4.7 (incl. 1M ctx variant) -- premium tier.
    "claude-opus-4-7": {
        "input": 15.00,
        "output": 75.00,
        "cache_create": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-7[1m]": {
        "input": 15.00,
        "output": 75.00,
        "cache_create": 18.75,
        "cache_read": 1.50,
    },
    # Earlier Opus revisions kept on the same rate (Anthropic has not
    # split historical Opus pricing to date).
    "claude-opus-4-6": {
        "input": 15.00,
        "output": 75.00,
        "cache_create": 18.75,
        "cache_read": 1.50,
    },
    "claude-opus-4-5": {
        "input": 15.00,
        "output": 75.00,
        "cache_create": 18.75,
        "cache_read": 1.50,
    },
    # Sonnet 4.6 -- balanced tier.
    "claude-sonnet-4-6": {
        "input": 3.00,
        "output": 15.00,
        "cache_create": 3.75,
        "cache_read": 0.30,
    },
    "claude-sonnet-4-5": {
        "input": 3.00,
        "output": 15.00,
        "cache_create": 3.75,
        "cache_read": 0.30,
    },
    # Haiku 4.5 -- speed tier.
    "claude-haiku-4-5": {
        "input": 1.00,
        "output": 5.00,
        "cache_create": 1.25,
        "cache_read": 0.10,
    },
    "claude-haiku-4-5-20251001": {
        "input": 1.00,
        "output": 5.00,
        "cache_create": 1.25,
        "cache_read": 0.10,
    },
}

# Fallback rate when an unknown model id appears in the stream. We pick
# Sonnet's rate because it's the median tier; users will see *roughly*
# the right ballpark even if the model is mis-tagged. The structured
# response also returns ``model_recognized: false`` so the UI can show
# a "approximate" badge.
FALLBACK_RATE: dict[str, float] = {
    "input": 3.00,
    "output": 15.00,
    "cache_create": 3.75,
    "cache_read": 0.30,
}


def get_rate(model: str | None) -> tuple[dict[str, float], bool]:
    """Resolve a per-MTok rate dict for a model id.

    Returns ``(rate, recognized)``: ``recognized`` is False when we fell
    back to the median tier, so callers can mark the figure as approximate.
    Matching is exact first, then prefix-based (``claude-opus-4-7-foo``
    matches ``claude-opus-4-7``) so future point-release tags still hit.
    """
    if not model:
        return FALLBACK_RATE, False
    norm = model.strip().lower()
    if norm in PRICING_USD_PER_MTOK:
        return PRICING_USD_PER_MTOK[norm], True
    # Longest-prefix wins so ``claude-opus-4-7-future`` doesn't accidentally
    # match ``claude-opus-4-5``.
    candidates = sorted(
        (k for k in PRICING_USD_PER_MTOK if norm.startswith(k)),
        key=lambda k: len(k),
        reverse=True,
    )
    if candidates:
        return PRICING_USD_PER_MTOK[candidates[0]], True
    return FALLBACK_RATE, False


def compute_cost(
    model: str | None,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compute cost breakdown for a single turn or aggregated usage.

    ``usage`` is the dict shape claude emits per assistant message:

        {"input_tokens": 1234, "output_tokens": 567,
         "cache_creation_input_tokens": 8000, "cache_read_input_tokens": 2000}

    Returns a self-describing dict with both the rate that was applied
    and the per-bucket cost contribution, so the UI can render a
    breakdown table without re-doing the math:

        {
          "model": "claude-opus-4-7",
          "model_recognized": true,
          "rate_per_mtok": {"input": 15.0, "output": 75.0, ...},
          "tokens": {input: 1234, output: 567, ...},
          "cost_usd": {input: 0.0185, output: 0.0425, cache_create: 0.15, cache_read: 0.003,
                      total: 0.214}
        }
    """
    usage = usage or {}
    rate, recognized = get_rate(model)
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cc_tok = int(usage.get("cache_creation_input_tokens") or 0)
    cr_tok = int(usage.get("cache_read_input_tokens") or 0)
    in_cost = (in_tok / 1_000_000) * rate["input"]
    out_cost = (out_tok / 1_000_000) * rate["output"]
    cc_cost = (cc_tok / 1_000_000) * rate["cache_create"]
    cr_cost = (cr_tok / 1_000_000) * rate["cache_read"]
    total_cost = in_cost + out_cost + cc_cost + cr_cost
    return {
        "model": model,
        "model_recognized": recognized,
        "rate_per_mtok": rate,
        "tokens": {
            "input": in_tok,
            "output": out_tok,
            "cache_create": cc_tok,
            "cache_read": cr_tok,
            "total": in_tok + out_tok + cc_tok + cr_tok,
        },
        "cost_usd": {
            "input": round(in_cost, 6),
            "output": round(out_cost, 6),
            "cache_create": round(cc_cost, 6),
            "cache_read": round(cr_cost, 6),
            "total": round(total_cost, 6),
        },
    }


def aggregate_usage_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum per-assistant-turn ``usage`` blocks into a single dict.

    Walks the stream-json event list, picks every ``assistant`` event,
    and accumulates the four token buckets. Final ``result`` event also
    carries a usage block (often equal to the last assistant turn) which
    we deliberately *skip* to avoid double-counting -- claude's result
    usage is already reflected in the assistant turns it summarizes.
    """
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    for evt in events:
        if evt.get("event") not in {"stdout"}:
            continue
        data = evt.get("data") or {}
        if data.get("type") != "assistant":
            continue
        usage = (data.get("message") or {}).get("usage") or {}
        for k in totals:
            v = usage.get(k)
            if isinstance(v, int):
                totals[k] += v
    return totals


def extract_model_from_events(events: list[dict[str, Any]]) -> str | None:
    """Return the model id from the first ``system.init`` event, if present."""
    for evt in events:
        if evt.get("event") != "stdout":
            continue
        data = evt.get("data") or {}
        if data.get("type") == "system" and data.get("subtype") == "init":
            model = data.get("model")
            if isinstance(model, str):
                return model
    return None


def extract_total_cost_from_events(events: list[dict[str, Any]]) -> float | None:
    """Return claude's authoritative ``total_cost_usd`` from the result event.

    When present this is preferred over the manual ``compute_cost`` figure
    (it accounts for any per-turn pricing nuance Anthropic applies that
    we cannot model from the public rate card).
    """
    for evt in events:
        if evt.get("event") != "stdout":
            continue
        data = evt.get("data") or {}
        if data.get("type") == "result":
            cost = data.get("total_cost_usd")
            if isinstance(cost, int | float):
                return float(cost)
    return None


def extract_tool_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Walk events and produce a per-tool call count + ordered list of tools used.

    Returns:
        {
            "count": <total tool_use blocks>,
            "by_tool": {"Bash": 4, "Read": 7, ...},
            "errors": <count of tool_results with is_error=true>,
        }
    """
    by_tool: dict[str, int] = {}
    total = 0
    errors = 0
    for evt in events:
        if evt.get("event") != "stdout":
            continue
        data = evt.get("data") or {}
        if data.get("type") == "assistant":
            for block in (data.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "?")
                    by_tool[name] = by_tool.get(name, 0) + 1
                    total += 1
        elif data.get("type") == "user":
            for block in (data.get("message") or {}).get("content") or []:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error") is True
                ):
                    errors += 1
    return {"count": total, "by_tool": by_tool, "errors": errors}


def build_run_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Single entry point: build the summary blob persisted on a finished run.

    Combines model, token totals, cost breakdown (preferring claude's
    authoritative number), and tool-call breakdown. Shape is stable across
    runs so the UI can render unconditionally.
    """
    model = extract_model_from_events(events)
    usage = aggregate_usage_from_events(events)
    cost_breakdown = compute_cost(model, usage)
    authoritative = extract_total_cost_from_events(events)
    if authoritative is not None:
        cost_breakdown["cost_usd"]["total"] = round(authoritative, 6)
        cost_breakdown["cost_usd"]["source"] = "claude_result"
    else:
        cost_breakdown["cost_usd"]["source"] = "computed"
    tools = extract_tool_summary(events)
    return {
        "model": model,
        "model_recognized": cost_breakdown["model_recognized"],
        "tokens": cost_breakdown["tokens"],
        "rate_per_mtok": cost_breakdown["rate_per_mtok"],
        "cost_usd": cost_breakdown["cost_usd"],
        "tools": tools,
        "events_count": len(events),
    }
