"""
Confidence scoring and HITL decision models.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class HITLDecision(Enum):
    """Human-in-the-Loop routing decision."""
    AUTO_APPROVE = "auto_approve"      # Confidence > 0.9
    FLAG_FOR_REVIEW = "flag_for_review"  # 0.6 < Confidence <= 0.9
    STOP_AND_ASK = "stop_and_ask"      # Confidence <= 0.6


@dataclass
class ConfidenceResult:
    """
    Confidence score with rationale for a decision.
    Every module outputs this alongside its main result.
    """
    score: float  # 0.0 to 1.0
    rationale: str
    signals: List[str] = field(default_factory=list)
    module_name: str = ""
    
    @property
    def decision(self) -> HITLDecision:
        """Get HITL routing decision based on score."""
        if self.score > 0.9:
            return HITLDecision.AUTO_APPROVE
        elif self.score > 0.6:
            return HITLDecision.FLAG_FOR_REVIEW
        else:
            return HITLDecision.STOP_AND_ASK
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "score": round(self.score, 3),
            "decision": self.decision.value,
            "rationale": self.rationale,
            "signals": self.signals,
            "module": self.module_name
        }


@dataclass
class HITLReviewItem:
    """
    An item queued for human review.
    """
    item_id: str
    module_name: str
    confidence: ConfidenceResult
    proposed_output: Dict[str, Any]
    grid_preview: Optional[str] = None  # Text representation of relevant grid
    corrections_made: Optional[Dict[str, Any]] = None
    reviewed: bool = False
    
    def apply_correction(self, corrections: Dict[str, Any]) -> None:
        """Apply human corrections."""
        self.corrections_made = corrections
        self.reviewed = True
    
    def to_labeled_example(self) -> Dict[str, Any]:
        """Convert to labeled example for training store."""
        return {
            "input_context": {
                "grid_preview": self.grid_preview,
                "module": self.module_name
            },
            "proposed_output": self.proposed_output,
            "corrected_output": self.corrections_made or self.proposed_output,
            "is_correction": self.corrections_made is not None
        }


@dataclass
class ProcessingTrace:
    """
    Execution trace for observability and debugging.
    Logs each step of the SIA pipeline.
    """
    trace_id: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    overall_confidence: float = 0.0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    debug_events: List[Dict[str, Any]] = field(default_factory=list)
    debug_enabled: bool = False
    # LLM debug fields
    llm_debug_summary: Dict[str, Any] = field(default_factory=dict)
    llm_calls: List[Dict[str, Any]] = field(default_factory=list)
    # Human Review Flag
    requires_review: bool = False
    review_reason: str = ""
    # NEW: HITL Pause for Destructive Operations
    hitl_pending: bool = False
    hitl_pause_type: Optional[str] = None
    deletion_previews: List[Dict[str, Any]] = field(default_factory=list)
    pending_state: Dict[str, Any] = field(default_factory=dict)
    hitl_checkpoints: List[Dict[str, Any]] = field(default_factory=list)
    low_confidence_items: List[Dict[str, Any]] = field(default_factory=list) # NEW
    verifier_issues: List[Dict[str, Any]] = field(default_factory=list) # NEW
    approval_items: List[Dict[str, Any]] = field(default_factory=list)
    planned_rule_actions: List[Dict[str, Any]] = field(default_factory=list)
    output_file: Optional[str] = None
    # NEW: Judge Evaluation
    judge_result: Optional[Dict[str, Any]] = None
    # NEW: Lifecycle timeline (derived) aligning Debug with planner/review/resume/finalize
    lifecycle_events: List[Dict[str, Any]] = field(default_factory=list)
    # Tools deferred from per-source execution until after union/collation (e.g. weekly rollup).
    deferred_post_collate_tools: List[Dict[str, Any]] = field(default_factory=list)

    
    def add_lifecycle_event(
        self,
        phase: str,
        status: str = "info",
        message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a high-level lifecycle event for the Debug timeline.

        phases: ``setup`` | ``load`` | ``analyze`` | ``plan`` | ``plan_review`` |
                ``resume`` | ``execute`` | ``execute_pause`` | ``verify`` |
                ``replan`` | ``finalize`` | ``output_ready`` | ``error``.
        status: ``info`` | ``ok`` | ``pending`` | ``warning`` | ``error``.
        """
        from datetime import datetime, timezone
        self.lifecycle_events.append({
            "phase": phase,
            "status": status,
            "message": message or "",
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def add_step(self, module_name: str, input_summary: str,
                 output_summary: str, confidence) -> None:
        """Add a step to the trace. Confidence can be ConfidenceResult or dict."""
        # Handle both ConfidenceResult objects and plain dicts
        if hasattr(confidence, 'to_dict'):
            conf_dict = confidence.to_dict()
        elif isinstance(confidence, dict):
            # Already a dict, ensure it has required fields
            conf_dict = {
                "score": confidence.get("score", confidence.get("confidence", 0.0)),
                "decision": confidence.get("decision", "unknown"),
                "rationale": confidence.get("rationale", ""),
                "signals": confidence.get("signals", []),
                "module": module_name
            }
        else:
            # Fallback for numbers or other types
            conf_dict = {"score": float(confidence) if confidence else 0.0, "decision": "unknown"}
        
        self.steps.append({
            "module": module_name,
            "input": input_summary,
            "output": output_summary,
            "confidence": conf_dict
        })
    
    def add_error(self, error: str) -> None:
        """Log an error."""
        self.errors.append(error)
    
    def add_warning(self, warning: str) -> None:
        """Log a warning."""
        self.warnings.append(warning)
    
    def attach_debug_session(self, debug_session) -> None:
        """Attach debug events from a DebugSession."""
        if debug_session and debug_session.enabled:
            # Match LLMObserver method name
            if hasattr(debug_session, 'get_hierarchical_events'):
                self.debug_events = debug_session.get_hierarchical_events()
            elif hasattr(debug_session, 'get_events'):
                self.debug_events = debug_session.get_events()
            self.debug_enabled = True
    
    def calculate_overall_confidence(self) -> float:
        """Minimum confidence across pipeline steps (excludes verify/replan).

        Verification and replan steps often carry low scores when iteration is still
        refining the table; including them collapses overall to 0% even though
        structure and extraction steps succeeded.
        """
        if not self.steps:
            return 0.0
        skip_modules = frozenset({"verify_output", "replan"})
        confidences: List[float] = []
        for s in self.steps:
            if not isinstance(s, dict):
                continue
            mod = str(s.get("module", "")).strip()
            if mod in skip_modules:
                continue
            cd = s.get("confidence")
            if isinstance(cd, dict) and cd.get("score") is not None:
                try:
                    confidences.append(float(cd["score"]))
                except (TypeError, ValueError):
                    continue
        if not confidences:
            for s in self.steps:
                if not isinstance(s, dict):
                    continue
                cd = s.get("confidence")
                if isinstance(cd, dict) and cd.get("score") is not None:
                    try:
                        confidences.append(float(cd["score"]))
                    except (TypeError, ValueError):
                        continue
        if not confidences:
            return 0.0
        self.overall_confidence = min(confidences)
        return self.overall_confidence
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/UI display."""
        result = {
            "trace_id": self.trace_id,
            "steps": self.steps,
            "overall_confidence": self.overall_confidence,
            "errors": self.errors,
            "warnings": self.warnings,
            "requires_review": self.requires_review,
            "review_reason": self.review_reason,
            "hitl_pending": self.hitl_pending,
            "hitl_pause_type": self.hitl_pause_type,
            "hitl_checkpoints": self.hitl_checkpoints,
            "low_confidence_items": self.low_confidence_items, # NEW
            "verifier_issues": self.verifier_issues, # NEW
            "approval_items": self.approval_items,
            "planned_rule_actions": self.planned_rule_actions,
            "output_file": self.output_file,
            "judge_result": self.judge_result,
            "lifecycle_events": self.lifecycle_events,
            "deferred_post_collate_tools": self.deferred_post_collate_tools,
        }
        if self.debug_enabled and self.debug_events:
            result["debug_events"] = self.debug_events
        if self.llm_debug_summary:
            result["llm_debug_summary"] = self.llm_debug_summary
        if self.llm_calls:
            result["llm_calls"] = self.llm_calls
        return result

