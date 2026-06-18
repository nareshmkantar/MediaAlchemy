"""Tests for cross-source derived fields."""

from __future__ import annotations

import pandas as pd
import pytest

from sia.agent.cross_source import (
    DerivedFieldPlan,
    apply_derived_field_plan,
    parse_expression,
    validate_derived_field_plan,
)
from sia.agent.source_graph import (
    RelationshipEdge,
    SourceGraph,
    SourceNode,
)


def _graph_with_fact_and_lookup():
    return SourceGraph(
        nodes=[
            SourceNode(source_id="fact", columns=["date", "campaign_id", "spend"]),
            SourceNode(source_id="lookup", columns=["campaign_id", "campaign_group"]),
        ],
        edges=[
            RelationshipEdge(
                relationship_id="fact_lookup",
                from_source_id="fact",
                to_source_id="lookup",
                relationship_kind="lookup",
                join_keys=[{"from": "campaign_id", "to": "campaign_id"}],
            )
        ],
    )


def test_parse_expression_supports_ref_literal_and_coalesce():
    ref = parse_expression("lookup.campaign_group")
    assert ref == {"kind": "ref", "source_id": "lookup", "column": "campaign_group"}

    lit = parse_expression('literal:"Unknown"')
    assert lit == {"kind": "literal", "value": "Unknown"}

    coalesce = parse_expression('COALESCE(lookup.campaign_group, literal:"Other")')
    assert coalesce["kind"] == "coalesce"
    assert coalesce["args"][0]["kind"] == "ref"
    assert coalesce["args"][1]["value"] == "Other"


def test_parse_expression_rejects_arithmetic_for_now():
    with pytest.raises(ValueError):
        parse_expression("fact.spend + lookup.cpi")


def test_validate_detects_missing_column_and_unknown_source():
    graph = _graph_with_fact_and_lookup()
    plan = DerivedFieldPlan(
        derived_field_id="d1",
        target_source_id="fact",
        target_column="campaign_group",
        expression="lookup.unknown_column",
    )
    result = validate_derived_field_plan(plan, graph)
    assert not result.ok
    assert any("unknown_column" in issue.code for issue in result.issues)

    plan_bad_source = DerivedFieldPlan(
        derived_field_id="d2",
        target_source_id="fact",
        target_column="campaign_group",
        expression="ghost.campaign_group",
    )
    result_bad = validate_derived_field_plan(plan_bad_source, graph)
    assert not result_bad.ok
    assert any(issue.code == "unknown_source" for issue in result_bad.issues)


def test_validate_requires_approved_join_path():
    graph = SourceGraph(
        nodes=[
            SourceNode(source_id="fact", columns=["campaign_id", "spend"]),
            SourceNode(source_id="lookup", columns=["campaign_id", "campaign_group"]),
        ],
        edges=[],  # no approved relationship
    )
    plan = DerivedFieldPlan(
        derived_field_id="d_missing_path",
        target_source_id="fact",
        target_column="campaign_group",
        expression="lookup.campaign_group",
    )
    result = validate_derived_field_plan(plan, graph)
    assert not result.ok
    assert any(issue.code == "no_join_path" for issue in result.issues)


def test_apply_derived_field_plan_materializes_lookup_with_fallback():
    graph = _graph_with_fact_and_lookup()
    fact = pd.DataFrame(
        {
            "date": ["2025-01-01", "2025-01-02", "2025-01-03"],
            "campaign_id": ["c1", "c2", "c3"],
            "spend": [100.0, 200.0, 150.0],
        }
    )
    lookup = pd.DataFrame(
        {
            "campaign_id": ["c1", "c2"],
            "campaign_group": ["Brand", "Performance"],
        }
    )
    plan = DerivedFieldPlan(
        derived_field_id="d_lookup",
        target_source_id="fact",
        target_column="campaign_group",
        expression='COALESCE(lookup.campaign_group, literal:"Other")',
    )

    result = apply_derived_field_plan(
        plan,
        graph=graph,
        target_frame=fact,
        frames_by_source={"lookup": lookup},
    )

    assert list(result["campaign_group"]) == ["Brand", "Performance", "Other"]
    # Original frame unchanged
    assert "campaign_group" not in fact.columns


def test_apply_derived_field_plan_respects_fallback_attribute():
    graph = _graph_with_fact_and_lookup()
    fact = pd.DataFrame({"campaign_id": ["c1", "missing"], "spend": [1.0, 2.0]})
    lookup = pd.DataFrame({"campaign_id": ["c1"], "campaign_group": ["Brand"]})

    plan = DerivedFieldPlan(
        derived_field_id="d_fallback",
        target_source_id="fact",
        target_column="campaign_group",
        expression="lookup.campaign_group",
        fallback='"Unassigned"',
    )
    result = apply_derived_field_plan(
        plan,
        graph=graph,
        target_frame=fact,
        frames_by_source={"lookup": lookup},
    )
    assert list(result["campaign_group"]) == ["Brand", "Unassigned"]
