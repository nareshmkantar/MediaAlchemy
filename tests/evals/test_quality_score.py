"""Tests for composite quality score."""

from __future__ import annotations

from sia.evals.display import build_eval_display
from sia.evals.quality_score import (
    compute_quality_scores,
    compute_source_quality_scores,
    compute_all_source_quality,
    _task_texts,
)


def _job_with_plan(contract_score: float = 1.0):
    return {
        "pipeline_evals": {
            "critical_gate": {"pass": True, "blocked_reasons": []},
            "per_source": {
                "src1": {
                    "plan": {
                        "pass": True,
                        "metrics": {
                            "final_tool_contract_score": contract_score,
                            "tool_contract_score": contract_score,
                        },
                        "violations": [],
                    },
                    "context_isolation": {"pass": True, "violations": [], "metrics": {}},
                }
            },
            "collation": {},
            "judge": {},
        },
        "source_registry": [{"source_id": "src1", "sheet_name": "TV_FR"}],
    }


def test_quality_score_passing_job_not_flat_100():
    job = _job_with_plan(1.0)
    job["judge_result"] = {
        "fidelity": 0.82,
        "integrity": 0.88,
        "tool_accuracy": 0.75,
        "trajectory_success": 0.7,
        "task_success": True,
        "verdict": "PASS",
    }
    q = compute_quality_scores(job, judge_result=job["judge_result"], use_deepeval=False)
    assert q["overall"] is not None
    assert 50 <= q["overall"] <= 100
    assert q["subscores"]["plan_quality"]["score"] >= 0.7
    assert isinstance(q.get("strengths"), list)


def test_quality_score_capped_when_gate_blocked():
    job = _job_with_plan(1.0)
    job["pipeline_evals"]["critical_gate"] = {"pass": False, "blocked_reasons": ["dimension_mismatch:src1"]}
    q = compute_quality_scores(job, use_deepeval=False)
    assert q["gate_blocked"] is True
    assert q["overall"] <= 40


def test_build_eval_display_includes_quality():
    job = _job_with_plan(0.95)
    job["judge_result"] = {"fidelity": 0.9, "task_success": True, "verdict": "PASS"}
    display = build_eval_display(job)
    assert "quality" in display
    assert display["quality"].get("overall") is not None


def test_deepeval_available_without_package():
    from sia.evals.deepeval_adapter import deepeval_available

    # Should not raise; may be True or False depending on install
    assert isinstance(deepeval_available(), bool)


def _multi_source_job():
    return {
        "pipeline_evals": {
            "critical_gate": {"pass": True, "blocked_reasons": []},
            "per_source": {
                "good": {
                    "plan": {"pass": True, "metrics": {"final_tool_contract_score": 0.95, "necessity_score": 0.9}, "violations": []},
                    "context_isolation": {"pass": True, "violations": [], "metrics": {}},
                    "execution": {"pass": True, "metrics": {"verifier_issue_count": 0}},
                },
                "weak": {
                    "plan": {"pass": True, "metrics": {"final_tool_contract_score": 0.5}, "violations": []},
                    "context_isolation": {
                        "pass": False,
                        "violations": [{"type": "dimension_mismatch"}],
                        "metrics": {},
                    },
                    "execution": {"pass": True, "metrics": {"verifier_issue_count": 3}},
                },
            },
            "collation": {},
            "judge": {},
        },
        "source_registry": [
            {"source_id": "good", "sheet_name": "TV_FR"},
            {"source_id": "weak", "sheet_name": "Radio_DE"},
        ],
    }


def test_per_source_quality_differs_by_source():
    job = _multi_source_job()
    per = compute_all_source_quality(job)
    assert set(per.keys()) == {"good", "weak"}
    assert per["good"]["overall"] > per["weak"]["overall"]
    # Weak source has isolation failure -> capped
    assert per["weak"]["overall"] <= 55


def test_per_source_quality_only_source_level_subscores():
    job = _multi_source_job()
    q = compute_source_quality_scores(job["pipeline_evals"]["per_source"]["good"])
    assert set(q["subscores"].keys()) == {"plan_quality", "context_quality", "step_efficiency"}
    # Task completion is job-level only; must NOT appear per source
    assert "task_completion" not in q["subscores"]


