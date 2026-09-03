import pandas as pd

from sia.agent.parent_collation_graph import run_parent_collation_graph
from sia.agent.relationships import collate_frames_detailed
from sia.tools.collation_tools import aggregate_duplicate_keys, drop_duplicate_rows, reorder_columns


def test_collation_tools_reorder_and_drop():
    df = pd.DataFrame({"b": [1], "a": [2]})
    out = reorder_columns(df, ["a", "b"])
    assert list(out.columns) == ["a", "b"]
    d2 = pd.DataFrame({"k": [1, 1], "v": [1, 1]})
    out2 = drop_duplicate_rows(d2, subset=["k", "v"])
    assert len(out2) == 1


def test_parent_graph_aggregates_conflicting_numeric_on_union_keys():
    a = pd.DataFrame({"date": [1, 2], "impressions": [10, 20]})
    b = pd.DataFrame({"date": [1, 2], "impressions": [5, 10]})
    rels = [
        {
            "relationship_kind": "union",
            "source_ids": ["s1", "s2"],
            "join_keys": ["date"],
        }
    ]
    out, steps, _events = run_parent_collation_graph({"s1": a, "s2": b}, rels)
    assert len(out) == 2
    assert list(out["impressions"]) == [15, 30]
    assert any(s.get("step") == "parent_execute_merge_tools" for s in steps)


def test_collate_frames_single_source_skips_parent_graph():
    df = pd.DataFrame({"x": [1]})
    out, meta, _dbg = collate_frames_detailed({"only": df}, [])
    assert len(out) == 1
    assert meta and meta[0].get("step") == "collate_baseline"
    assert _dbg is None


def test_union_stack_dedupes_identical_rows_when_join_keys_empty():
    """Stacked union with empty join_keys should still dedupe on shared columns (intersection)."""
    a = pd.DataFrame({"date": [1], "spend": [10.0]})
    b = pd.DataFrame({"date": [1], "spend": [10.0]})
    rels = [{"relationship_kind": "union", "source_ids": ["s1", "s2"], "join_keys": []}]
    out, steps, dbg = collate_frames_detailed({"s1": a, "s2": b}, rels)
    assert len(out) == 1
    assert any(s.get("step") == "parent_execute_merge_tools" for s in steps)
    assert dbg and any(
        isinstance(e, dict) and e.get("process_type") == "tool_execution" and e.get("module") == "collation.union_stack"
        for e in dbg
    )


def test_parent_graph_resolves_join_keys_case_insensitive():
    a = pd.DataFrame({"Date": [1], "metric": [10.0]})
    b = pd.DataFrame({"Date": [1], "metric": [5.0]})
    rels = [
        {
            "relationship_kind": "union",
            "source_ids": ["s1", "s2"],
            "join_keys": ["date"],
        }
    ]
    out, _steps, _dbg = collate_frames_detailed({"s1": a, "s2": b}, rels)
    assert len(out) == 1
    assert float(out["metric"].iloc[0]) == 15.0


def test_bool_flags_do_not_force_aggregate_when_measures_match():
    """Boolean columns must not trigger aggregate_duplicate_keys when costs match."""
    from sia.agent.parent_collation_graph import diagnose_stacked_union_duplicates

    df = pd.DataFrame(
        {
            "date": [1, 1],
            "cost": [10.34, 10.34],
            "impressions": [737, 737],
            "from_file_b": [True, False],
        }
    )
    rels = [{"relationship_kind": "union", "source_ids": ["a", "b"], "join_keys": ["date"]}]
    d = diagnose_stacked_union_duplicates(df, rels, [])
    assert int(d.get("duplicate_rows_on_keys") or 0) >= 1
    assert d.get("numeric_conflict_columns") == []
    assert not d.get("numeric_conflict_on_duplicate_keys")


def test_numeric_conflict_lists_disagreeing_measure_columns():
    from sia.agent.parent_collation_graph import diagnose_stacked_union_duplicates

    df = pd.DataFrame({"date": [1, 1], "cost": [10.0, 20.0]})
    rels = [{"relationship_kind": "union", "source_ids": ["a", "b"], "join_keys": ["date"]}]
    d = diagnose_stacked_union_duplicates(df, rels, [])
    assert d.get("numeric_conflict_on_duplicate_keys")
    assert "cost" in (d.get("numeric_conflict_columns") or [])


def test_string_numeric_measures_detect_conflict_like_floats():
    """Excel-style number cells loaded as object dtype must still count as measure disagreement."""
    from sia.agent.parent_collation_graph import diagnose_stacked_union_duplicates

    df = pd.DataFrame({"date": [1, 1], "cost": ["28.8", "45.62"], "impressions": ["2108", "2759"]})
    rels = [{"relationship_kind": "union", "source_ids": ["a", "b"], "join_keys": ["date"]}]
    d = diagnose_stacked_union_duplicates(df, rels, [])
    assert d.get("numeric_conflict_on_duplicate_keys")
    cols = set(d.get("numeric_conflict_columns") or [])
    assert "cost" in cols
    assert "impressions" in cols


