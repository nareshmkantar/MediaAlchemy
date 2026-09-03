"""Plan review destructive impact previews (before execution)."""
import pandas as pd

from sia.agent.plan_destructive_preview import compute_plan_destructive_impacts


def _resolve_job_source(_job, source_id=None):
    return {
        "file_path": "uploads/example.xlsx",
        "sheet_name": "Sheet1",
    }


def test_compute_plan_destructive_impacts_shows_total_row_removal(monkeypatch):
    frame = pd.DataFrame(
        {
            "campaign": ["Alpha", "Beta", "Total"],
            "spend": [100, 200, 300],
        }
    )

    def _fake_load(_path, _sheet, _scoped):
        return frame.copy(), {}

    monkeypatch.setattr(
        "sia.agent.plan_destructive_preview.load_scoped_dataframe",
        _fake_load,
    )

    tool_calls = [
        {"tool": "layout.extract", "params": {"header_row": 0}},
        {
            "tool": "transform.filter_summaries",
            "params": {"keywords": ["Total", "Subtotal"]},
        },
    ]
    job = {"file_path": "uploads/example.xlsx", "scoped_source": {"sheet_name": "Sheet1"}}
    pending_state = {"scoped_source": {"sheet_name": "Sheet1"}}

    payload = compute_plan_destructive_impacts(
        job,
        pending_state,
        tool_calls,
        resolve_job_source_fn=_resolve_job_source,
    )

    assert payload["preview_available"] is True
    assert len(payload["destructive_tools"]) == 1
    assert payload["destructive_tools"][0]["tool"] == "transform.filter_summaries"
    assert len(payload["impacts"]) == 1

    impact = payload["impacts"][0]
    assert impact["has_removals"] is True
    assert impact["rows_before"] == 3
    assert impact["rows_after"] == 2
    assert impact["visual_sample"] is not None
    assert impact["visual_sample"]["title"].startswith("Remove subtotal / total rows")
    outcomes = [row.get("Outcome") for row in impact["visual_sample"]["rows"]]
    assert outcomes == ["Would remove if you continue"]
    assert impact["visual_sample"]["rows"][0]["campaign"] == "Total"


def test_compute_plan_destructive_impacts_no_destructive_tools():
    payload = compute_plan_destructive_impacts(
        {},
        {},
        [{"tool": "transform.rename", "params": {"mapping": {"a": "b"}}}],
        resolve_job_source_fn=_resolve_job_source,
    )

    assert payload["preview_available"] is False
    assert payload["destructive_tools"] == []
    assert payload["impacts"] == []
