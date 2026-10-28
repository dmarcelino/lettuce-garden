"""The rule both garden and gmail-triggers follow for when they want a fresh
agent list (garden_common.executor.AgentAsks); garden-common has no test run of
its own, and this image installs it."""

from garden_common.executor import AGENTS_FRESH_SECONDS, AgentAsks

T = 1_800_000_000


def test_a_list_is_wanted_until_one_arrives_then_again_once_stale():
    asks = AgentAsks()
    assert asks.wanted(T)
    asks = asks.read().listed(T)
    assert not asks.wanted(T + AGENTS_FRESH_SECONDS)
    assert asks.wanted(T + AGENTS_FRESH_SECONDS + 1)


def test_an_ask_is_answered_only_by_a_list_made_after_the_executor_saw_it():
    asks = AgentAsks().read().listed(T)
    asks = asks.ask()
    assert asks.wanted(T)
    # A list sent without a status read since the ask may predate it.
    assert asks.listed(T).wanted(T)
    asks = asks.read()
    # Asked again while the executor builds its list: that list doesn't answer it.
    asks = asks.ask().listed(T)
    assert asks.wanted(T)
    asks = asks.read().listed(T)
    assert not asks.wanted(T)


def test_reading_the_status_keeps_an_unanswered_ask():
    asks = AgentAsks().read().listed(T).ask().read()
    assert asks.read().wanted(T)
    assert asks == asks.read()
