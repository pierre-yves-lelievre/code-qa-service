"""Planner: standalone rewrite of a follow-up, history cap, and the fallback on any failure."""

import pytest

from app.config import settings
from app.errors import ProviderError
from app.llm import FakeLLM, Usage
from app.planning import PLAN_MAX_TOKENS, PLAN_SCHEMA, Plan, Turn, plan

FIRST = Turn("What does crud.authenticate do?", "It checks a password against the stored hash.")


def test_follow_up_is_rewritten_into_a_standalone_query():
    reply = {
        "query": "crud.authenticate tests",
        "identifiers": ["crud.authenticate"],
        "intent": "lookup",
    }
    llm = FakeLLM([reply])
    result = plan("what about its tests?", [FIRST], llm)
    assert (result.query, result.identifiers, result.intent, result.planner) == (
        "crud.authenticate tests",
        ("crud.authenticate",),
        "lookup",
        "ok",
    )
    assert result.usage.input > 0 and result.usage.output > 0
    (request,) = llm.requests
    assert request["messages"] == [
        {"role": "user", "content": FIRST.question},
        {"role": "assistant", "content": FIRST.answer},
        {"role": "user", "content": "what about its tests?"},
    ]
    assert request["schema"] == PLAN_SCHEMA
    assert (request["max_tokens"], request["timeout"]) == (
        PLAN_MAX_TOKENS,
        settings.planner_timeout_s,
    )


def test_history_is_capped_at_the_last_four_turns():
    history = [Turn(f"question {i}", f"answer {i}") for i in range(6)]
    llm = FakeLLM()
    plan("and then?", history, llm)
    messages = llm.requests[0]["messages"]
    assert len(messages) == 2 * 4 + 1
    assert messages[0] == {"role": "user", "content": "question 2"}


def test_unscripted_fake_plans_the_question_as_its_own_query():
    result = plan("Where is slugify defined?", [], FakeLLM())
    assert (result.query, result.intent, result.planner) == (
        "Where is slugify defined?",
        "explain",
        "ok",
    )


@pytest.mark.parametrize(
    "reply",
    [
        {"query": "slugify", "identifiers": [], "intent": "guess"},
        {"query": "   ", "identifiers": [], "intent": "lookup"},
        {"query": "slugify", "identifiers": "slugify", "intent": "lookup"},
        {"query": "slugify", "identifiers": [1], "intent": "lookup"},
        {},
    ],
)
def test_invalid_plan_falls_back_to_the_raw_question_keeping_the_usage(reply: dict):
    result = plan("where is slugify?", [], FakeLLM([reply]))
    assert result.planner == "fallback"
    assert (result.query, result.identifiers, result.intent) == ("where is slugify?", (), "explain")
    assert result.usage.input > 0  # the call was made and paid for


def test_planner_error_falls_back_with_no_usage():
    result = plan("where is slugify?", [], FakeLLM([ProviderError("Claude timed out.")]))
    assert result == Plan("where is slugify?", (), "explain", "fallback", Usage())
