"""
Agent State Definition for LangGraph

Enhanced state with semantic tracking for:
- Tool execution history with results
- Issue tracking across iterations
- Confidence trajectory over time
- HITL checkpoints
- Rollback support
"""
from typing import TypedDict, List, Optional, Dict, Any, Annotated

try:
    from typing import NotRequired
except ImportError:  # pragma: no cover - py<3.11
    from typing_extensions import NotRequired
import operator
import pandas as pd


class ToolExecutionRecord(TypedDict):
    """Record of a single tool execution."""
    step: int
    tool: str
    params: Dict[str, Any]
    success: bool
    message: str
    rows_before: int
    rows_after: int
    duration_ms: float


class IssueRecord(TypedDict):
    """Record of an issue found during verification."""
    iteration: int
    issue_type: str
    description: str
    affected_columns: List[str]
    severity: str  # low, medium, high
    resolved: bool
    resolution_tool: Optional[str]


class AgentState(TypedDict):
    """
    Enhanced agent state with semantic tracking.
    
    This state is passed through all nodes in the LangGraph workflow.
    """
    # ========== Input ==========
    file_path: str
    sheet_name: str
    target_template: Optional[Dict[str, Any]] # NEW: Job-specific target template
    template_contract: NotRequired[Optional[Dict[str, Any]]]  # normalized x_scope + column_rules
    context_packet: Optional[Dict[str, Any]]
    source_metadata: Optional[Dict[str, Any]]
    approved_mappings: List[Dict[str, Any]]
    business_rules: List[Dict[str, Any]]
    mapping_summary: Optional[Dict[str, Any]]
    relationship_proposals: List[Dict[str, Any]]
    approved_relationships: List[Dict[str, Any]]
    # Registry id for multi-source runs; kept on state so HITL ``pending_state`` survives graph round-trips.
    source_id: Optional[str]
    # True when web batch is processing multiple sources in one job (union deferral, registry).
    multi_source_active_batch: NotRequired[bool]
    # Tools removed from per-source execution until after union/collation (e.g. weekly rollup).
    deferred_post_collate_tools: NotRequired[List[Dict[str, Any]]]

    # ========== Intermediate ==========
    grid: Optional[Any]  # VisualGrid object
    structure_analysis: Optional[Dict[str, Any]]
    structure_report: Optional[Dict[str, Any]]  # inspect_sheet_structure output
    file_inventory: NotRequired[Optional[Dict[str, Any]]]  # discovery.inventory summary at load
    extraction_plan: Optional[Any]  # ExtractionPlan object
    current_df: Optional[pd.DataFrame]
    scoped_source: Optional[Dict[str, Any]]  # Shared selected-sheet/demarcation scope
    source_frame: Optional[Dict[str, int]]  # NEW: {rows, cols} of the raw Excel
    current_frame: Optional[Dict[str, int]] # NEW: {rows, cols} of the active DataFrame
    planned_rule_actions: List[Dict[str, Any]]
    approval_items: List[Dict[str, Any]]
    resume_mode: Optional[str]
    resume_graph_from: NotRequired[Optional[str]]
    hitl_resume_from: NotRequired[Optional[str]]
    resume_skip_pipeline_after_load: NotRequired[bool]
    
    # ========== Verification Loop ==========
    is_flat: bool
    iteration: int
    max_iterations: int
    suggested_tools: List[Dict[str, Any]]
    last_replan_tools: NotRequired[List[Dict[str, Any]]]
    verifier_issues: List[Dict[str, Any]] # NEW: To surface in UI
    
    # ========== Output ==========
    final_schema: Optional[Any]  # InferredSchema object
    judge_result: Optional[Dict[str, Any]]  # LLM Judge Evaluation
    
    # ========== Trace/Logs ==========
    trace_steps: Annotated[List[Dict[str, Any]], operator.add]
    errors: Annotated[List[str], operator.add]
    warnings: Annotated[List[str], operator.add]
    
    # ========== HITL ==========
    requires_review: bool
    review_reason: str
    hitl_checkpoints: List[Dict[str, Any]]  # List of HITLCheckpoint dicts
    escalation_reason: str  # Why HITL was triggered
    destructive_approved: bool # NEW: User approved destructive tools
    hitl_pending_approval: bool # NEW: Graph is paused for approval
    hitl_pause_type: Optional[str]
    deletion_previews: List[Dict[str, Any]] # NEW: Previews for approval UI
    pending_destructive_tools: List[Dict[str, Any]] # NEW: Tools waiting for approval
    low_confidence_items: List[Dict[str, Any]] # NEW: Items flagged for low confidence (column matching or tool calls)
    
    # ========== Semantic Tracking (NEW) ==========
    
    # Tool execution history - track what tools ran and their results
    tools_history: Annotated[List[ToolExecutionRecord], operator.add]
    
    # Issue tracking across iterations - detect repeated issues
    issues_history: Annotated[List[IssueRecord], operator.add]
    
    # Confidence after each major step - detect degradation
    confidence_trajectory: Annotated[List[float], operator.add]
    
    # Rollback support - save last known good state
    last_valid_checkpoint: Optional[pd.DataFrame]
    checkpoint_iteration: int  # Which iteration the checkpoint is from
    
    # Expected output from plan - for verification comparison
    expected_columns: List[str]
    expected_row_count_estimate: Optional[int]
    
    # ========== Context ==========
    llm_client: Optional[Any]
    debug_enabled: bool
    enable_llm_judge: bool  # When False, skip LLMJudge in finalize (no extra LLM call / verdict gating)
    hitl_manager: Optional[Any]  # HITLManager instance


