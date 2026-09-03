"""
LLM Observer - LangSmith-like observability for LLM calls.
Provides structured tracing for all LLM components.
"""
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Dict, Any, Optional
from contextlib import contextmanager
from pathlib import Path

@dataclass
class Span:
    """A logical unit of work (step, node, etc.) that can contain other traces."""
    span_id: str
    run_id: str
    name: str
    type: str  # "node", "chain", "tool", "llm"
    start_time: str
    end_time: str = ""
    parent_id: Optional[str] = None
    input: Any = None
    output: Any = None
    status: str = "running"  # running, success, error
    system_prompt: str = ""  # For nodes that use an LLM
    user_prompt: str = ""    # For nodes that use an LLM
    metadata: Dict = field(default_factory=dict)
    
@dataclass
class LLMTrace:
    """Complete trace of a single LLM call."""
    
    # Identity
    trace_id: str
    run_id: str
    component: str  # "structure_analyzer", "plan_generator", etc.
    timestamp: str
    parent_id: Optional[str] = None  # To link to parent span
    
    # Context (What the LLM knows)
    system_prompt: str = ""
    retrieved_examples: List[Dict] = field(default_factory=list)
    input_context: Dict = field(default_factory=dict)
    
    # Prompt (What we asked)
    user_prompt: str = ""
    full_prompt: str = ""  # System + Context + User combined
    
    # Response (What we got)
    raw_response: str = ""
    parsed_output: Dict = field(default_factory=dict)
    
    # Metadata
    model_id: str = ""
    model_version: str = ""
    prompt_version: str = "1.0"
    
    # Performance
    latency_ms: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    
    # Quality
    confidence_score: float = 0.0
    validation_errors: List[str] = field(default_factory=list)
    success: bool = True
    error_message: str = ""


