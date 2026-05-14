"""Tests for pricing module: per-model rates, cost calc, event aggregation.

The pricing module is pure: events in, summary out. Each test feeds a
hand-crafted slice of the claude stream-json shape and asserts the
derived model id, token totals, cost breakdown, and tool counts.
"""

from __future__ import annotations

from keboola_agent_cli.server.pricing import (
    FALLBACK_RATE,
    PRICING_USD_PER_MTOK,
    aggregate_usage_from_events,
    build_run_summary,
    compute_cost,
    extract_model_from_events,
    extract_tool_summary,
    extract_total_cost_from_events,
    get_rate,
)


class TestGetRate:
    def test_exact_match_opus_4_7(self) -> None:
        rate, recognized = get_rate("claude-opus-4-7")
        assert recognized is True
        assert rate["input"] == 15.00
        assert rate["output"] == 75.00

    def test_exact_match_sonnet(self) -> None:
        rate, recognized = get_rate("claude-sonnet-4-6")
        assert recognized is True
        assert rate["input"] == 3.00
        assert rate["output"] == 15.00

    def test_haiku_dated_id(self) -> None:
        rate, recognized = get_rate("claude-haiku-4-5-20251001")
        assert recognized is True
        assert rate["input"] == 1.00

    def test_prefix_match_for_future_point_release(self) -> None:
        # A future ``claude-opus-4-7-20260601`` should still hit the Opus row.
        rate, recognized = get_rate("claude-opus-4-7-20260601")
        assert recognized is True
        assert rate["input"] == 15.00

    def test_unknown_model_falls_back_with_flag(self) -> None:
        rate, recognized = get_rate("totally-fake-model")
        assert recognized is False
        assert rate == FALLBACK_RATE

    def test_none_model_falls_back(self) -> None:
        rate, recognized = get_rate(None)
        assert recognized is False
        assert rate == FALLBACK_RATE

    def test_case_insensitive(self) -> None:
        rate, recognized = get_rate("Claude-Opus-4-7")
        assert recognized is True
        assert rate["output"] == 75.00


class TestComputeCost:
    def test_zero_usage_zero_cost(self) -> None:
        result = compute_cost("claude-opus-4-7", {})
        assert result["cost_usd"]["total"] == 0.0
        assert result["tokens"]["total"] == 0

    def test_basic_input_output(self) -> None:
        # 1M input @ $15 + 1M output @ $75 = $90
        result = compute_cost(
            "claude-opus-4-7",
            {"input_tokens": 1_000_000, "output_tokens": 1_000_000},
        )
        assert result["cost_usd"]["input"] == 15.0
        assert result["cost_usd"]["output"] == 75.0
        assert result["cost_usd"]["total"] == 90.0

    def test_cache_savings(self) -> None:
        # 100k cache_read @ $1.50/MTok = $0.15 (vs $1.50 if it was input)
        result = compute_cost(
            "claude-opus-4-7",
            {"cache_read_input_tokens": 100_000},
        )
        assert result["cost_usd"]["cache_read"] == 0.15
        assert result["cost_usd"]["total"] == 0.15

    def test_cache_creation_premium(self) -> None:
        # cache_create is 1.25x input rate ($18.75/MTok for Opus)
        result = compute_cost(
            "claude-opus-4-7",
            {"cache_creation_input_tokens": 1_000_000},
        )
        assert result["cost_usd"]["cache_create"] == 18.75

    def test_unknown_model_recognized_false(self) -> None:
        result = compute_cost("foo", {"input_tokens": 1_000_000})
        assert result["model_recognized"] is False
        # Falls back to median (sonnet) tier @ $3
        assert result["cost_usd"]["input"] == 3.0


