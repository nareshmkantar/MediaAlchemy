"""Pipeline evaluation framework (deterministic scorecard + critical gate)."""

from sia.evals.display import build_eval_display, eval_display_sort_key
from sia.evals.runner import PipelineEvalRunner
from sia.evals.plan_contract import evaluate_plan_contract
from sia.evals.structure_consistency import evaluate_structure_consistency
from sia.evals.plan_review_quality import evaluate_plan_review_quality
from sia.evals.collation_sanity import evaluate_union_false_duplicates, evaluate_cross_source_isolation
from sia.evals.quality_score import (
    compute_quality_scores,
    compute_source_quality_scores,
    compute_all_source_quality,
)

__all__ = [
    "PipelineEvalRunner",
    "build_eval_display",
    "eval_display_sort_key",
    "evaluate_plan_contract",
    "evaluate_structure_consistency",
    "evaluate_plan_review_quality",
    "evaluate_union_false_duplicates",
    "evaluate_cross_source_isolation",
    "compute_quality_scores",
    "compute_source_quality_scores",
    "compute_all_source_quality",
]
