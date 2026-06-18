import pandas as pd

from sia.agent.hitl import HITLManager
from sia.agent.nodes import infer_relationships_node, resolve_mapping_node
from sia.agent.graph import create_graph


def test_resolve_mapping_node_proposes_and_applies_context(monkeypatch):
    prepared_df = pd.DataFrame(
        {
            "date": ["2026-03-20", None],
            "clicks": [5, ""],
            "total_cost": [10.5, 20.0],
        }
    )

    def fake_load_scoped_dataframe(_file_path, _sheet_name, _scoped_source):
        return prepared_df.copy(), {"sheet_name": "Sheet1", "header_row": 0, "scope_type": "demarcated_table"}

    monkeypatch.setattr("sia.agent.nodes.load_scoped_dataframe", fake_load_scoped_dataframe)

    state = {
        "file_path": "uploads/example.xlsx",
        "sheet_name": "Sheet1",
        "target_template": {
            "x_scope": {"uid_hierarchy": ["date"], "metrics": ["clicks", "total_cost"], "supporting_columns": []},
            "business_logic": {"column_rules": []},
            "aggregation_logic": {},
            "properties": {
                "date": {"type": "string"},
                "clicks": {"type": "integer"},
                "total_cost": {"type": "number"},
            },
        },
        "approved_mappings": [],
        "business_rules": [{"target_column": "clicks", "rule_type": "fill_blank", "rule_expression": "0"}],
        "source_metadata": {
            "source_id": "src_001",
            "file_name": "example.xlsx",
            "sheet_name": "Sheet1",
            "source_type": "paid_media",
        },
        "context_packet": {"user_notes": ["Use execution date"]},
        "scoped_source": {"sheet_name": "Sheet1"},
        "current_df": None,
        "current_frame": None,
        "requires_review": False,
        "review_reason": "",
        "hitl_checkpoints": [],
        "hitl_manager": None,
        "llm_client": None,
    }

    result = resolve_mapping_node(state)

    assert result["mapping_summary"]["unresolved_items"] == []
    assert len(result["approved_mappings"]) == 3
    assert result["current_df"]["clicks"].tolist() == [5, 0]
    assert result["context_packet"]["planning_summary"]["mapping_summary"]["mapped_count"] == 3


def test_resolve_mapping_node_respects_approved_mappings_and_discards_columns(monkeypatch):
    prepared_df = pd.DataFrame(
        {
            "Campaign": ["A", "B"],
            "Spend": [10, 20],
            "Ignore Me": ["x", "y"],
        }
    )

    def fake_load_scoped_dataframe(_file_path, _sheet_name, _scoped_source):
        return prepared_df.copy(), {"sheet_name": "Sheet1", "header_row": 0, "scope_type": "demarcated_table"}

    monkeypatch.setattr("sia.agent.nodes.load_scoped_dataframe", fake_load_scoped_dataframe)

    state = {
        "file_path": "uploads/example.xlsx",
        "sheet_name": "Sheet1",
        "target_template": {
            "x_scope": {"uid_hierarchy": ["campaign_name"], "metrics": ["total_cost"], "supporting_columns": []},
            "business_logic": {"column_rules": []},
            "aggregation_logic": {},
            "properties": {
                "campaign_name": {"type": "string"},
                "total_cost": {"type": "number"},
            },
        },
        "approved_mappings": [
            {"source_column": "Campaign", "target_column": "campaign_name", "decision": "Keep"},
            {"source_column": "Spend", "target_column": "total_cost", "decision": "Keep"},
            {"source_column": "Ignore Me", "target_column": "No match", "decision": "Discard"},
        ],
        "business_rules": [],
        "source_metadata": {"source_id": "src_002", "file_name": "example.xlsx", "sheet_name": "Sheet1"},
        "context_packet": {},
        "scoped_source": {"sheet_name": "Sheet1"},
        "current_df": None,
        "current_frame": None,
        "requires_review": False,
        "review_reason": "",
        "hitl_checkpoints": [],
        "hitl_manager": None,
        "llm_client": None,
    }

    result = resolve_mapping_node(state)

    assert list(result["current_df"].columns) == ["campaign_name", "total_cost"]
    assert result["mapping_summary"]["mapped_count"] == 2
    assert result["mapping_summary"]["unresolved_items"] == []


def test_graph_places_mapping_after_structure():
    graph = create_graph()
    graph_data = getattr(graph, "get_graph", lambda: None)()
    nodes = getattr(graph_data, "nodes", {}) if graph_data is not None else {}
    edges = getattr(graph_data, "edges", []) if graph_data is not None else {}

    edge_pairs = {(edge.source, edge.target) for edge in edges}
    assert "analyze_structure" in nodes
    assert "resolve_mapping" in nodes
    assert "infer_relationships" not in nodes
    assert ("load_file", "analyze_structure") in edge_pairs
    assert ("analyze_structure", "resolve_mapping") in edge_pairs
    assert ("resolve_mapping", "generate_plan") in edge_pairs


def test_infer_relationships_node_requests_review_for_multi_source_context():
    state = {
        "context_packet": {
            "available_source_summaries": [
                {
                    "source_id": "src_meta",
                    "file_name": "meta.xlsx",
                    "sheet_name": "Sheet1",
                    "variable_type": "Paid Media impressions",
                    "uid": ["date", "channel", "publisher_name"],
                    "date_granularity": "daily",
                    "contains_main_data": True,
                    "mapped_targets": ["date", "publisher_name", "channel", "impressions"],
                },
                {
                    "source_id": "src_youtube",
                    "file_name": "youtube.xlsx",
                    "sheet_name": "Sheet1",
                    "variable_type": "Paid Media impressions",
                    "uid": ["date", "channel", "publisher_name"],
                    "date_granularity": "daily",
                    "contains_main_data": True,
                    "mapped_targets": ["date", "publisher_name", "channel", "impressions"],
                },
            ]
        },
        "approved_relationships": [],
        "hitl_checkpoints": [],
        "hitl_manager": HITLManager(),
    }

    result = infer_relationships_node(state)

    assert result["requires_review"] is True
    assert result["hitl_pending_approval"] is True
    assert result["hitl_pause_type"] == "file_relationship_review"
    assert result["relationship_proposals"][0]["relationship_kind"] == "union"


def test_infer_relationships_node_defers_graph_hitl_when_flag_set():
    state = {
        "context_packet": {
            "available_source_summaries": [
                {
                    "source_id": "src_a",
                    "file_name": "a.xlsx",
                    "sheet_name": "Sheet1",
                    "variable_type": "Paid Media impressions",
                    "uid": ["date"],
                    "date_granularity": "daily",
                    "contains_main_data": True,
                    "mapped_targets": ["date"],
                },
                {
                    "source_id": "src_b",
                    "file_name": "b.xlsx",
                    "sheet_name": "Sheet1",
                    "variable_type": "Paid Media impressions",
                    "uid": ["date"],
                    "date_granularity": "daily",
                    "contains_main_data": True,
                    "mapped_targets": ["date"],
                },
            ],
            "defer_graph_file_relationship_review": True,
        },
        "approved_relationships": [],
        "hitl_checkpoints": [],
        "hitl_manager": HITLManager(),
    }

    result = infer_relationships_node(state)

    assert result["requires_review"] is False
    assert result["hitl_pending_approval"] is False
    assert result.get("hitl_pause_type") is None
    assert result["trace_steps"][0]["status"] == "deferred_post_execution"
    assert result["relationship_proposals"]
