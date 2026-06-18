"""
HITL (Human-in-the-Loop) Checkpoint Management.

This module provides strategic checkpoint creation and resume functionality
for human intervention in the agentic workflow.
"""
import logging
import json
from datetime import datetime
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field, asdict
from enum import Enum
import pandas as pd

logger = logging.getLogger(__name__)


class CheckpointType(Enum):
    """Types of HITL checkpoints."""
    PLAN_REVIEW = "plan_review"              # Review extraction plan before execution
    DESTRUCTIVE_TOOL = "destructive_tool"    # Confirm destructive operation
    VERIFICATION_STALL = "verification_stall" # Same issue found multiple times
    LOW_CONFIDENCE = "low_confidence"         # Overall confidence below threshold
    SCHEMA_MISMATCH = "schema_mismatch"       # Actual output doesn't match expected
    PARTIAL_FAILURE = "partial_failure"       # Some tools failed, partial success
    MANUAL_TRIGGER = "manual_trigger"         # User explicitly requested pause
    STRUCTURAL_REVIEW = "structural_review"  # inspect_sheet_structure found noise/ambiguity
    CHECKSUM_FAILURE = "checksum_failure"     # verify_checksum detected integrity issues
    COLUMN_DECISION = "column_decision"       # Human needs to decide Keep/Discard for columns
    FILE_RELATIONSHIP_REVIEW = "file_relationship_review"  # Review AI-proposed multi-file relationships


@dataclass
class HITLCheckpoint:
    """Represents a point where human intervention is needed."""
    checkpoint_id: str
    checkpoint_type: CheckpointType
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # Context for the human reviewer
    title: str = ""
    description: str = ""
    severity: str = "medium"  # low, medium, high, critical
    
    # What triggered this checkpoint
    trigger_reason: str = ""
    trigger_data: Dict[str, Any] = field(default_factory=dict)
    
    # Options for the human
    available_actions: List[str] = field(default_factory=list)
    recommended_action: str = ""
    
    # State snapshot
    current_step: str = ""
    iteration: int = 0
    confidence: float = 0.0
    
    # Data preview (for display in UI)
    data_preview: Dict[str, Any] = field(default_factory=dict)
    schema_preview: Dict[str, Any] = field(default_factory=dict)
    
    # Resolution
    resolved: bool = False
    resolved_at: Optional[str] = None
    resolution_action: Optional[str] = None
    resolution_data: Optional[Dict] = None
    resolved_by: Optional[str] = None
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d['checkpoint_type'] = self.checkpoint_type.value
        return d
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'HITLCheckpoint':
        """Create from dictionary."""
        data['checkpoint_type'] = CheckpointType(data['checkpoint_type'])
        return cls(**data)


@dataclass
class HITLDecisionConfig:
    """Configuration for HITL decision thresholds."""
    # Confidence thresholds
    auto_approve_threshold: float = 0.9
    flag_for_review_threshold: float = 0.7
    stop_and_ask_threshold: float = 0.5
    
    # Destructive tool thresholds
    row_drop_warning_percent: float = 0.2   # Warn if dropping >20% of rows
    column_drop_warning_count: int = 3       # Warn if dropping >3 columns
    
    # Verification stall thresholds  
    max_same_issue_count: int = 2            # Escalate after same issue 2x
    max_iterations_before_review: int = 3
    
    # Structural review thresholds
    noise_score_threshold: float = 0.5       # Flag for review if noise score > 0.5
    min_header_candidates: int = 2           # Flag if multiple ambiguous header rows
    
    # Checksum tolerance
    checksum_fail_threshold: int = 1         # Flag if >= N checksum columns fail
    
    # Enable/disable checkpoint types
    enable_plan_review: bool = True
    enable_destructive_warnings: bool = True
    enable_verification_stall: bool = True
    enable_low_confidence: bool = True
    enable_schema_mismatch: bool = False
    enable_structural_review: bool = True
    enable_checksum_review: bool = True