def test_aggregate_duplicate_keys_sums_string_numerics():
    df = pd.DataFrame({"k": [1, 1], "cost": ["10.5", "4.5"]})
    out = aggregate_duplicate_keys(df, ["k"])
    assert len(out) == 1
    assert abs(float(out["cost"].iloc[0]) - 15.0) < 1e-9


def test_diagnose_stacked_union_matches_baseline_stack():
    from sia.agent.parent_collation_graph import diagnose_stacked_union_duplicates
    from sia.agent.relationships import _collate_frames_baseline, _union_stack_column_intersection

    a = pd.DataFrame({"date": [1, 1], "x": [1, 2]})
    b = pd.DataFrame({"date": [1], "x": [3]})
    rels = [{"relationship_kind": "union", "source_ids": ["s1", "s2"], "join_keys": ["date"]}]
    frames = {"s1": a, "s2": b}
    stacked = _collate_frames_baseline(frames, rels)
    uci = _union_stack_column_intersection(frames, rels)
    d = diagnose_stacked_union_duplicates(stacked, rels, uci)
    assert int(d.get("duplicate_rows_on_keys") or 0) >= 1
    assert "date" in (d.get("subset_for_dedupe") or [])


def test_duplicate_merge_mode_keep_first_drops_despite_measure_conflict():
    a = pd.DataFrame({"date": [1], "impressions": [10]})
    b = pd.DataFrame({"date": [1], "impressions": [20]})
    rels = [
        {
            "relationship_kind": "union",
            "source_ids": ["s1", "s2"],
            "join_keys": ["date"],
            "duplicate_merge_mode": "keep_first_per_key",
        }
    ]
    out, _steps, _dbg = run_parent_collation_graph({"s1": a, "s2": b}, rels)
    assert len(out) == 1
    assert int(out["impressions"].iloc[0]) == 10


def test_duplicate_merge_mode_sum_measures_when_measures_match():
    a = pd.DataFrame({"date": [1], "impressions": [10]})
    b = pd.DataFrame({"date": [1], "impressions": [10]})
    rels = [
        {
            "relationship_kind": "union",
            "source_ids": ["s1", "s2"],
            "join_keys": ["date"],
            "duplicate_merge_mode": "sum_measures_per_key",
        }
    ]
    out, _steps, _dbg = run_parent_collation_graph({"s1": a, "s2": b}, rels)
    assert len(out) == 1
    assert int(out["impressions"].iloc[0]) == 20


def test_resolve_duplicate_merge_action_respects_mode():
    from sia.agent.parent_collation_graph import resolve_duplicate_merge_action

    assert resolve_duplicate_merge_action(2, True, "keep_first_per_key") == "drop_duplicate_rows"
    assert resolve_duplicate_merge_action(2, False, "sum_measures_per_key") == "aggregate_duplicate_keys"
    assert resolve_duplicate_merge_action(2, True, "auto") == "aggregate_duplicate_keys"
    assert resolve_duplicate_merge_action(2, False, "auto") == "drop_duplicate_rows"
    assert resolve_duplicate_merge_action(0, True, "auto") == "none"


def test_resolve_duplicate_key_groups_fallback_exact_does_not_sum_measures():
    """Without per-group decisions, identical rows must not be misclassified as partial because of __stack_order__."""
    from sia.tools.collation_tools import resolve_duplicate_key_groups

    df = pd.DataFrame({"k": [1, 1], "cost": [5.0, 5.0]})
    out = resolve_duplicate_key_groups(
        df,
        ["k"],
        {},
        default_exact="treat_duplicate",
        default_partial="keep_both",
    )
    assert len(out) == 1
    assert float(out["cost"].iloc[0]) == 5.0


def test_resolve_duplicate_key_groups_tool():
    from sia.tools.collation_tools import _duplicate_group_fingerprint, resolve_duplicate_key_groups

    df = pd.DataFrame({"k": [1, 1, 2, 2], "m": [1, 1, 10, 20]})
    g1 = _duplicate_group_fingerprint(df.iloc[0], ["k"])
    g2 = _duplicate_group_fingerprint(df.iloc[2], ["k"])
    decisions = {
        g1: {"category": "exact", "action": "treat_duplicate"},
        g2: {"category": "partial", "action": "keep_source_2"},
    }
    out = resolve_duplicate_key_groups(
        df,
        ["k"],
        decisions,
        default_exact="treat_duplicate",
        default_partial="keep_both",
    )
    assert len(out) == 2


