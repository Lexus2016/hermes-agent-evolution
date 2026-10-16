"""Provider billing guidance and structured turn-failure presentation."""
from __future__ import annotations
from typing import Optional
from utils import base_url_host_matches

def _nous_entitlement_message(capability: str) -> str:
    try:
        from hermes_cli.nous_account import (
            format_nous_portal_entitlement_message,
            get_nous_portal_account_info,
        )
        account_info = get_nous_portal_account_info(force_fresh=True)
        return format_nous_portal_entitlement_message(
            account_info, capability=capability, in_chat=True
        ) or ""
    except Exception:
        return ""

def _print_guidance(agent, message: str) -> bool:
    """Print each line of ``message`` as a 💡 hint; False when there is nothing to print."""
    if not message:
        return False
    for line in message.splitlines():
        agent._vprint(f"{agent.log_prefix}   💡 {line}", force=True, diagnostic=True)
    return True

def _print_nous_entitlement_guidance(agent, capability: str) -> bool:
    return _print_guidance(agent, _nous_entitlement_message(capability))

def _is_nous_inference_route(provider: str, base_url: str) -> bool:
    return (provider or "").strip().lower() == "nous" or base_url_host_matches(
        str(base_url or ""), "inference-api.nousresearch.com"
    )

def _billing_or_entitlement_message(
    *, capability: str, provider: str, base_url: str, model: str, unverified: bool = False
) -> str:
    if _is_nous_inference_route(provider, base_url):
        return _nous_entitlement_message(capability)

    provider_label = (provider or "").strip() or "the selected provider"
    model_label = (model or "").strip() or "the selected model"

    # Anthropic Pro/Max OAuth surfaces "extra usage" exhaustion as a hard 400 — "add credits"
    # does not apply. ``unverified`` (#82154): the same 400 is returned for a server-side
    # content-filter rejection, so hedge and name the other cause.
    if (provider or "").strip().lower() == "anthropic":
        switch = (
            "You can also switch to an Anthropic API key or another provider with "
            "/model <model> --provider <provider>."
        )
        if unverified:
            return "\n".join([
                f"{provider_label} reported that your Claude subscription usage may be exhausted for "
                f"{model_label} (included quota + extra-usage credits) — but this specific error is "
                "not proof of a billing problem.",
                "If https://claude.ai/settings/usage still shows quota remaining, this is probably NOT "
                "a billing problem: on a Claude subscription (OAuth) token Anthropic returns this same "
                "message when its content filter rejects part of the request — typically a phrase in "
                "the system prompt.",
                "If usage really is exhausted: wait for the billing cycle to reset, or add extra usage "
                "at https://claude.ai/settings/usage",
                switch,
                # The exhaustion latch replays the stored error without a request.
                "Retry with a fresh credential state: `hermes auth reset anthropic`. Until that "
                "cooldown clears, this error can be replayed from cache without contacting the API.",
            ])
        return "\n".join([
            f"{provider_label} reported that your Claude subscription usage is exhausted for "
            f"{model_label} (included quota + extra-usage credits).",
            "Options: wait for the billing cycle to reset, or add extra usage at https://claude.ai/settings/usage",
            switch,
        ])

    # Provider-agnostic billing URL so every text surface shows the same actionable link.
    try:
        from agent.billing_links import build_billing_block
        _link = build_billing_block(provider=provider, base_url=base_url, model=model)
        provider_label = _link.provider_label or provider_label
        billing_url = _link.billing_url
    except Exception:
        billing_url = None
    return "\n".join([
        "HTTP 402 Payment Required — the provider account is out of credit or quota.",
        f"{provider_label} reported that billing, credits, or account entitlement is exhausted for {model_label}.",
        "Add credits or update billing with that provider, then retry.",
        *([f"{provider_label} billing: {billing_url}"] if billing_url else []),
        "You can switch providers temporarily with /model <model> --provider <provider>.",
    ])

def _billing_block_dict(provider, base_url, model, message="", *, unverified: bool = False) -> Optional[dict]:
    """Best-effort structured billing descriptor (None if billing_links is unavailable)."""
    try:
        from agent.billing_links import build_billing_block
        block = build_billing_block(
            provider=provider, base_url=str(base_url), model=model, message=message
        ).to_dict()
    except Exception:
        return None
    if block is not None and unverified:
        block["unverified"] = True  # every surface rendering the block can hedge too (#82154)
    return block

def _billing_terminal_label(summary: str, unverified: bool) -> str:
    """Terminal-failure prefix for a billing-classified error; ``unverified`` (#82154) must
    not assert exhaustion as fact."""
    if unverified:
        return (
            "Provider reported usage/credit exhaustion (unverified — the same "
            f"error can be a content-filter rejection, not billing): {summary}"
        )
    return f"Billing or credits exhausted: {summary}"

def _billing_failure_result(
    *, classified, summary: str, messages, api_call_count: int, provider: str, base_url, model: str,
    guidance: Optional[str] = None,
) -> dict:
    """Structured terminal result for a billing-classified failure — the single construction
    point for the non-retryable abort and max-retries paths (#82154)."""
    unverified = bool(getattr(classified, "billing_unverified", False))
    if guidance is None:
        guidance = _billing_or_entitlement_message(
            capability="model access", provider=provider, base_url=str(base_url), model=model,
            unverified=unverified,
        )
    final = _billing_terminal_label(summary, unverified) + (f"\n\n{guidance}" if guidance else "")
    return {
        "final_response": final, "messages": messages, "api_calls": api_call_count,
        "completed": False, "failed": True, "error": summary,
        "failure_reason": classified.reason.value,
        # Classifier's own retry verdict so the UI shows Retry only when a re-run can differ.
        "failure_retryable": bool(classified.retryable),
        "billing_unverified": unverified,
        "billing_block": _billing_block_dict(provider, base_url, model, guidance, unverified=unverified),
    }

def _print_billing_or_entitlement_guidance(
    agent, *, capability: str, provider: str, base_url: str, model: str, unverified: bool = False
) -> bool:
    return _print_guidance(agent, _billing_or_entitlement_message(
        capability=capability, provider=provider, base_url=base_url, model=model,
        unverified=unverified,
    ))
