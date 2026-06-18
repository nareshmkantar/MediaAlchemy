"""Tests for tools_history accumulation in agent state."""

from sia.agent.state import add_tool_execution, create_initial_state


def test_add_tool_execution_accumulates_with_base_history():
    state = create_initial_state("dummy.xlsx")
    h: list = list(state["tools_history"])
    h = add_tool_execution(
        state,
        "transform.rename",
        {},
        True,
        "ok",
        10,
        10,
        1.0,
        base_history=h,
    )
    h = add_tool_execution(
        state,
        "transform.aggregate_weekly",
        {},
        True,
        "ok",
        10,
        8,
        2.0,
        base_history=h,
    )
    assert [r["tool"] for r in h] == ["transform.rename", "transform.aggregate_weekly"]
    assert h[0]["step"] == 1
    assert h[1]["step"] == 2


def test_add_tool_execution_without_base_history_uses_state_only():
    state = create_initial_state("dummy.xlsx")
    h1 = add_tool_execution(state, "a", {}, True, "", 0, 0, 0.0)
    assert len(h1) == 1 and h1[0]["tool"] == "a"
    # Second call still reads empty state — legacy single-append use case
    h2 = add_tool_execution(state, "b", {}, True, "", 0, 0, 0.0)
    assert len(h2) == 1 and h2[0]["tool"] == "b"