def test_parent_graph_uses_duplicate_key_group_decisions():
    from sia.tools.collation_tools import _duplicate_group_fingerprint

    a = pd.DataFrame({"d": [1], "m": [5]})
    b = pd.DataFrame({"d": [1], "m": [6]})
    fp = _duplicate_group_fingerprint(pd.Series({"d": 1}), ["d"])
    rels = [
        {
            "relationship_kind": "union",
            "source_ids": ["s1", "s2"],
            "join_keys": ["d"],
            "duplicate_key_group_decisions": {fp: {"category": "partial", "action": "keep_source_1"}},
        }
    ]
    out, _steps, _dbg = run_parent_collation_graph({"s1": a, "s2": b}, rels)
    assert len(out) == 1
    assert int(out["m"].iloc[0]) == 5


def test_duplicate_group_fingerprint_normalizes_date_like_scalars():
    from sia.tools.collation_tools import _duplicate_group_fingerprint

    keys = ["date"]
    fp_str = _duplicate_group_fingerprint(pd.Series({"date": "2023-06-06"}), keys)
    fp_ts = _duplicate_group_fingerprint(pd.Series({"date": pd.Timestamp("2023-06-06")}), keys)
    assert fp_str == fp_ts
    fp_iso = _duplicate_group_fingerprint(pd.Series({"date": "2023-06-06T00:00:00"}), keys)
    assert fp_str == fp_iso


def test_union_duplicate_key_group_decisions_merges_past_empty_union():
    from sia.agent.parent_collation_graph import union_duplicate_key_group_decisions_from_relationships

    rels = [
        {"relationship_kind": "union", "source_ids": ["a"], "duplicate_key_group_decisions": {}},
        {
            "relationship_kind": "union",
            "source_ids": ["b", "c"],
            "duplicate_key_group_decisions": {"abc123": {"category": "exact", "action": "treat_separate"}},
        },
    ]
    out = union_duplicate_key_group_decisions_from_relationships(rels)
    assert out == {"abc123": {"category": "exact", "action": "treat_separate"}}


def test_collation_merge_reorders_columns_from_target_template():
    from sia.agent.target_template_utils import validate_template_shape

    tpl = {
        "x_scope": {
            "uid_hierarchy": ["channel", "date", "publisher"],
            "metrics": ["spends", "impressions"],
            "supporting_columns": ["flag"],
        },
        "properties": {
            "channel": {},
            "date": {"format": "date"},
            "publisher": {},
            "spends": {},
            "impressions": {},
            "flag": {},
        },
        "business_logic": {"column_rules": []},
        "aggregation_logic": {},
    }
    ok, err = validate_template_shape(tpl)
    assert ok, err
    base = {"publisher": "p", "spends": 2.0, "channel": "c", "impressions": 1, "flag": True}
    a = pd.DataFrame([{**base, "date": 1}])
    b = pd.DataFrame([{**base, "date": 2, "impressions": 3, "spends": 4.0, "flag": False}])
    rels = [
        {
            "relationship_kind": "union",
            "source_ids": ["s1", "s2"],
            "join_keys": ["date", "channel", "publisher"],
        }
    ]
    out, steps, _dbg = run_parent_collation_graph({"s1": a, "s2": b}, rels, target_template=tpl)
    assert len(out) == 2
    assert list(out.columns)[:6] == ["date", "channel", "publisher", "flag", "spends", "impressions"]
    assert any(
        s.get("step") == "parent_plan_merge_tools"
        and "collation.reorder_columns" in (s.get("tools") or [])
        for s in steps
    )


def test_enumerate_duplicate_key_groups_includes_file_and_sheet():
    from sia.agent.parent_collation_graph import enumerate_duplicate_key_groups_for_review

    stacked = pd.DataFrame(
        {
            "date": ["2025-03-01", "2025-03-01"],
            "channel": ["digital", "digital"],
            "market": ["UK", "UK"],
            "publisher": ["SiteC", "SiteC"],
            "spends": [258, 258],
            "impressions": [3030, 3030],
        }
    )
    meta = [
        {"source_id": "src_a", "file_name": "CP-07.xlsx", "sheet_name": "Digital_UK"},
        {"source_id": "src_b", "file_name": "CP-07.xlsx", "sheet_name": "Digital_UK"},
    ]
    exact, partial = enumerate_duplicate_key_groups_for_review(
        stacked,
        ["date", "channel", "market", "publisher"],
        union_break_before_row=[1],
        source_labels=["CP-07.xlsx · Digital_UK", "CP-07.xlsx · Digital_UK"],
        source_meta=meta,
        max_each=10,
    )
    assert len(exact) == 1
    assert len(partial) == 0
    rows = exact[0]["rows"]
    assert len(rows) == 2
    assert rows[0]["file_name"] == "CP-07.xlsx"
    assert rows[0]["sheet_name"] == "Digital_UK"
    assert rows[1]["sheet_name"] == "Digital_UK"
