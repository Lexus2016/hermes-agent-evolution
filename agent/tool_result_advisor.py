"""Append untrusted delegation advice without changing tool-result envelopes."""
from __future__ import annotations
import json
import logging
from utils import safe_json_loads
from agent.tool_dispatch_helpers import _is_multimodal_tool_result, _append_subdir_hint_to_multimodal


def attach_delegation_advice(agent, function_name, function_result, persisted_result, *, blocked):
    # The delegated controller consumes the real result before the append-only
    # persistence fence. Advice remains tool data, never a synthetic user turn.
    escalation = getattr(agent, "_delegation_escalation", None)
    if escalation is None:
        return persisted_result
    from tools.delegation_escalation import EscalationRun
    if not isinstance(escalation, EscalationRun):
        return persisted_result
    try:
        advice = escalation.contact(function_name, function_result, blocked=blocked)
        if not advice:
            return persisted_result
        if _is_multimodal_tool_result(persisted_result):
            _append_subdir_hint_to_multimodal(persisted_result, advice)
            return persisted_result
        # Keep diagnosed failures parseable. Advice remains untrusted tool context.
        payload = safe_json_loads(persisted_result)
        if isinstance(payload, dict):
            key = "delegation_advisor_context"
            while key in payload:
                key = "_" + key
            payload[key] = advice
            persisted_result = json.dumps(payload, ensure_ascii=False)
        else:
            persisted_result += advice
    except Exception:
        logging.warning("Delegation contact processing failed", exc_info=True)
    return persisted_result
