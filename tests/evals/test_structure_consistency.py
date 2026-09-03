"""Structure eval: sparse-dimension false positives and layout_scan_score."""

from __future__ import annotations

from sia.evals.display import build_eval_display
from sia.evals.runner import PipelineEvalRunner
from sia.evals.structure_consistency import evaluate_structure_consistency


def test_sparse_dimension_false_positive_when_llm_contradicts_scan():
    signals = {
        "grouped_rows_likely": True,
        "sparse_dimension_columns": [
            {"column_label": "Publisher", "blank_ratio": 0.96, "role_hint": "dimension_or_label"},
        ],
        "block_sparse_metrics": [],
    }
    sa = {
        "confidence": 0.92,
        "column_analysis": [
            {
                "column_label": "Publisher",
                "role": "dimension",
                "is_blank": False,
                "notes": [
                    "Metric layout signals reported sparse_dimension_columns anomaly, "
                    "but sampled rows show populated publisher values throughout."
                ],
            }
        ],
    }
    out = evaluate_structure_consistency(sa, metric_layout_signals=signals)
    assert out["metrics"]["sparse_dimension_false_positive_count"] == 1
    assert out["metrics"]["sparse_dimension_precision"] == 0.0
    assert out["metrics"]["layout_scan_score"] < 0.85
    types = {v["type"] for v in out["violations"]}
    assert "sparse_dimension_false_positive" in types
    assert "grouped_layout_false_positive" in types


def test_no_false_positive_when_text_fill_rate_high_in_signals():
    signals = {
        "grouped_rows_likely": False,
        "sparse_dimension_columns": [
            {"column_label": "Publisher", "blank_ratio": 0.05, "text_fill_rate": 0.95},
        ],
        "block_sparse_metrics": [],
    }
    out = evaluate_structure_consistency({"confidence": 0.9}, metric_layout_signals=signals)
    assert out["metrics"]["sparse_dimension_false_positive_count"] == 1
    assert out["metrics"]["sparse_dimension_precision"] == 0.0


def test_layout_scan_score_perfect_when_no_sparse_flags():
    out = evaluate_structure_consistency(
        {"confidence": 0.95, "tables": [{"label": "main"}]},
        metric_layout_signals={"grouped_rows_likely": False, "sparse_dimension_columns": []},
    )
    assert out["metrics"]["sparse_dimension_precision"] == 1.0
    assert out["metrics"]["layout_scan_score"] >= 0.9
    assert not out["violations"]


def test_eval_display_includes_structure_metrics_in_rollup():
    job = {
        "filename": "CP-07.xlsx",
        "pipeline_evals": {
            "critical_gate": {"pass": True},
            "per_source": {
                "src:Digital_UK": {
                    "structure": {
                        "pass": True,
                        "metrics": {
                            "layout_scan_score": 0.62,
                            "sparse_dimension_precision": 0.0,
                            "sparse_dimension_false_positive_count": 1,
                        },
                        "violations": [
                            {
                                "type": "sparse_dimension_false_positive",
                                "severity": "advisory",
                                "message": "Publisher false positive",
                            }
                        ],
                    }
                }
            },
        },
    }
    d = build_eval_display(job)
    struct = next(r for r in d["stage_rollups"] if r["stage"] == "structure")
    assert struct["metrics"]["layout_scan_score"] == 0.62
    assert struct["advisory"] >= 1
    assert any(i["type"] == "sparse_dimension_false_positive" for i in d["issues"])


def test_runner_records_structure_fp():
    job = {"job_id": "struct-fp"}
    PipelineEvalRunner.record_structure(
        job,
        "src-a",
        structure_analysis={
            "confidence": 0.9,
            "column_analysis": [
                {
                    "column_label": "Publisher",
                    "is_blank": False,
                    "notes": ["populated publisher values throughout"],
                }
            ],
        },
        metric_layout_signals={
            "sparse_dimension_columns": [{"column_label": "Publisher", "blank_ratio": 0.9}],
            "grouped_rows_likely": True,
        },
    )
    bucket = job["pipeline_evals"]["per_source"]["src-a"]["structure"]
    assert bucket["metrics"]["sparse_dimension_false_positive_count"] == 1
