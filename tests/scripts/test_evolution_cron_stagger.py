"""Regression test for issue #1673 — cron schedule staggering.

Ensures evolution cron jobs do NOT all fire at the same wall-clock minute
(:00), which caused ~90 HTTP 429 errors from concurrent API calls to the
zai/glm-5.2 provider.

The test checks that the highest-frequency LLM-consuming jobs (analysis,
research, ci-diagnosis) are staggered off the :00/:30 marks where hydra fires.
"""

import re
from pathlib import Path

import pytest

CRON_DIR = Path(__file__).resolve().parents[2] / "cron" / "evolution"


def _schedule(yaml_name: str) -> str:
    """Extract the raw cron schedule expression from a cron/evolution/*.yaml."""
    text = (CRON_DIR / yaml_name).read_text()
    m = re.search(r'^schedule:\s*"([^"]+)"', text, re.M)
    assert m, f"{yaml_name} has no schedule field"
    return m.group(1)


class TestCronStagger:
    """Issue #1673: concurrent cron jobs hitting the API simultaneously
    cause HTTP 429 rate-limit errors. Stagger schedules so they don't
    collide at :00."""

    def test_analysis_off_zero_minute(self):
        """analysis was `0 1,5,9,...` — now staggered off :00."""
        sched = _schedule("analysis.yaml")
        minute = sched.split()[0]
        assert minute != "0", (
            f"analysis.yaml schedule '{sched}' fires at :00 — collides with "
            f"hydra (*/30). Stagger by at least 5 minutes (#1673)."
        )

    def test_research_off_zero_minute(self):
        """research was `0 9` — now staggered off :00."""
        sched = _schedule("research.yaml")
        minute = sched.split()[0]
        assert minute != "0", (
            f"research.yaml schedule '{sched}' fires at :00 — collides with "
            f"hydra (*/30) and analysis at 09:00. Stagger (#1673)."
        )

    def test_ci_diagnosis_off_zero_minute(self):
        """ci-diagnosis was `*/30` — now staggered off :00/:30."""
        sched = _schedule("ci-diagnosis.yaml")
        # `*/30` fires at :00 and :30 — same as hydra. Must be staggered.
        assert sched != "*/30 * * * *", (
            "ci-diagnosis.yaml schedule '*/30 * * * *' fires at :00/:30 — "
            "identical to hydra. Stagger to avoid collisions (#1673)."
        )

    def test_no_three_way_collision_at_0900(self):
        """The specific 09:00 collision identified in #1673:
        hydra + research + analysis all fired at 09:00:00.
        After staggering, at most ONE of them should fire on the :00 minute."""
        hydra_sched = _schedule("hydra.yaml")
        research_sched = _schedule("research.yaml")
        analysis_sched = _schedule("analysis.yaml")

        def fires_at_minute_zero(sched: str) -> bool:
            """True if this schedule fires at minute :00 of any hour."""
            minute_field = sched.split()[0]
            if minute_field == "*":
                return True
            if minute_field.startswith("*/"):
                step = int(minute_field[2:])
                return 0 % step == 0  # :00 always matches */N
            return "0" in minute_field.split(",")

        colliding = []
        if fires_at_minute_zero(hydra_sched):
            colliding.append("hydra")
        if fires_at_minute_zero(research_sched):
            colliding.append("research")
        if fires_at_minute_zero(analysis_sched):
            colliding.append("analysis")

        # Hydra at */30 fires at :00 — that's expected (it's the orchestrator
        # with a gate). But research and analysis must NOT also fire at :00.
        assert "research" not in colliding, (
            "research still fires at :00 — collides with hydra at 09:00 (#1673)"
        )
        assert "analysis" not in colliding, (
            "analysis still fires at :00 — collides with hydra at 09:00 (#1673)"
        )
