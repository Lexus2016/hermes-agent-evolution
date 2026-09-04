# -*- coding: utf-8 -*-
"""Provenance is classified at ingress and stamped in exactly one place.

The role label is not a trust boundary: the runtime writes ``role="user"`` rows
out of tool output in several places. ``origin`` records which channel a row
really came from, and the rule that keeps it meaningful is that only a genuine
human surface may say ``human``.

Two properties are asserted:

* **One stamping point.** Every surface converges on ``run_conversation`` →
  ``turn_context``, so the value is applied once rather than in twenty adapters,
  any one of which could ship ``human`` by copy-paste.
* **A closed allowlist.** The inventory below is the complete set of places
  allowed to assert human provenance. A new one has to be added here
  deliberately, which is the review this field exists to force.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestStampingPoint:
    """``turn_context`` is the single place that writes the field."""

    @staticmethod
    def _stamp(origin):
        """Reproduce the stamp exactly as turn_context applies it."""
        from agent.message_metadata import normalize_message_origin

        user_msg = {"role": "user", "content": "x"}
        stamped = normalize_message_origin(origin)
        if stamped:
            user_msg["origin"] = stamped
        return user_msg

    @pytest.mark.parametrize("origin", ["human", "runtime", "api"])
    def test_known_values_are_stamped(self, origin):
        assert self._stamp(origin)["origin"] == origin

    @pytest.mark.parametrize(
        "origin", [None, "", "HUMAN", "human ", "trusted", 1, True, [], {}]
    )
    def test_unknown_values_leave_the_turn_untrusted(self, origin):
        assert "origin" not in self._stamp(origin)

    def test_turn_context_stamps_through_the_normalizer(self):
        source = (REPO / "agent" / "turn_context.py").read_text(encoding="utf-8")
        assert "normalize_message_origin(persist_user_origin)" in source, (
            "the stamp must go through the normalizer, or an unvalidated value "
            "from any caller becomes trusted provenance"
        )

    def test_the_parameter_is_threaded_end_to_end(self):
        """A parameter that stops halfway is worse than none: the caller
        believes it classified the turn and nothing carries the answer."""
        for rel in (
            "run_agent.py",
            "agent/conversation_loop.py",
            "agent/turn_context.py",
        ):
            source = (REPO / rel).read_text(encoding="utf-8")
            assert "persist_user_origin" in source, rel


class TestGatewayClassification:
    """Three conditions, each ruling out a real case that bit the first attempt.

    ``internal`` alone was not enough: it defaults to False and several
    runtime-generated events omit it (the watch heartbeat, goal continuations,
    the goal kickoff), so "not internal" classified them as human. Setting
    ``internal=True`` on them was not an option either — that flag also
    bypasses user authorization checks.
    """

    SERVICE = {"webhook", "msgraph_webhook", "api_server", "relay"}

    @staticmethod
    def _classify(*, internal=False, raw_message="payload", platform="telegram"):
        return (
            "human"
            if (
                not internal
                and raw_message is not None
                and platform not in TestGatewayClassification.SERVICE
            )
            else "runtime"
        )

    def test_a_real_inbound_chat_message_is_human(self):
        assert self._classify() == "human"

    def test_a_wake_up_or_delegation_reply_is_runtime(self):
        assert self._classify(internal=True) == "runtime"

    def test_a_runtime_synthesised_event_is_runtime(self):
        """The heartbeat, goal continuations and kickoff omit raw_message."""
        assert self._classify(raw_message=None) == "runtime"

    @pytest.mark.parametrize(
        "platform", ["webhook", "msgraph_webhook", "api_server", "relay"]
    )
    def test_service_channels_are_never_human(self, platform):
        """Their payload is attacker-reachable even though it is parsed."""
        assert self._classify(platform=platform) == "runtime"

    @pytest.mark.parametrize(
        "platform",
        ["telegram", "discord", "slack", "whatsapp", "signal", "matrix", "sms"],
    )
    def test_person_to_agent_channels_are_human(self, platform):
        assert self._classify(platform=platform) == "human"

    def test_the_gateway_implements_all_three_conditions(self):
        source = (REPO / "gateway" / "run.py").read_text(encoding="utf-8")
        assert 'not getattr(event, "internal", False)' in source
        assert 'getattr(event, "raw_message", None) is not None' in source
        assert "_platform_value not in _SERVICE_PLATFORMS" in source

    def test_runtime_event_constructors_still_omit_raw_message(self):
        """The discriminator only holds while this stays true, so assert it."""
        run = (REPO / "gateway" / "run.py").read_text(encoding="utf-8")
        slash = (REPO / "gateway" / "slash_commands.py").read_text(encoding="utf-8")
        for source, marker in (
            (run, "hb_event = MessageEvent("),
            (run, "cont_event = MessageEvent("),
            (slash, "kickoff_event = MessageEvent("),
        ):
            start = source.index(marker)
            block = source[start : source.index(")", source.index("\n", start))]
            assert "raw_message" not in block, (
                f"{marker} now sets raw_message; the human classification in "
                "gateway/run.py would start treating it as a person's turn"
            )


class TestHumanAllowlist:
    """The complete set of places allowed to assert human provenance.

    Adding a surface means adding it here, on purpose. That is the point: this
    test exists so a new user-row producer cannot quietly inherit trust, which
    is how the role label became untrustworthy in the first place.
    """

    ALLOWED = {
        # Real inbound platform messages (Telegram, Discord, Slack, WhatsApp,
        # Signal, …) — every adapter funnels through this one classification.
        "gateway/run.py",
        # A person typing at the interactive CLI prompt.
        "cli.py",
        # A person typing in the desktop client.
        "tui_gateway/server.py",
        # A person prompting from their editor over ACP.
        "acp_adapter/server.py",
    }

    # AST, not text. Two earlier attempts used regexes and both were wrong in
    # opposite directions: one silently stopped matching the gateway once its
    # assignment became a multi-line conditional, the other tripped on prose in
    # a docstring that merely explains the design. A guard that fires on
    # documentation gets deleted; one that matches nothing is worse than none.
    _SCOPE = (
        "agent", "gateway", "hermes_cli", "tui_gateway", "acp_adapter",
        "cron", "tools", "providers", "evolution",
    )
    _ROOT_FILES = ("cli.py", "run_agent.py", "hermes_state.py", "batch_runner.py")

    @staticmethod
    def _asserts_human(text: str) -> bool:
        """True when the module assigns human provenance in real code."""
        import ast

        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False

        def is_human(node) -> bool:
            return isinstance(node, ast.Constant) and node.value == "human"

        def mentions_human(node) -> bool:
            return any(is_human(n) for n in ast.walk(node))

        for node in ast.walk(tree):
            # persist_user_origin="human"  (keyword argument)
            if isinstance(node, ast.keyword) and node.arg == "persist_user_origin":
                if mentions_human(node.value):
                    return True
            # persist_user_origin = "human"  /  = "human" if ... else ...
            if isinstance(node, ast.Assign):
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                if "persist_user_origin" in names and mentions_human(node.value):
                    return True
            # {"origin": "human"} or {"persist_user_origin": "human"}
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value in ("origin", "persist_user_origin")
                        and mentions_human(value)
                    ):
                        return True
            # d["persist_user_origin"] = "human"
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value
                        in ("origin", "persist_user_origin")
                        and mentions_human(node.value)
                    ):
                        return True
        return False

    def _offenders(self):
        found = set()

        def scan(path: pathlib.Path) -> None:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return
            if self._asserts_human(text):
                found.add(path.relative_to(REPO).as_posix())

        for directory in self._SCOPE:
            base = REPO / directory
            if not base.is_dir():
                continue
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                scan(path)
        for name in self._ROOT_FILES:
            path = REPO / name
            if path.is_file():
                scan(path)
        return found

    def test_only_the_allowlisted_surfaces_assert_human(self):
        found = self._offenders()
        unexpected = found - self.ALLOWED
        assert not unexpected, (
            "a new place asserts human provenance without review: "
            f"{sorted(unexpected)}. If it really is a human channel, add it to "
            "ALLOWED with a one-line reason; if not, classify it as runtime."
        )

    def test_the_allowlist_is_not_stale(self):
        """A surface that stopped stamping should leave the list, or the guard
        slowly becomes a list of places that no longer do anything."""
        found = self._offenders()
        missing = self.ALLOWED - found
        assert not missing, f"allowlisted but no longer stamping: {sorted(missing)}"


class TestNonHumanSurfacesAreExplicit:
    """Background and preview runs are the agent talking to itself."""

    @pytest.mark.parametrize(
        "rel",
        [
            "tui_gateway/methods_prompt.py",
            "hermes_cli/cli_commands_mixin.py",
        ],
    )
    def test_background_runs_declare_runtime(self, rel):
        source = (REPO / rel).read_text(encoding="utf-8")
        assert 'persist_user_origin="runtime"' in source

    def test_the_api_turn_itself_is_marked_api(self):
        """Not just its history: /v1/responses takes the current turn from the
        request body too."""
        source = (REPO / "gateway" / "platforms" / "api_server.py").read_text(
            encoding="utf-8"
        )
        assert 'persist_user_origin="api"' in source

    def test_api_supplied_messages_are_marked_api(self):
        source = (REPO / "gateway" / "platforms" / "api_server.py").read_text(
            encoding="utf-8"
        )
        assert source.count('"origin": "api"') >= 2, (
            "/v1/responses takes the role straight from the caller, so its "
            "messages must carry their own provenance class"
        )
