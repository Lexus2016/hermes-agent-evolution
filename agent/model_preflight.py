"""Pre-flight provider/model validation (issue #929).

Proactive companion to #758 (PR #923).  #758 handles the *reactive* case: when
a provider rejects a model with ``model_not_found`` the user gets an actionable
recovery message instead of a silent abort.  #929 catches the same class of
failure *before* the first provider request, so a run does not burn a round
trip — and, in unattended cron/subagent contexts, does not risk cascading into
provider connection exhaustion — on a model name the provider can never serve.

Validation-source boundary (deliberate — documented in the PR)
--------------------------------------------------------------
The codebase maintains a LOCAL provider registry
(``providers.get_provider_profile`` / ``model_metadata._PROVIDER_PREFIXES``)
and per-provider *curated* ``fallback_models`` lists, but it has NO cheap,
authoritative, EXHAUSTIVE local catalog of every model each provider currently
serves.  Enumerating that requires a network call
(``ProviderProfile.fetch_models`` / models.dev / OpenRouter's ``/models``),
which must NOT run on every turn.

So this pre-flight fails-closed ONLY on model strings that are *structurally*
unresolvable — model ids no provider can ever route:

  * a bare provider prefix with nothing after the colon (``"openrouter:"``)
  * an id containing internal whitespace (``"gpt 4o"`` — a pasted display name),
    but ONLY for a recognised hosted provider — unknown custom/corporate
    gateways fail open (their id convention is unknown to us)

Those deterministically produce ``model_not_found`` (HTTP 400/404) at every
provider and are knowable with zero network and essentially zero false
positives.  Deliberately NOT rejected here:

  * an EMPTY / blank model — that is a legitimate "use the provider default"
    state elsewhere in the codebase (exercised by existing tests), so failing
    it closed would block valid runs.
  * a well-formed-but-currently-unavailable model (e.g. an OpenRouter slug the
    catalog has dropped) — no cheap local signal distinguishes it from a valid
    one, so it stays on #758's reactive path.

The provider's ``fallback_models`` are consulted only to SUGGEST known-good
names, never to reject.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PreflightMiss:
    """A structurally-unresolvable model config caught before any request.

    ``detail`` is a short, log-friendly reason.  ``suggestions`` is a curated
    (non-exhaustive, LOCAL) list of known-good model names for the provider,
    used only to enrich the user-facing guidance.
    """

    detail: str
    suggestions: tuple[str, ...] = ()


def _known_good_models(provider: str, base_url: str) -> tuple[str, ...]:
    """Best-effort curated known-good models for *provider* — LOCAL only.

    Reads the provider profile's ``fallback_models`` (a curated, non-exhaustive
    list).  Never triggers a network fetch.  Returns ``()`` when nothing local
    is available.
    """
    try:
        from providers import get_provider_profile
    except Exception:
        return ()

    profile = None
    try:
        if provider:
            profile = get_provider_profile(provider)
        if profile is None and base_url:
            # Fall back to URL->provider inference for custom-labelled providers.
            from agent.model_metadata import _infer_provider_from_url

            inferred = _infer_provider_from_url(base_url)
            if inferred:
                profile = get_provider_profile(inferred)
    except Exception:
        return ()

    if profile is None:
        return ()
    return tuple(profile.fallback_models or ())[:5]


def _is_local_or_custom(provider: str, base_url: str) -> bool:
    """True for local/custom endpoints, whose ad-hoc model ids we can't assume.

    Their ``/models`` catalog can expose identifiers we don't control (LM
    Studio, Ollama, bespoke relays), so the whitespace heuristic is skipped
    for them to stay false-positive-free.
    """
    prov = (provider or "").strip().lower()
    if prov in {"local", "custom", "lmstudio", "ollama"}:
        return True
    try:
        from agent.model_metadata import is_local_endpoint

        return bool(is_local_endpoint(base_url))
    except Exception:
        return False


def _is_known_hosted_provider(provider: str, base_url: str) -> bool:
    """True only when provider/base_url resolve to a RECOGNISED hosted provider.

    The whitespace heuristic fires exclusively for these. For an unrecognised
    custom or corporate gateway we cannot assume the model-id convention (a
    bespoke relay could, in principle, expose space-bearing ids), so those fail
    open. This keeps the check false-positive-free for private endpoints.
    """
    prov = (provider or "").strip().lower()
    try:
        from providers import get_provider_profile

        if prov and get_provider_profile(prov) is not None:
            return True
    except Exception:
        pass
    try:
        from agent.model_metadata import _PROVIDER_PREFIXES, _infer_provider_from_url

        if prov and prov in _PROVIDER_PREFIXES and prov not in {"custom", "local"}:
            return True
        if base_url and _infer_provider_from_url(base_url):
            return True
    except Exception:
        pass
    return False


def check_model(
    provider: str, model: str, base_url: str = ""
) -> Optional[PreflightMiss]:
    """Return a :class:`PreflightMiss` iff *model* is structurally unresolvable.

    LOCAL-ONLY, no network.  ``None`` means "nothing provably wrong locally —
    proceed with the request".  See the module docstring for the boundary and
    the deliberately-excluded cases (empty model, unavailable-but-well-formed).
    """
    raw = model if isinstance(model, str) else ("" if model is None else str(model))
    stripped = raw.strip()

    # Empty / blank is intentionally allowed (provider-default state). Nothing
    # structural to check on an empty string anyway.
    if not stripped:
        return None

    # 1. A bare recognised provider prefix with no model after the ':' —
    #    e.g. "openrouter:", "anthropic:". The prefix is not itself a model.
    #    Guard against URLs (a base-url-shaped model carries a "://").
    if ":" in stripped and not raw.startswith("http"):
        prefix, _, suffix = stripped.partition(":")
        prefix_l = prefix.strip().lower()
        try:
            from agent.model_metadata import _PROVIDER_PREFIXES
        except Exception:
            _PROVIDER_PREFIXES = frozenset()
        if prefix_l in _PROVIDER_PREFIXES and not suffix.strip():
            return PreflightMiss(
                detail=(
                    f"the model name '{stripped}' is a bare '{prefix_l}:' provider "
                    "prefix with no model after the colon"
                ),
                suggestions=_known_good_models(provider or prefix_l, base_url),
            )

    # 2. Internal whitespace — no hosted provider accepts a model id with a
    #    space (a common paste of a display name, e.g. "gpt 4o"). Fires ONLY for
    #    recognised hosted providers; unknown custom/corporate gateways and
    #    local endpoints fail open (we can't assume their id convention).
    if (
        any(ch.isspace() for ch in stripped)
        and _is_known_hosted_provider(provider, base_url)
        and not _is_local_or_custom(provider, base_url)
    ):
        return PreflightMiss(
            detail=(
                f"the model name '{stripped}' contains whitespace, which no "
                "hosted provider accepts as an identifier"
            ),
            suggestions=_known_good_models(provider, base_url),
        )

    return None