class HITLManager:
    """
    Manages HITL checkpoints throughout the agent workflow.
    
    Usage:
        manager = HITLManager()
        
        # Check if checkpoint needed
        checkpoint = manager.should_checkpoint(state, "execute_tools")
        if checkpoint:
            # Pause and wait for human resolution
            resolved = manager.await_resolution(checkpoint)
            
        # Or create checkpoint proactively
        checkpoint = manager.create_checkpoint(
            CheckpointType.LOW_CONFIDENCE,
            state,
            "Confidence score is 0.45"
        )
    """
    
    def __init__(self, config: HITLDecisionConfig = None):
        self.config = config or HITLDecisionConfig()
        self.checkpoints: List[HITLCheckpoint] = []
        self.checkpoint_counter = 0
    
    def should_checkpoint(
        self,
        state: Dict[str, Any],
        current_step: str
    ) -> Optional[HITLCheckpoint]:
        """
        Determine if a checkpoint is needed at the current state.
        
        Args:
            state: Current agent state
            current_step: Name of the current step
            
        Returns:
            HITLCheckpoint if one is needed, None otherwise
        """
        # Check plan review
        if current_step == "generate_plan" and self.config.enable_plan_review:
            plan = state.get("extraction_plan")
            if plan and hasattr(plan, 'confidence') and plan.confidence < self.config.flag_for_review_threshold:
                return self._create_plan_review_checkpoint(state, plan)
        
        # Check for destructive tools
        if current_step == "execute_tools" and self.config.enable_destructive_warnings:
            suggested_tools = state.get("suggested_tools", [])
            destructive = self._find_destructive_tools(suggested_tools)
            if destructive:
                return self._create_destructive_tool_checkpoint(state, destructive)
        
        # Check verification stall
        if current_step == "verify_output" and self.config.enable_verification_stall:
            if self._is_verification_stalled(state):
                return self._create_verification_stall_checkpoint(state)
        
        # Check structural review after analysis
        if current_step == "analyze_structure" and self.config.enable_structural_review:
            scoped_source = state.get("scoped_source") or {}
            if scoped_source and scoped_source.get("header_row") is not None:
                return None
            structure_report = state.get("structure_report", {})
            noise_score = structure_report.get("noise_score", 0.0)
            header_candidates = structure_report.get("header_candidates", [])
            if noise_score > self.config.noise_score_threshold:
                return self._create_structural_review_checkpoint(state, structure_report)
            if len(header_candidates) >= self.config.min_header_candidates:
                return self._create_structural_review_checkpoint(state, structure_report)
        
        # Check checksum failures after tool execution
        if current_step == "execute_tools" and self.config.enable_checksum_review:
            checksum_result = state.get("checksum_result", {})
            failures = checksum_result.get("failures", [])
            if len(failures) >= self.config.checksum_fail_threshold:
                return self._create_checksum_failure_checkpoint(state, checksum_result)
        
        # Check low confidence after finalization
        if current_step == "finalize" and self.config.enable_low_confidence:
            confidence = self._calculate_overall_confidence(state)
            if confidence < self.config.stop_and_ask_threshold:
                return self._create_low_confidence_checkpoint(state, confidence)
        
        return None
    
    def create_checkpoint(
        self,
        checkpoint_type: CheckpointType,
        state: Dict[str, Any],
        reason: str,
        **kwargs
    ) -> HITLCheckpoint:
        """Create a checkpoint manually."""
        self.checkpoint_counter += 1
        checkpoint_id = f"hitl_{self.checkpoint_counter:04d}"
        
        checkpoint = HITLCheckpoint(
            checkpoint_id=checkpoint_id,
            checkpoint_type=checkpoint_type,
            trigger_reason=reason,
            current_step=state.get("current_step", "unknown"),
            iteration=state.get("iteration", 0),
            confidence=self._calculate_overall_confidence(state),
            **kwargs
        )
        
        self.checkpoints.append(checkpoint)
        logger.info(f"Created HITL checkpoint: {checkpoint_id} ({checkpoint_type.value})")
        
        return checkpoint
    
    def resolve_checkpoint(
        self,
        checkpoint_id: str,
        action: str,
        resolution_data: Dict = None,
        resolved_by: str = "user"
    ) -> bool:
        """
        Resolve a checkpoint with human decision.
        
        Args:
            checkpoint_id: ID of checkpoint to resolve
            action: Action taken (e.g., "approve", "reject", "modify")
            resolution_data: Additional data from resolution (e.g., modified plan)
            resolved_by: Who resolved it
            
        Returns:
            True if resolution successful
        """
        for cp in self.checkpoints:
            if cp.checkpoint_id == checkpoint_id:
                cp.resolved = True
                cp.resolved_at = datetime.now().isoformat()
                cp.resolution_action = action
                cp.resolution_data = resolution_data
                cp.resolved_by = resolved_by
                logger.info(f"Resolved checkpoint {checkpoint_id}: {action}")
                return True
        
        logger.warning(f"Checkpoint not found: {checkpoint_id}")
        return False
    
    def get_pending_checkpoints(self) -> List[HITLCheckpoint]:
        """Get all unresolved checkpoints."""
        return [cp for cp in self.checkpoints if not cp.resolved]
    
    def get_checkpoint(self, checkpoint_id: str) -> Optional[HITLCheckpoint]:
        """Get a specific checkpoint by ID."""
        for cp in self.checkpoints:
            if cp.checkpoint_id == checkpoint_id:
                return cp
        return None
    
    # ===== Private Helper Methods =====
    
    def _create_plan_review_checkpoint(
        self,
        state: Dict,
        plan: Any
    ) -> HITLCheckpoint:
        """Create checkpoint for plan review."""
        return self.create_checkpoint(
            checkpoint_type=CheckpointType.PLAN_REVIEW,
            state=state,
            reason=f"Plan confidence ({plan.confidence:.1%}) is below threshold ({self.config.flag_for_review_threshold:.1%})",
            title="Extraction Plan Review Required",
            description=f"The AI generated an extraction plan with confidence {plan.confidence:.1%}. Please review before proceeding.",
            severity="medium" if plan.confidence > 0.5 else "high",
            available_actions=["approve", "modify", "reject", "regenerate"],
            recommended_action="approve" if plan.confidence > 0.6 else "modify",
            trigger_data={
                "plan_confidence": plan.confidence,
                "tool_count": len(plan.tool_calls) if hasattr(plan, 'tool_calls') else 0,
                "reasoning": plan.reasoning if hasattr(plan, 'reasoning') else ""
            }
        )
    
    def _find_destructive_tools(self, tools: List[Dict]) -> List[str]:
        """Find destructive tools in the tool list."""
        from .tool_validator import is_destructive_tool
        destructive = []
        for t in tools:
            tool_name = t.get("tool", "")
            if is_destructive_tool(tool_name):
                destructive.append(tool_name)
        return list(set(destructive))
    
    def _create_destructive_tool_checkpoint(
        self,
        state: Dict,
        destructive_tools: List[str]
    ) -> HITLCheckpoint:
        """Create checkpoint for destructive tool warning."""
        return self.create_checkpoint(
            checkpoint_type=CheckpointType.DESTRUCTIVE_TOOL,
            state=state,
            reason=f"Plan includes destructive tools: {destructive_tools}",
            title="Destructive Operations Warning",
            description=f"The following tools will modify data structure: {', '.join(destructive_tools)}. These operations may remove rows or change column layout.",
            severity="medium",
            available_actions=["proceed", "skip_destructive", "cancel"],
            recommended_action="proceed",
            trigger_data={
                "destructive_tools": destructive_tools
            }
        )
    
    def _is_verification_stalled(self, state: Dict) -> bool:
        """Check if verification is repeating the same issues."""
        issues_history = state.get("issues_history", [])
        if len(issues_history) < 2:
            return False
        
        # Check if last N issues are the same
        recent_issues = issues_history[-self.config.max_same_issue_count:]
        issue_types = [i.get("issue_type", "") for i in recent_issues]
        
        return len(set(issue_types)) == 1 and len(issue_types) >= self.config.max_same_issue_count
    
    def _create_verification_stall_checkpoint(self, state: Dict) -> HITLCheckpoint:
        """Create checkpoint for verification stall."""
        issues_history = state.get("issues_history", [])
        repeated_issue = issues_history[-1] if issues_history else {}
        
        return self.create_checkpoint(
            checkpoint_type=CheckpointType.VERIFICATION_STALL,
            state=state,
            reason=f"Same issue found {self.config.max_same_issue_count} times: {repeated_issue.get('issue_type', 'unknown')}",
            title="Verification Stalled",
            description=f"The verification step has found the same issue multiple times. The automated fix may not be working correctly.",
            severity="high",
            available_actions=["fix_manually", "skip_verification", "retry_with_different_tools", "accept_as_is"],
            recommended_action="fix_manually",
            trigger_data={
                "repeated_issue": repeated_issue,
                "iteration": state.get("iteration", 0)
            }
        )
    
    def _create_low_confidence_checkpoint(
        self,
        state: Dict,
        confidence: float
    ) -> HITLCheckpoint:
        """Create checkpoint for low overall confidence."""
        # Pull specific issues from verifier to make it actionable
        verifier_issues = state.get("verifier_issues", [])
        issue_summary = ""
        if verifier_issues:
            descriptions = [i.get("description", "") for i in verifier_issues[:3]]
            issue_summary = " - Issues: " + "; ".join(descriptions)
            if len(verifier_issues) > 3:
                issue_summary += " ..."

        return self.create_checkpoint(
            checkpoint_type=CheckpointType.LOW_CONFIDENCE,
            state=state,
            reason=f"Overall confidence ({confidence:.1%}) is critically low{issue_summary}",
            title="Low Confidence - Review Required",
            description=f"The extraction completed with confidence {confidence:.1%}.{issue_summary} Results may contain significant errors.",
            severity="critical" if confidence < 0.3 else "high",
            available_actions=["accept", "reject", "retry", "manual_correction"],
            recommended_action="manual_correction",
            trigger_data={
                "confidence": confidence,
                "confidence_trajectory": state.get("confidence_trajectory", []),
                "verifier_issues": verifier_issues
            }
        )
    
    def _create_structural_review_checkpoint(
        self,
        state: Dict,
        structure_report: Dict
    ) -> HITLCheckpoint:
        """Create checkpoint for structural findings that need analyst review."""
        noise_score = structure_report.get("noise_score", 0.0)
        merged_count = len(structure_report.get("merged_cells", []))
        header_candidates = structure_report.get("header_candidates", [])
        spacer_rows = structure_report.get("empty_rows", [])
        spacer_cols = structure_report.get("empty_cols", [])
        hidden = structure_report.get("hidden_elements", {})
        
        # Build human-readable summary of findings
        findings = []
        if merged_count > 0:
            findings.append(f"{merged_count} merged cell regions")
        if spacer_rows:
            findings.append(f"{len(spacer_rows)} empty spacer rows")
        if spacer_cols:
            findings.append(f"{len(spacer_cols)} empty spacer columns")
        if hidden.get("hidden_rows") or hidden.get("hidden_cols"):
            h_rows = len(hidden.get("hidden_rows", []))
            h_cols = len(hidden.get("hidden_cols", []))
            findings.append(f"{h_rows} hidden rows, {h_cols} hidden cols")
        if len(header_candidates) > 1:
            findings.append(f"{len(header_candidates)} possible header rows")
        
        findings_str = "; ".join(findings) if findings else "Structural noise detected"
        severity = "high" if noise_score > 0.7 else "medium"
        
        return self.create_checkpoint(
            checkpoint_type=CheckpointType.STRUCTURAL_REVIEW,
            state=state,
            reason=f"Sheet structure noise score {noise_score:.0%}: {findings_str}",
            title="📐 Structural Review Required",
            description=(
                f"The sheet has a noise score of {noise_score:.0%}. "
                f"Findings: {findings_str}. "
                f"Please review before the agent makes assumptions about headers and data regions."
            ),
            severity=severity,
            available_actions=["approve", "select_header_row"],
            recommended_action="approve" if noise_score < 0.6 else "select_header_row",
            trigger_data={
                "sheet_name": state.get("sheet_name"),
                "source_id": (state.get("source_metadata") or {}).get("source_id"),
                "noise_score": noise_score,
                "merged_cells": structure_report.get("merged_cells", []),
                "header_candidates": header_candidates,
                "empty_rows": spacer_rows,
                "empty_cols": spacer_cols,
                "hidden_elements": hidden,
                "data_region": structure_report.get("data_region", {}),
                "density": structure_report.get("density", 0.0),
                "scoped_source": state.get("scoped_source") or {},
            }
        )
    
    def _create_checksum_failure_checkpoint(
        self,
        state: Dict,
        checksum_result: Dict
    ) -> HITLCheckpoint:
        """Create checkpoint when checksum validation detects data integrity issues."""
        failures = checksum_result.get("failures", [])
        total_checked = checksum_result.get("total_checked", 0)
        passed = checksum_result.get("passed", 0)
        
        # Build failure description
        failure_details = []
        for f in failures[:5]:  # Limit to 5 for readability
            col = f.get("column", "unknown")
            expected = f.get("expected", "?")
            actual = f.get("actual", "?")
            diff = f.get("difference", "?")
            failure_details.append(f"{col}: expected {expected}, got {actual} (diff: {diff})")
        
        failure_str = "; ".join(failure_details)
        severity = "critical" if len(failures) > 2 else "high"
        
        return self.create_checkpoint(
            checkpoint_type=CheckpointType.CHECKSUM_FAILURE,
            state=state,
            reason=f"Checksum failed for {len(failures)}/{total_checked} columns: {failure_str}",
            title="📊 Data Integrity Issue Detected",
            description=(
                f"Checksum validation found {len(failures)} column(s) with mismatched totals "
                f"out of {total_checked} checked. {passed} column(s) passed. "
                f"This may indicate data loss during transformation."
            ),
            severity=severity,
            available_actions=["accept_with_note", "investigate", "rollback", "ignore"],
            recommended_action="investigate",
            trigger_data={
                "failures": failures,
                "total_checked": total_checked,
                "passed": passed,
                "tolerance": checksum_result.get("tolerance", 0.0)
            }
        )
    
    def _create_column_decision_checkpoint(
        self,
        state: Dict,
        ambiguous_columns: List[Dict[str, Any]]
    ) -> HITLCheckpoint:
        """
        Create a checkpoint for column-level Keep/Discard decisions.
        
        Args:
            state: Current agent state
            ambiguous_columns: List of column metadata: 
                              [{"name": "CPC", "class": "derived", "confidence": 0.4, "reason": "Looks like noise"}]
        """
        column_names = [c.get("name") for c in ambiguous_columns]
        
        return self.create_checkpoint(
            checkpoint_type=CheckpointType.COLUMN_DECISION,
            state=state,
            reason=f"Ambiguous columns detected: {', '.join(column_names[:3])}",
            title="🎯 Column Decision Required",
            description=(
                f"The agent found {len(ambiguous_columns)} columns that might be noise or derived metrics. "
                "Please decide which columns to keep in the final table."
            ),
            severity="medium",
            available_actions=["resolve", "keep_all", "discard_all"],
            recommended_action="resolve",
            trigger_data={
                "columns": ambiguous_columns
            }
        )
    
    def _calculate_overall_confidence(self, state: Dict) -> float:
        """Calculate overall confidence from state."""
        trajectory = state.get("confidence_trajectory", [])
        if trajectory:
            return min(trajectory)
        
        trace_steps = state.get("trace_steps", [])
        if trace_steps:
            confidences = [s.get("confidence", 1.0) for s in trace_steps]
            return min(confidences) if confidences else 1.0
        
        return 0.5  # Default when no data
    
    def export_checkpoints(self) -> List[Dict]:
        """Export all checkpoints for persistence."""
        return [cp.to_dict() for cp in self.checkpoints]
    
    def import_checkpoints(self, data: List[Dict]) -> None:
        """Import checkpoints from persistence."""
        self.checkpoints = [HITLCheckpoint.from_dict(d) for d in data]
        if self.checkpoints:
            # Update counter to avoid ID conflicts
            max_id = max(int(cp.checkpoint_id.split("_")[1]) for cp in self.checkpoints)
            self.checkpoint_counter = max_id


