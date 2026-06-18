"""Sanitize and merge deferred aggregate_weekly params."""
from sia.agent.post_collate_transforms import merge_deferred_post_collate_tool_lists
from sia.agent.target_template_utils import sanitize_aggregate_weekly_params


def test_sanitize_drops_legacy_week_start_col_when_date_col_is_date():
    raw = {
        "date_col": "date",
        "group_by_cols": ["channel", "publisher"],
        "metric_rules": {"spends": "sum"},
        "week_start_col": "week_start",
        "drop_original_date": True,
    }
    out = sanitize_aggregate_weekly_params(raw)
    assert "week_start_col" not in out
    assert out["date_col"] == "date"


def test_sanitize_keeps_week_start_col_when_template_date_is_week_start():
    raw = {
        "date_col": "week_start",
        "metric_rules": {"spends": "sum"},
        "week_start_col": "week_start",
        "drop_original_date": True,
    }
    out = sanitize_aggregate_weekly_params(raw)
    assert out.get("week_start_col") == "week_start"


def test_sanitize_keeps_custom_output_bucket_name():
    raw = {
        "date_col": "date",
        "week_start_col": "bucket_mon",
        "drop_original_date": True,
    }
    out = sanitize_aggregate_weekly_params(raw)
    assert out.get("week_start_col") == "bucket_mon"


def test_merge_deferred_collapses_two_aggregate_weekly_steps():
    by_source = {
        "a": [
            {
                "tool": "transform.aggregate_weekly",
                "params": {
                    "date_col": "date",
                    "group_by_cols": ["channel"],
                    "metric_rules": {"spends": "sum"},
                    "week_start_col": "week_start",
                },
            }
        ],
        "b": [
            {
                "tool": "transform.aggregate_weekly",
                "params": {
                    "date_col": "date",
                    "group_by_cols": ["publisher", "channel"],
                    "metric_rules": {"impressions": "sum", "spends": "sum"},
                    "week_start_col": "week_start",
                },
            }
        ],
    }
    merged = merge_deferred_post_collate_tool_lists(by_source)
    aw = [t for t in merged if t.get("tool") == "transform.aggregate_weekly"]
    assert len(aw) == 1
    params = aw[0]["params"]
    assert "week_start_col" not in params
    assert params["group_by_cols"] == ["channel", "publisher"]
    assert params["metric_rules"] == {"impressions": "sum", "spends": "sum"}
