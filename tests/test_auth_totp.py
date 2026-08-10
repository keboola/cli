"""Tests for auth/totp.py: RFC 6238 TOTP code computation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from keboola_agent_cli.auth.totp import compute_totp_code


class TestComputeTotpCode:
    def test_rfc6238_test_vector(self) -> None:
        """RFC 6238 appendix B: seed 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ' (the base32
        encoding of the ASCII string '12345678901234567890') at T=59s -> '94287082'
        for SHA1/8-digit. ``digits`` defaults to 6 (the login-password CLI never
        overrides it), but the RFC's published vector is 8-digit, so pass digits=8
        here to check against the exact reference value rather than a truncation."""
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=59):
            code = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", digits=8)
        assert code == "94287082"

    def test_default_is_six_digits(self) -> None:
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=59):
            code = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        assert len(code) == 6
        assert code.isdigit()

    def test_same_time_window_is_deterministic(self) -> None:
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=1000):
            first = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
            second = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        assert first == second

    def test_different_time_window_usually_differs(self) -> None:
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=0):
            first = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=10_000_000):
            second = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        assert first != second

    def test_accepts_lowercase_and_whitespace(self) -> None:
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=59):
            lower = compute_totp_code("gezdgnbvgy3tqojqgezdgnbvgy3tqojq")
            spaced = compute_totp_code("GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ")
            plain = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        assert lower == plain
        assert spaced == plain

    def test_code_rotates_across_period_boundary(self) -> None:
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=29):
            before = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        with patch("keboola_agent_cli.auth.totp.time.time", return_value=30):
            after = compute_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
        assert before != after

    def test_empty_secret_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            compute_totp_code("")

    def test_whitespace_only_secret_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            compute_totp_code("   ")

    def test_malformed_base32_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="base32"):
            compute_totp_code("not-valid-base32!!")
