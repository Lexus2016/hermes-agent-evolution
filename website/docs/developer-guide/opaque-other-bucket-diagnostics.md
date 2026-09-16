# Opaque `other` bucket diagnostics (issue #3010)

The tool-failure classifier (`tools/tool_failure_classifier.py`) maps raw errors
to named categories; unmatched errors fall through to `persistent_error` /
`unknown` — historically a generic catch-all with no recovery path, causing
blind retries and 11-18-deep spirals.

**Change**: (1) an actionable fall-through hint — a fingerprint + "verify
args/syntax, do not blind-retry" (still non-retryable); (2) `error_fingerprint()`
— a short stable key so identical causes tally; (3) `UnhandledDrilldown` — a
session-local, bounded per-tool tally of top `other`-bucket fingerprints, wired
into the always-on failure path (`run_agent.py` / `agent_init.py`) to feed the
next introspection pass.

**Guarantees**: default classification path unchanged except the hint; recording
is a no-op when unarmed or classified cleanly, never raises, never persists;
recovery dispatcher (`#1027`) and diagnosis (`#1029`) unaffected.