def create_initial_state(
    file_path: str,
    sheet_name: str = "Sheet1",
    llm_client: Any = None,
    debug_enabled: bool = False,
    max_iterations: int = 3,
    hitl_manager: Any = None
) -> AgentState:
    """
    Create initial agent state with all fields properly initialized.
    
    Args:
        file_path: Path to the Excel file
        sheet_name: Name of sheet to process
        llm_client: LLM client instance
        debug_enabled: Whether to enable debug logging
        max_iterations: Maximum verification iterations
        hitl_manager: HITL manager instance
        
    Returns:
        Initialized AgentState
    """
    return AgentState(
        # Input
        file_path=file_path,
        sheet_name=sheet_name,
        target_template=None,
        context_packet=None,
        source_metadata=None,
        approved_mappings=[],
        business_rules=[],
        mapping_summary=None,
        relationship_proposals=[],
        approved_relationships=[],
        source_id=None,

        # Intermediate
        grid=None,
        structure_analysis=None,
        structure_report=None,
        file_inventory=None,
        extraction_plan=None,
        current_df=None,
        scoped_source=None,
        source_frame=None,
        current_frame=None,
        planned_rule_actions=[],
        approval_items=[],
        resume_mode=None,
        
        # Verification loop
        is_flat=False,
        iteration=0,
        max_iterations=max_iterations,
        suggested_tools=[],
        
        # Output
        final_schema=None,
        judge_result=None,
        
        # Trace/Logs
        trace_steps=[],
        errors=[],
        warnings=[],
        
        # HITL
        requires_review=False,
        review_reason="",
        hitl_checkpoints=[],
        escalation_reason="",
        hitl_pending_approval=False,
        hitl_pause_type=None,
        deletion_previews=[],
        pending_destructive_tools=[],
        low_confidence_items=[],
        
        # Semantic tracking
        tools_history=[],
        issues_history=[],
        confidence_trajectory=[],
        last_valid_checkpoint=None,
        checkpoint_iteration=0,
        expected_columns=[],
        expected_row_count_estimate=None,
        
        # Context
        llm_client=llm_client,
        debug_enabled=debug_enabled,
        enable_llm_judge=True,
        hitl_manager=hitl_manager
    )