class TestAggregateUsageFromEvents:
    def test_sums_across_assistant_turns(self) -> None:
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 200,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        }
                    },
                },
            },
            {
                "event": "stdout",
                "data": {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 50,
                            "output_tokens": 75,
                            "cache_creation_input_tokens": 1000,
                            "cache_read_input_tokens": 500,
                        }
                    },
                },
            },
        ]
        totals = aggregate_usage_from_events(events)
        assert totals["input_tokens"] == 150
        assert totals["output_tokens"] == 275
        assert totals["cache_creation_input_tokens"] == 1000
        assert totals["cache_read_input_tokens"] == 500

    def test_skips_result_event(self) -> None:
        # Result event also has usage but we don't want to double-count it
        # since assistant turns already covered everything.
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "assistant",
                    "message": {
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 200,
                        }
                    },
                },
            },
            {
                "event": "stdout",
                "data": {
                    "type": "result",
                    "usage": {
                        "input_tokens": 999,
                        "output_tokens": 999,
                    },
                },
            },
        ]
        totals = aggregate_usage_from_events(events)
        assert totals["input_tokens"] == 100  # not 1099
        assert totals["output_tokens"] == 200

    def test_handles_missing_usage_blocks(self) -> None:
        events = [
            {"event": "stdout", "data": {"type": "assistant", "message": {}}},
            {"event": "stderr", "data": {"raw": "noise"}},
        ]
        totals = aggregate_usage_from_events(events)
        assert all(v == 0 for v in totals.values())


class TestExtractModelFromEvents:
    def test_picks_model_from_init(self) -> None:
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-opus-4-7",
                },
            },
        ]
        assert extract_model_from_events(events) == "claude-opus-4-7"

    def test_returns_none_when_no_init(self) -> None:
        assert extract_model_from_events([{"event": "stdout", "data": {"type": "result"}}]) is None


class TestExtractTotalCostFromEvents:
    def test_returns_authoritative_cost(self) -> None:
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "result",
                    "total_cost_usd": 0.42,
                },
            },
        ]
        assert extract_total_cost_from_events(events) == 0.42

    def test_returns_none_without_result(self) -> None:
        assert extract_total_cost_from_events([]) is None


class TestExtractToolSummary:
    def test_counts_tool_uses_per_name(self) -> None:
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {}},
                            {"type": "tool_use", "name": "Bash", "input": {}},
                            {"type": "tool_use", "name": "Read", "input": {}},
                        ]
                    },
                },
            },
        ]
        summary = extract_tool_summary(events)
        assert summary["count"] == 3
        assert summary["by_tool"] == {"Bash": 2, "Read": 1}
        assert summary["errors"] == 0

    def test_counts_tool_errors(self) -> None:
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "is_error": True, "content": "boom"},
                            {"type": "tool_result", "is_error": False, "content": "ok"},
                        ]
                    },
                },
            },
        ]
        summary = extract_tool_summary(events)
        assert summary["errors"] == 1


class TestBuildRunSummary:
    def test_complete_run(self) -> None:
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-opus-4-7",
                },
            },
            {
                "event": "stdout",
                "data": {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                        "usage": {
                            "input_tokens": 1000,
                            "output_tokens": 500,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                        },
                    },
                },
            },
            {
                "event": "stdout",
                "data": {
                    "type": "result",
                    "total_cost_usd": 0.0525,
                },
            },
        ]
        summary = build_run_summary(events)
        assert summary["model"] == "claude-opus-4-7"
        assert summary["model_recognized"] is True
        assert summary["tokens"]["input"] == 1000
        assert summary["tokens"]["output"] == 500
        assert summary["tools"]["count"] == 1
        assert summary["tools"]["by_tool"] == {"Bash": 1}
        # Authoritative cost takes priority over computed
        assert summary["cost_usd"]["total"] == 0.0525
        assert summary["cost_usd"]["source"] == "claude_result"
        assert summary["events_count"] == 3

    def test_falls_back_to_computed_when_no_result(self) -> None:
        events = [
            {
                "event": "stdout",
                "data": {
                    "type": "assistant",
                    "message": {
                        "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
                    },
                },
            },
        ]
        summary = build_run_summary(events)
        # No model -> fallback rate ($3/MTok) -> $3 for 1M input
        assert summary["cost_usd"]["total"] == 3.0
        assert summary["cost_usd"]["source"] == "computed"


class TestPricingTableConsistency:
    """Smoke test that all entries have the expected four-bucket shape."""

    def test_every_model_has_four_buckets(self) -> None:
        for model, rate in PRICING_USD_PER_MTOK.items():
            assert set(rate.keys()) == {"input", "output", "cache_create", "cache_read"}, (
                f"{model} missing buckets"
            )
            for bucket, value in rate.items():
                assert isinstance(value, int | float), f"{model}.{bucket} not numeric"
                assert value > 0, f"{model}.{bucket} should be positive"
