"""Tests for Retry-After parsing in agent.retry_utils."""

from __future__ import annotations

import time

import pytest

from agent.retry_utils import extract_retry_after_seconds, jittered_backoff


def test_extract_retry_after_seconds_integer():
    assert extract_retry_after_seconds("42") == 42.0


def test_extract_retry_after_seconds_integer_with_whitespace():
    assert extract_retry_after_seconds("  7  ") == 7.0


def test_extract_retry_after_seconds_http_date():
    # 10 seconds in the future
    future = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 10))
    parsed = extract_retry_after_seconds(future)
    assert parsed is not None
    assert 9.0 <= parsed <= 10.0


def test_extract_retry_after_seconds_http_date_past():
    past = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 5))
    assert extract_retry_after_seconds(past) == 0.0


def test_extract_retry_after_seconds_caps_at_600():
    parsed = extract_retry_after_seconds("900")
    assert parsed is not None
    assert parsed == 600.0


def test_extract_retry_after_seconds_invalid_returns_none():
    assert extract_retry_after_seconds("not-a-number-or-date") is None
    assert extract_retry_after_seconds("") is None
    assert extract_retry_after_seconds(None) is None


def test_jittered_backoff_increases_with_attempt():
    base = jittered_backoff(1, base_delay=1.0, max_delay=120.0)
    later = jittered_backoff(5, base_delay=1.0, max_delay=120.0)
    assert base < later


def test_jittered_backoff_respects_max_delay():
    assert jittered_backoff(100, base_delay=1.0, max_delay=30.0) <= 45.0