class LLMObserver:
    """
    LangSmith-like observability for LLM calls.
    Supports hierarchical tracing with Spans.
    """
    
    def __init__(self, run_id: str, model_id: str = "", enabled: bool = True):
        self.run_id = run_id
        self.model_id = model_id
        self.enabled = enabled
        self.traces: List[LLMTrace] = []
        self.spans: List[Span] = []
        self.tool_executions: List[Dict] = []  # Track tool calls
        self.state_snapshots: List[Dict] = []  # Compact AgentState / context / memory snapshots
        self._start_time = time.time()
        self._active_spans: List[Span] = [] # Stack of active spans
    
    @contextmanager
    def trace_span(self, name: str, type: str = "node", metadata: Dict = None, input: Any = None):
        """Context manager for tracing a logical span (step/node)."""
        if not self.enabled:
            yield None
            return

        parent_id = self._active_spans[-1].span_id if self._active_spans else None
        
        span = Span(
            span_id=str(uuid.uuid4())[:8],
            run_id=self.run_id,
            name=name,
            type=type,
            start_time=datetime.now().isoformat(),
            parent_id=parent_id,
            input=input,
            metadata=metadata or {}
        )
        
        self.spans.append(span)
        self._active_spans.append(span)
        start = time.time()
        
        try:
            yield span
            span.status = "success"
        except Exception as e:
            span.status = "error"
            span.metadata["error"] = str(e)
            raise
        finally:
            span.end_time = datetime.now().isoformat()
            duration_ms = (time.time() - start) * 1000
            span.metadata["duration_ms"] = duration_ms
            if self._active_spans:
                self._active_spans.pop()

    @contextmanager
    def trace(self, component: str, **initial_context):
        """Context manager for tracing an LLM call."""
        if not self.enabled:
            yield LLMTrace(
                trace_id="disabled",
                run_id=self.run_id,
                component=component,
                timestamp=datetime.now().isoformat()
            )
            return
        
        parent_id = self._active_spans[-1].span_id if self._active_spans else None
        
        trace = LLMTrace(
            trace_id=str(uuid.uuid4())[:8],
            run_id=self.run_id,
            component=component,
            timestamp=datetime.now().isoformat(),
            model_id=self.model_id,
            parent_id=parent_id,
            **initial_context
        )
        start = time.time()
        
        try:
            yield trace
            trace.success = True
        except Exception as e:
            trace.success = False
            trace.error_message = str(e)
            raise
        finally:
            trace.latency_ms = (time.time() - start) * 1000
            if trace.full_prompt:
                trace.input_tokens = len(trace.full_prompt) // 4
            if trace.raw_response:
                trace.output_tokens = len(trace.raw_response) // 4
            self.traces.append(trace)
    
    def get_component_traces(self, component: str) -> List[LLMTrace]:
        """Get all traces for a specific component."""
        return [t for t in self.traces if t.component == component]
    
    def get_all_traces(self) -> List[LLMTrace]:
        """Get all traces."""
        return self.traces
    
    def get_summary(self) -> Dict:
        """Get summary statistics for the current observer session only."""
        return summarize_llm_traces(self.export_for_debug_ui(), run_id=self.run_id, default_model_id=self.model_id)

    def update_span_metadata(self, key: str, value: Any):
        """Update metadata for the currently active span."""
        if self._active_spans:
            self._active_spans[-1].metadata[key] = value

    def annotate_last_trace_component(self, component: str, patch: Dict[str, Any]) -> None:
        """Merge ``patch`` into ``parsed_output`` on the most recent trace for ``component``."""
        if not patch:
            return
        for tr in reversed(self.traces):
            if tr.component != component:
                continue
            base = tr.parsed_output if isinstance(tr.parsed_output, dict) else {}
            merged = {**base, **patch}
            tr.parsed_output = merged
            return

    def export_for_debug_ui(self) -> List[Dict]:
        """Export traces in format for Debug UI."""
        # We need to unify spans, traces, and tools into a single list of events
        events = []
        
        # 1. Spans
        for s in self.spans:
            events.append({
                "event_id": s.span_id,
                "parent_event_id": s.parent_id,
                "process_type": s.type, # "node", "chain"
                "module": s.name,
                "operation": s.type,
                "timestamp": s.start_time,
                "duration_ms": s.metadata.get("duration_ms", 0),
                "status": s.status,
                "input_summary": str(s.input) if s.input else "",
                "output_summary": str(s.output) if s.output else ""
            })
            
        # 2. LLM Traces
        for t in self.traces:
            events.append({
                "event_id": t.trace_id,
                "parent_event_id": t.parent_id,
                "process_type": "llm_call",
                "module": t.component,
                "operation": "generate",
                "timestamp": t.timestamp,
                "duration_ms": t.latency_ms,
                "status": "success" if t.success else "error",
                "model": t.model_id,
                "input_tokens": t.input_tokens,
                "output_tokens": t.output_tokens,
                # Detail fields
                "system_prompt": t.system_prompt,
                "user_prompt": t.user_prompt,
                "raw_response": t.raw_response,
                "retrieved_examples": t.retrieved_examples,
                "confidence_score": t.confidence_score,
                "input_context": t.input_context,
            })
            
        # 3. Tool Executions
        for tool in self.tool_executions:
            # Tools don't strictly have parent_id in current log_tool_execution
            # We'll need to infer or add it. For now, try to find active span when logged.
            # (Requires modifying log_tool_execution to capture active span)
            pass 
            
        # For now, return the unified event list which debug.js can build into a tree
        # But wait, debug.js expects 'llm_traces' list separate from 'trace'?
        # Actually existing debug.js handles `llm_traces` separately in `renderLLMCallsTab`.
        # To support the Tree view, we should probably output a unified tree structure
        # OR update debug.js to build tree from this list.
        # The prompt inspector and summary tabs rely on the specific structure of LLMTrace objects.
        # So we should return the list of LLMTrace objects as before, BUT with parent_ids added.
        
        return [asdict(t) for t in self.traces]
        
    def get_hierarchical_events(self) -> List[Dict]:
        """Get all events (spans, traces, tools) as a flat list with parent_ids."""
        events = []
        
        def summarize(obj):
            if isinstance(obj, str): return obj
            if isinstance(obj, (int, float, bool)): return str(obj)
            if hasattr(obj, 'shape'): # DataFrame summary
                return f"DataFrame {obj.shape[0]}x{obj.shape[1]}"
            if isinstance(obj, list):
                if not obj: return "[]"
                # Check if it's a list of tool calls
                if isinstance(obj[0], dict) and "tool" in obj[0]:
                    return f"Tools: {', '.join([t.get('tool', 'unknown') for t in obj])}"
                return f"List with {len(obj)} items"
            if isinstance(obj, dict):
                # Specific logic for our agent's dicts
                fp = obj.get("file_path") or obj.get("pipeline_workbook_path")
                if fp:
                    bn = Path(str(fp)).name
                    sh = obj.get("sheet_name") or obj.get("sheet") or ""
                    if sh:
                        return f"{bn} · {sh}"
                    return bn
                if "message" in obj: return str(obj["message"])
                if "status" in obj: return f"Status: {obj['status']}"
                if "tables" in obj: # Structure Analysis
                    return f"Analysis: {len(obj['tables'])} tables found. Conf: {obj.get('confidence', 0):.1%}"
                if "tool_calls" in obj: # Plan
                    return f"Plan: {len(obj['tool_calls'])} tools"
                if "fields" in obj: # Schema
                    return f"Schema: {len(obj['fields'])} fields"
                return f"Keys: {list(obj.keys())}"
            return str(obj)

        for s in self.spans:
            events.append({
                "event_id": s.span_id,
                "parent_event_id": s.parent_id,
                "process_type": s.type,
                "module": s.name, 
                "operation": s.type,
                "timestamp": s.start_time,
                "duration_ms": s.metadata.get("duration_ms", 0),
                "status": s.status,
                "input_summary": summarize(s.input),
                "output_summary": summarize(s.output),
                "system_prompt": s.system_prompt,
                "user_prompt": s.user_prompt,
                "metadata": s.metadata 
            })
            
        for t in self.traces:
             events.append({
                "event_id": t.trace_id,
                "parent_event_id": t.parent_id,
                "process_type": "llm_call",
                "module": t.component,
                "operation": "generate",
                "timestamp": t.timestamp,
                "duration_ms": t.latency_ms,
                "status": "success" if t.success else "error",
                "model": t.model_id or self.model_id or "unknown",
                "input_tokens": t.input_tokens,
                "output_tokens": t.output_tokens,
                "confidence_score": t.confidence_score,
                # Detail fields for modal/pane
                "system_prompt": t.system_prompt,
                "user_prompt": t.user_prompt,
                "raw_response": t.raw_response,
                "parsed_output": t.parsed_output if isinstance(t.parsed_output, dict) else {},
                "retrieved_examples": t.retrieved_examples,
                "input_summary": t.user_prompt[:200] if t.user_prompt else "",
                "output_summary": t.raw_response[:200] if t.raw_response else "",
                "input_context": t.input_context,
            })
            
        for tool in self.tool_executions:
             ps = tool.get("pipeline_stage")
             meta = {"pipeline_stage": ps} if ps else {}
             events.append({
                "event_id": tool.get("event_id") or str(uuid.uuid4())[:8],
                "parent_event_id": tool.get("parent_id"),
                "process_type": "tool_execution",
                "module": tool["tool"],
                "operation": "execute",
                "timestamp": tool["timestamp"],
                "duration_ms": tool["duration_ms"],
                "status": "success" if tool["success"] else "error",
                "result": tool["result"],
                "params": tool.get("params", {}),
                "input_preview": tool.get("input_preview", {}),
                "output_preview": tool.get("output_preview", {}),
                "snapshot_path": tool.get("snapshot_path"),
                "pipeline_stage": ps,
                "input_summary": f"Params: {list(tool.get('params', {}).keys())}",
                "output_summary": tool["result"][:200] if tool["result"] else "",
                "metadata": meta,
            })

        return events

    def log_state_snapshot(self, label: str, phase: str, payload: Dict):
        """Record a compact state/context/memory snapshot for Debug UI inspection."""
        if not self.enabled:
            return
        event_id = str(uuid.uuid4())[:8]
        snapshot_id = f"state-{event_id}"
        payload = dict(payload or {})
        payload["snapshot_id"] = snapshot_id
        payload.setdefault("anchor_event_id", self._active_spans[-1].span_id if self._active_spans else None)
        payload.setdefault("label", label)
        payload.setdefault("phase", phase)
        payload.setdefault("timestamp", datetime.now().isoformat())
        payload.setdefault("event_id", event_id)
        agent_state = payload.get("agent_state") if isinstance(payload.get("agent_state"), dict) else {}
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        run = agent_state.get("run") if isinstance(agent_state.get("run"), dict) else {}
        deferrals = context.get("deferrals") if isinstance(context.get("deferrals"), dict) else {}
        src = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        payload.setdefault(
            "summary",
            (
                f"source={src.get('source_id') or '?'} "
                f"multi_source={run.get('multi_source_active_batch')} "
                f"defer_weekly={deferrals.get('weekly_rollups_to_post_union')}"
            ),
        )
        self.state_snapshots.append(payload)

    def get_state_snapshots(self) -> List[Dict]:
        """Get compact state snapshots."""
        return self.state_snapshots

    def log_tool_execution(self, tool_name: str, params: Dict, result: str, 
                           success: bool = True, duration_ms: float = 0,
                           input_preview: Dict = None, output_preview: Dict = None,
                           snapshot_path: str = None,
                           pipeline_stage: Optional[str] = None):
        """
        Log a tool execution for the Debug UI.
        """
        parent_id = self._active_spans[-1].span_id if self._active_spans else None
        
        self.tool_executions.append({
            "event_id": str(uuid.uuid4())[:8], # Add ID for tree
            "parent_id": parent_id, # Capture parent
            "tool": tool_name,
            "step": len(self.tool_executions) + 1,
            "params": params,
            "result": result,
            "success": success,
            "duration_ms": duration_ms,
            "timestamp": datetime.now().isoformat(),
            "input_preview": input_preview or {},
            "output_preview": output_preview or {},
            "snapshot_path": snapshot_path,
            "pipeline_stage": pipeline_stage,
        })
    
    def get_tool_executions(self) -> List[Dict]:
        """Get all tool executions."""
        return self.tool_executions
    
    def to_json(self) -> str:
        """Export as JSON string."""
        return json.dumps({
            "summary": self.get_summary(),
            "traces": self.export_for_debug_ui(),
            "events": self.get_hierarchical_events() # New field for tree view
        }, indent=2, default=str)