# ===== Utility Functions =====

def create_data_preview(df: pd.DataFrame, max_rows: int = 5) -> Dict:
    """Create a preview dict for display in HITL UI."""
    if df is None or df.empty:
        return {"rows": 0, "columns": 0, "sample": []}
    
    return {
        "rows": len(df),
        "columns": len(df.columns),
        "column_names": list(df.columns),
        "sample": df.head(max_rows).to_dict(orient="records")
    }


def format_checkpoint_for_ui(checkpoint: HITLCheckpoint) -> Dict:
    """Format checkpoint for web UI display."""
    severity_emoji = {
        "low": "ℹ️",
        "medium": "⚠️",
        "high": "🔶",
        "critical": "🔴"
    }
    
    return {
        "id": checkpoint.checkpoint_id,
        "type": checkpoint.checkpoint_type.value,
        "title": checkpoint.title,
        "description": checkpoint.description,
        "severity": checkpoint.severity,
        "severity_icon": severity_emoji.get(checkpoint.severity, "❓"),
        "reason": checkpoint.trigger_reason,
        "actions": checkpoint.available_actions,
        "recommended": checkpoint.recommended_action,
        "created_at": checkpoint.created_at,
        "resolved": checkpoint.resolved,
        "resolution": checkpoint.resolution_action if checkpoint.resolved else None
    }