def test_build_eval_display_includes_per_source_quality():
    job = _multi_source_job()
    display = build_eval_display(job)
    psq = display.get("per_source_quality") or {}
    assert "good" in psq and "weak" in psq
    assert psq["good"].get("overall") is not None


def test_job_level_quality_marks_job_only_metrics():
    job = _job_with_plan(0.95)
    q = compute_quality_scores(job, use_deepeval=False)
    assert "deepeval_task_completion" in q.get("job_level_only", [])
    assert "collation" in q.get("job_level_only", [])


def test_task_texts_includes_schema_columns_preview_and_per_source():
    job = {
        "target_template": {"properties": {"date": {}, "spends": {}, "market": {}}},
        "schema": {
            "total_rows": 42,
            "fields": [
                {"name": "date", "type": "date", "role": "dimension"},
                {"name": "spends", "type": "number", "role": "metric"},
                {"name": "market", "type": "string", "role": "dimension"},
            ],
        },
        "data_preview": [
            {"date": "2024-01-01", "spends": 100, "market": "UK"},
            {"date": "2024-01-08", "spends": 120, "market": "UK"},
        ],
        "data_preview_column_order": ["date", "spends", "market"],
        "source_registry": [
            {"source_id": "src1", "sheet_name": "Digital_UK"},
            {"source_id": "src2", "sheet_name": "Print_US"},
        ],
        "pipeline_evals": {
            "per_source": {
                "src1": {
                    "context_isolation": {
                        "pass": True,
                        "metrics": {"local_context": {"market": "UK", "channel": "Digital"}},
                    }
                },
                "src2": {
                    "context_isolation": {
                        "pass": True,
                        "metrics": {"local_context": {"market": "US", "channel": "Print"}},
                    }
                },
            },
            "collation": {"pass": True, "violations": []},
        },
        "judge_result": {
            "verdict": "PASS",
            "critique": "Mappings look correct and no bleed detected.",
            "metrics": {"total_rows": 42, "total_cols": 3},
        },
    }
    task, output = _task_texts(job, job["judge_result"], job["schema"])
    assert "date" in output and "spends" in output and "market" in output
    assert "row 1:" in output
    assert "Digital_UK" in output and "Print_US" in output
    assert "market=UK" in output
    assert "Combined export preview" in output
    assert "Judge note:" in output
    assert task.startswith("Transform a messy")


def test_task_texts_falls_back_to_tool_preview_when_no_data_preview():
    job = {
        "tool_executions": [
            {
                "tool": "verify.schema",
                "output_preview": {
                    "shape": "(3, 2)",
                    "column_order": ["date", "spends"],
                    "sample": [{"date": "2024-01-01", "spends": 50}],
                },
            }
        ]
    }
    _, output = _task_texts(job, None, None)
    assert "Tool output preview" in output
    assert "row 1:" in output
    assert "spends" in output


def test_step_efficiency_not_penalized_by_multisource_repetition():
    """Regression: per-sheet tool repetition must not tank job-level efficiency.

    Four clean sources (verify.schema once per sheet, no verifier issues) should
    roll up to a high step-efficiency score, not the old ~0.5 whole-trace value.
    """
    job = {
        "pipeline_evals": {
            "critical_gate": {"pass": True, "blocked_reasons": []},
            "per_source": {
                sid: {
                    "plan": {"pass": True, "metrics": {"final_tool_contract_score": 0.95, "necessity_score": 1.0}, "violations": []},
                    "context_isolation": {"pass": True, "violations": [], "metrics": {}},
                    "execution": {"pass": True, "metrics": {"verifier_issue_count": 0}},
                }
                for sid in ("Digital_UK", "Print_US", "TV_FR", "Radio_DE")
            },
            "collation": {},
            "judge": {},
        },
        "source_registry": [{"source_id": s} for s in ("Digital_UK", "Print_US", "TV_FR", "Radio_DE")],
    }
    # Whole-trace tool list that would trip the old heuristic (verify.schema x4 + consecutive repeats).
    tools = []
    for _ in range(4):
        tools += [{"tool": "extract.table"}, {"tool": "verify.schema"}]
    q = compute_quality_scores(job, tool_executions=tools, use_deepeval=False)
    eff = q["subscores"]["step_efficiency"]
    assert eff["engine"] == "per_source_avg"
    assert eff["score"] >= 0.9