def summarize_llm_traces(
    traces: Optional[List[Dict[str, Any]]],
    *,
    run_id: str = "",
    default_model_id: str = "",
) -> Dict[str, Any]:
    """
    Aggregate Run Summary metrics across all persisted LLM trace rows.

    ``job['llm_traces']`` accumulates every capture; ``observer.get_summary()`` only
  covers the latest batch and must not replace the job-level summary.
    """
    rows = [t for t in (traces or []) if isinstance(t, dict)]
    if not rows:
        return {"total_calls": 0}

    latencies: List[float] = []
    input_tokens = 0
    output_tokens = 0
    confidences: List[float] = []
    successes: List[bool] = []
    errors: List[str] = []
    components: set = set()
    models: List[str] = []

    for row in rows:
        ms = row.get("latency_ms")
        if ms is None:
            ms = row.get("duration_ms")
        latencies.append(float(ms or 0.0))
        input_tokens += int(row.get("input_tokens") or 0)
        output_tokens += int(row.get("output_tokens") or 0)
        confidences.append(float(row.get("confidence_score") or 0.0))
        ok = bool(row.get("success", True))
        successes.append(ok)
        if not ok:
            err = str(row.get("error_message") or "").strip()
            if err:
                errors.append(err)
        comp = str(row.get("component") or row.get("module") or "").strip()
        if comp:
            components.add(comp)
        model = str(row.get("model_id") or row.get("model") or "").strip()
        if model:
            models.append(model)

    unique_models = sorted({m for m in models if m})
    if len(unique_models) == 1:
        model_id = unique_models[0]
    elif unique_models:
        model_id = f"{unique_models[0]} (+{len(unique_models) - 1} more)"
    else:
        model_id = default_model_id or ""

    return {
        "run_id": str(rows[0].get("run_id") or run_id or ""),
        "model_id": model_id,
        "total_calls": len(rows),
        "total_latency_ms": sum(latencies),
        "total_input_tokens": input_tokens,
        "total_output_tokens": output_tokens,
        "avg_confidence": sum(confidences) / len(confidences) if confidences else 0.0,
        "success_rate": sum(1 for ok in successes if ok) / len(successes) if successes else 0.0,
        "components": sorted(components),
        "errors": errors,
    }


def refresh_job_llm_summary(job: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute ``job['llm_summary']`` from all accumulated ``llm_traces``."""
    summary = summarize_llm_traces(job.get("llm_traces") or [])
    if summary.get("total_calls"):
        job["llm_summary"] = summary
    else:
        job.setdefault("llm_summary", summary)
    return job.get("llm_summary") or summary


import threading

# Thread-local storage for observer instance
class ObserverStorage(threading.local):
    def __init__(self):
        self.current = None

_storage = ObserverStorage()


def init_observer(run_id: str, model_id: str = "", enabled: bool = True) -> LLMObserver:
    """Initialize the observer for the current thread."""
    observer = LLMObserver(run_id, model_id, enabled)
    _storage.current = observer
    return observer


def get_observer() -> Optional[LLMObserver]:
    """Get the observer for the current thread."""
    return getattr(_storage, 'current', None)


def clear_observer():
    """Clear the observer for the current thread."""
    _storage.current = None
