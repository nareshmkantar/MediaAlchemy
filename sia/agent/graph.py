"""
Graph definition for Schema Agent.

Updated with ReAct pattern:
- verify_output → replan (LLM thinking) → execute_tools

HITL Enhancement:
- execute_tools pauses if destructive tools need approval

Cross-source relationship inference is **not** a mandatory graph node; use the optional tool
``discovery.propose_file_relationships`` inside ``execute_tools`` when the plan should
refresh proposals before transforms.
"""
from langgraph.graph import StateGraph, END
from .graph_resume import route_after_load_file, route_graph_entry
from .state import AgentState
from .nodes import (
    load_file_node,
    resolve_mapping_node,
    analyze_structure_node,
    generate_plan_node,
    execute_tools_node,
    verify_output_node,
    replan_node,  # NEW: Intelligent retry
    finalize_node
)

def is_stalled(state: AgentState) -> bool:
    """
    Check if the agent is making progress.
    Returns True if confidence confuses to plateau or loop detected.
    """
    history = state.get("confidence_trajectory", [])
    if len(history) < 2:
        return False
        
    # Check for plateau (last 2 scores identical and low)
    if history[-1] == history[-2] and history[-1] < 0.8:
        return True
        
    # Check for too many iterations without success
    if state.get("iteration", 0) > state.get("max_iterations", 3):
        return True
        
    return False

# Conditional edge logic
def should_retry(state: AgentState) -> str:
    """
    Decide next step after verification.
    
    Routes:
    - 'finalize': Data is flat
    - 'replan': Need another iteration (standard loop)
    - 'hitl_stall': Stalled (confidence plateau or repeated issue)
    """
    from .base import robust_bool
    
    if robust_bool(state.get("is_flat")):
        return "finalize"

    # Respect verification-driven HITL decisions before retrying.
    if state.get("hitl_pending_approval", False):
        print("[GRAPH] Verification requested HITL review. Pausing graph.", flush=True)
        return "hitl_stall"

    verify_runs = sum(
        1
        for s in (state.get("trace_steps") or [])
        if isinstance(s, dict) and str(s.get("step", "")).strip() == "verify_output"
    )
    mx = int(state.get("max_iterations", 3) or 3)
    fuse_limit = mx + 6
    if verify_runs > fuse_limit:
        print(
            f"[GRAPH] verify cycle fuse ({verify_runs} verify_output steps > {fuse_limit}); forcing finalize",
            flush=True,
        )
        return "finalize"
    
    # Check for stall BEFORE checking max_iterations
    if is_stalled(state):
        print(f"[GRAPH] Stall detected in iteration {state.get('iteration')}! Diverting to HITL.", flush=True)
        return "hitl_stall"
        
    if state["iteration"] >= state["max_iterations"]:
        return "finalize"  # Give up, but flag for review in finalize
    else:
        return "replan"  # Go to LLM for intelligent retry


def should_pause_for_hitl(state: AgentState) -> str:
    """
    Check if execute_tools returned a HITL pause request.
    """
    if state.get("hitl_pending_approval", False):
        print(f"[GRAPH] HITL Pause triggered for destructive operations.", flush=True)
        return "hitl_pause"
    return "verify_output"


def should_pause_after_plan(state: AgentState) -> str:
    """Pause after planning when analyst review is required before execution."""
    if state.get("hitl_pending_approval", False) and state.get("hitl_pause_type") == "plan_review":
        print("[GRAPH] HITL Pause triggered for plan review.", flush=True)
        return "hitl_pause"
    return "execute_tools"


def should_pause_after_replan(state: AgentState) -> str:
    """Stop a no-progress or explicitly escalated replan before re-execution."""
    if state.get("hitl_pending_approval", False):
        print("[GRAPH] HITL Pause triggered after replanning.", flush=True)
        return "hitl_pause"
    return "execute_tools"


def should_pause_after_mapping(state: AgentState) -> str:
    """Pause after mapping when file-relationship review is already pending (e.g. resume)."""
    if state.get("hitl_pending_approval", False) and state.get("hitl_pause_type") == "file_relationship_review":
        print("[GRAPH] HITL Pause triggered for file relationship review.", flush=True)
        return "hitl_pause"
    return "generate_plan"


def create_graph():
    workflow = StateGraph(AgentState)
    
    # Add nodes
    workflow.add_node("load_file", load_file_node)
    workflow.add_node("resolve_mapping", resolve_mapping_node)
    workflow.add_node("analyze_structure", analyze_structure_node)
    workflow.add_node("generate_plan", generate_plan_node)
    workflow.add_node("execute_tools", execute_tools_node)
    workflow.add_node("verify_output", verify_output_node)
    workflow.add_node("replan", replan_node)  # NEW: LLM thinking step
    workflow.add_node("finalize", finalize_node)
    
    # Define edges - main flow
    workflow.add_conditional_edges(
        "load_file",
        route_after_load_file,
        {
            "analyze_structure": "analyze_structure",
            "execute_tools": "execute_tools",
        },
    )
    workflow.add_edge("analyze_structure", "resolve_mapping")
    workflow.add_conditional_edges(
        "resolve_mapping",
        should_pause_after_mapping,
        {
            "hitl_pause": END,
            "generate_plan": "generate_plan",
        }
    )
    workflow.add_conditional_edges(
        "generate_plan",
        should_pause_after_plan,
        {
            "hitl_pause": END,
            "execute_tools": "execute_tools"
        }
    )
    
    # Conditional edge from execute_tools - check for HITL pause
    workflow.add_conditional_edges(
        "execute_tools",
        should_pause_for_hitl,
        {
            "hitl_pause": END,  # Pause graph, return to web server for approval
            "verify_output": "verify_output"
        }
    )
    
    # Conditional edge from verify_output
    workflow.add_conditional_edges(
        "verify_output",
        should_retry,
        {
            "finalize": "finalize",
            "replan": "replan",  # Route to LLM for thinking
            "hitl_stall": END    # Pause graph for human intervention
        }
    )
    
    workflow.add_conditional_edges(
        "replan",
        should_pause_after_replan,
        {
            "hitl_pause": END,
            "execute_tools": "execute_tools",
        },
    )
    
    workflow.add_edge("finalize", END)

    # HITL resume: skip load/analyze/mapping/plan when saved state still has grid + plan.
    workflow.set_conditional_entry_point(
        route_graph_entry,
        {
            "load_file": "load_file",
            "generate_plan": "generate_plan",
            "execute_tools": "execute_tools",
            "finalize": "finalize",
        },
    )

    return workflow.compile()
