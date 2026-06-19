"""Retry utilities — jittered backoff for decorrelated retries.

Replaces fixed exponential backoff with jittered delays to prevent
thundering-herd retry spikes when multiple sessions hit the same
rate-limited provider concurrently.
"""

import calendar
import email.utils
import random
import threading
import time

# Monotonic counter for jitter seed uniqueness within the same process.
# Protected by a lock to avoid race conditions in concurrent retry paths
# (e.g. multiple gateway sessions retrying simultaneously).
_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Base delay in seconds for attempt 1.
        max_delay: Maximum delay cap in seconds.
        jitter_ratio: Fraction of computed delay to use as random jitter
            range.  0.5 means jitter is uniform in [0, 0.5 * delay].

    Returns:
        Delay in seconds: min(base * 2^(attempt-1), max_delay) + jitter.

    The jitter decorrelates concurrent retries so multiple sessions
    hitting the same provider don't all retry at the same instant.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2**exponent), max_delay)

    # Seed from time + counter for decorrelation even with coarse clocks.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def extract_retry_after_seconds(
    retry_after: str | None, now: float | None = None
) -> float | None:
    """Parse a Retry-After value into seconds from now.

    Supports both integer seconds (RFC 7231 §7.1.3) and HTTP-date strings
    (RFC 7231 §7.1.1.2).  Returns None for malformed/empty values.
    Caps the result at 300 seconds (5 minutes) so a far-future Retry-After
    header cannot stall the agent indefinitely.
    """
    if not retry_after or not isinstance(retry_after, str):
        return None
    retry_after = retry_after.strip()
    if not retry_after:
        return None

    # Try integer seconds first.
    try:
        return min(float(retry_after), 300.0)
    except (TypeError, ValueError):
        pass

    # Try HTTP-date (e.g. "Wed, 21 Oct 2015 07:28:00 GMT").
    try:
        parsed = email.utils.parsedate_to_datetime(retry_after)
        retry_time = calendar.timegm(parsed.utctimetuple())
        now = now if now is not None else time.time()
        return max(0.0, min(retry_time - now, 300.0))
    except Exception:
        return None