def add_tool_execution(
    state: AgentState,
    tool: str,
    params: Dict,
    success: bool,
    message: str,
    rows_before: int,
    rows_after: int,
    duration_ms: float = 0,
    *,
    base_history: Optional[List[ToolExecutionRecord]] = None,
) -> List[ToolExecutionRecord]:
    """
    Add a tool execution record to history.

    When ``base_history`` is provided (e.g. inside ``execute_tools``), append after that
    list so multiple tools in one node accumulate correctly. ``state`` is unchanged until
    the graph merges the returned update, so relying on ``state["tools_history"]`` alone
    would drop all but the last in-batch tool.

    Returns updated tools_history list (LangGraph requires returning new state).
    """
    history = list(base_history) if base_history is not None else list(state.get("tools_history", []))
    record = ToolExecutionRecord(
        step=len(history) + 1,
        tool=tool,
        params=params,
        success=success,
        message=message,
        rows_before=rows_before,
        rows_after=rows_after,
        duration_ms=duration_ms,
    )
    history.append(record)
    return history


def add_issue(
    state: AgentState,
    issue_type: str,
    description: str,
    affected_columns: List[str] = None,
    severity: str = "medium"
) -> List[IssueRecord]:
    """
    Add an issue record to history.
    
    Returns updated issues_history list.
    """
    record = IssueRecord(
        iteration=state.get("iteration", 0),
        issue_type=issue_type,
        description=description,
        affected_columns=affected_columns or [],
        severity=severity,
        resolved=False,
        resolution_tool=None
    )
    
    history = list(state.get("issues_history", []))
    history.append(record)
    return history


def is_issue_repeated(state: AgentState, issue_type: str, min_count: int = 2) -> bool:
    """
    Check if an issue type has been found multiple times.
    
    Used to detect verification stalls.
    """
    issues = state.get("issues_history", [])
    count = sum(1 for i in issues if i["issue_type"] == issue_type and not i["resolved"])
    return count >= min_count


def get_confidence_trend(state: AgentState) -> str:
    """
    Analyze confidence trajectory to detect trends.
    
    Returns: "improving", "degrading", "stable", or "unknown"
    """
    trajectory = state.get("confidence_trajectory", [])
    if len(trajectory) < 2:
        return "unknown"
    
    recent = trajectory[-3:] if len(trajectory) >= 3 else trajectory
    
    if all(recent[i] < recent[i+1] for i in range(len(recent)-1)):
        return "improving"
    elif all(recent[i] > recent[i+1] for i in range(len(recent)-1)):
        return "degrading"
    else:
        return "stable"


def create_checkpoint(state: AgentState) -> Dict[str, Any]:
    """
    Create a checkpoint of current state for rollback.
    
    Returns dict with checkpoint data.
    """
    df = state.get("current_df")
    return {
        "df_copy": df.copy() if df is not None else None,
        "iteration": state.get("iteration", 0),
        "tools_count": len(state.get("tools_history", [])),
        "confidence": state.get("confidence_trajectory", [])[-1] if state.get("confidence_trajectory") else None
    }


def is_stalled(state: AgentState) -> bool:
    """
    Check if the agent is stuck in an unproductive loop.

    High confidence is not a stall — 0.85+ means the verifier is largely happy
    and remaining gaps should finish via max-iterations → finalize, not HITL.
    """
    trajectory = [float(x) for x in (state.get("confidence_trajectory") or []) if x is not None]
    latest = trajectory[-1] if trajectory else 0.0
    if latest >= 0.85:
        return False

    if len(trajectory) >= 3:
        last_three = trajectory[-3:]
        if abs(last_three[-1] - last_three[-2]) < 0.05 and abs(last_three[-2] - last_three[-3]) < 0.05:
            return True

    issues = state.get("issues_history", [])
    if issues:
        types = [i["issue_type"] for i in issues if not i.get("resolved")]
        from collections import Counter
        counts = Counter(types)
        if any(count >= 3 for count in counts.values()):
            return True

    return False

def should_rollback(state: AgentState) -> bool:
    """
    Determine if we should rollback to last checkpoint.
    
    Triggers:
    - Confidence dropped significantly
    - Data became empty
    - Same issue repeated too many times
    """
    current_df = state.get("current_df")
    checkpoint_df = state.get("last_valid_checkpoint")
    
    # Data became empty but we had data before
    if (current_df is None or current_df.empty) and checkpoint_df is not None and not checkpoint_df.empty:
        return True
    
    # Significant confidence drop
    trajectory = state.get("confidence_trajectory", [])
    if len(trajectory) >= 2:
        if trajectory[-1] < trajectory[-2] * 0.5:  # >50% drop
            return True
    
    return False
