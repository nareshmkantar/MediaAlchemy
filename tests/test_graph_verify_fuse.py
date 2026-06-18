from sia.agent.graph import should_retry


def test_verify_cycle_fuse_returns_finalize():
    """Failsafe when verify_output keeps running without converging (iteration bug, etc.)."""
    state = {
        "is_flat": False,
        "hitl_pending_approval": False,
        "iteration": 1,
        "max_iterations": 3,
        "confidence_trajectory": [0.9, 0.9, 0.9],
        "trace_steps": [{"step": "verify_output"} for _ in range(10)],
    }
    assert should_retry(state) == "finalize"
