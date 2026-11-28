
def test_cron_platform_uses_the_higher_ceiling():
    """A cron agent's request budget is raised to cron_api_max_retries (#2376)."""
    from agent.retry_utils import retry_budget_for

    class _Agent:
        _api_max_retries = 3
        _cron_api_max_retries = 15
        platform = "cron"

    assert retry_budget_for(_Agent()) == 15


def test_interactive_platform_keeps_its_own_ceiling():
    from agent.retry_utils import retry_budget_for

    class _Agent:
        _api_max_retries = 2
        _cron_api_max_retries = 15
        platform = "cli"

    assert retry_budget_for(_Agent()) == 2


def test_cron_ceiling_never_lowers_a_larger_interactive_budget():
    from agent.retry_utils import retry_budget_for

    class _Agent:
        _api_max_retries = 20
        _cron_api_max_retries = 5
        platform = "cron"

    assert retry_budget_for(_Agent()) == 20


def test_missing_attributes_fall_back_to_the_defaults():
    from agent.retry_utils import retry_budget_for

    class _Bare:
        platform = "cron"

    class _BareCli:
        platform = "cli"

    assert retry_budget_for(_Bare()) == 15
    assert retry_budget_for(_BareCli()) == 3
