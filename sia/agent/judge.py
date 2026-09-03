"""
LLM as a Judge - Automated Quality Evaluation for SIA.
Evaluates the final output DataFrame against the original input and inferred schema.
"""
import logging
import json
import pandas as pd
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Finalize must not block the job if Gemini hangs on the optional judge calls.
JUDGE_LLM_TIMEOUT_SEC = 45


def _load_system_prompt(name: str) -> str:
    """Load a markdown prompt file and strip frontmatter when present."""
    try:
        prompts_dir = Path(__file__).parent.parent.parent / "prompts"
        prompt_file = prompts_dir / f"{name}.md"
        if prompt_file.exists():
            content = prompt_file.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return content.strip()
    except Exception as exc:
        logger.warning(f"Failed to load system prompt {name}: {exc}")
    return ""

def safe_serialize(obj):
    """Safe serializer for JSON dumps, handling Timestamps and datetimes."""
    if isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date)):
        return obj.isoformat()
    if hasattr(obj, 'to_dict'):
        return obj.to_dict()
    return str(obj)

@dataclass
class JudgeScore:
    """Quality metrics for a processing job."""
    fidelity: float = 0.0  # 0.0 to 1.0 (Did we lose or hallucinate data?)
    flatness: float = 0.0  # 0.0 to 1.0 (Is the output properly normalized/unpivoted?)
    integrity: float = 0.0 # 0.0 to 1.0 (Does it match the inferred schema?)
    tool_accuracy: float = 0.0 # NEW: 0.0 to 1.0
    trajectory_success: float = 0.0 # NEW: 0.0 to 1.0
    task_success: bool = False    # NEW: Did it eventually produce the file?
    token_efficiency: float = 0.0 # NEW: 0.0 to 1.0
    verdict: str = "UNKNOWN"     # Pass/Fail/Human Review
    critique: str = ""    # Detailed explanation
    metrics: Dict[str, Any] = None # Raw metric counts
    trace_analysis: Optional[Dict[str, Any]] = None # Analysis of the execution process

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}

class LLMJudge:
    """
    Evaluates the quality of the agent's output using a 'Council of LLMs' pattern.
    """
    
    def __init__(self, llm_client):
        self.llm = llm_client
        self.system_prompt = _load_system_prompt("llm_judge")

    def _generate_json(self, prompt: str, timeout_sec: float = JUDGE_LLM_TIMEOUT_SEC):
        """Call the LLM with a hard timeout so finalize cannot hang indefinitely."""
        from .llm_handler import run_with_timeout

        return run_with_timeout(
            lambda: self.llm.generate_content(
                prompt, generation_config={"response_mime_type": "application/json"}
            ),
            timeout_sec,
            label="LLMJudge",
        )

    def evaluate_trace(self, trace_steps: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyze the execution trace for process quality.
        Returns both global scores and per-step evaluations.
        """
        if not trace_steps:
            return {"verdict": "UNKNOWN", "critique": "No trace steps available.", "step_scores": []}

        # Summarize trace for LLM context (reduce tokens)
        trace_summary = []
        for i, step in enumerate(trace_steps):
             # Basic fields
            summary = {"index": i}
            summary.update({k: v for k, v in step.items() if k in ["step", "tool", "success", "error", "confidence"]})
            # Summarize long messages
            if "message" in step:
                summary["message"] = step["message"][:200]
            # Summarize reasoning
            if "reasoning" in step:
                summary["reasoning"] = step["reasoning"][:200]
            trace_summary.append(summary)

        prompt = f"""
        You are a Process Auditor reviewing the logs of an AI Data Agent.
        
        EXECUTION TRACE:
        {json.dumps(trace_summary, indent=2, default=safe_serialize)}
        
        Evaluate the agent's performance based on:
        1. **Logic:** Did the agent follow a logical path (Plan -> Execute -> Verify)?
        2. **Tool Usage:** Were tools used effectively? (e.g. did 'analyze' proceed 'plan'?)
        3. **Error Handling:** If errors occurred, did the agent attempt to recover?
        4. **Efficiency:** Did it solve the problem without excessive looping?
        
        Return a JSON object with BOTH global scores AND individual step scores:
        {{
            "process_score": 0.0-1.0,
            "tool_accuracy": 0.0-1.0,
            "trajectory_score": 0.0-1.0,
            "logic_critique": "Assessment of the agent's reasoning",
            "tool_critique": "Assessment of tool usage (especially check for hallucinated tool names)",
            "error_handling": "Assessment of error recovery",
            "overall_summary": "Brief summary of the execution quality",
            "step_scores": [
                {{"index": 0, "step_name": "load_file", "logic_score": 0.9, "fidelity_score": 1.0, "critique": "Loaded file correctly."}},
                {{"index": 1, "step_name": "analyze_structure", "logic_score": 0.8, "fidelity_score": 0.9, "critique": "Identified most structures."}},
                ...
            ]
        }}
        The step_scores array should have one entry for each step in the input trace (using the same order/indices).
        """
        
        try:
            logger.info(
                "LLMJudge: starting trace evaluation (%s trace steps)",
                len(trace_steps),
            )
            response = self._generate_json(prompt)
            # Handle response object (has .text attribute)
            raw = response.text if hasattr(response, "text") else str(response)
            logger.info("LLMJudge: trace evaluation response received (%s chars)", len(raw or ""))
            if hasattr(response, "text"):
                return json.loads(response.text)
            return json.loads(str(response))
        except Exception as e:
            logger.error(f"Trace evaluation failed: {e}")
            return {"error": str(e), "overall_summary": f"Judge failed: {e}", "step_scores": []}

    def evaluate(self, 
                       input_sample: str, 
                       output_df: pd.DataFrame, 
                       schema: Dict[str, Any],
                       trace: List[Dict[str, Any]],
                       pipeline_evals_summary: str = "") -> JudgeScore:
        """
        Produce a quality report for the processing result.
        """
        from ..debug.llm_observer import get_observer
        observer = get_observer()
        
        try:
            # 1. Structural Analysis (Rule-based)
            flatness_score = self._calculate_flatness(output_df)
            
            # 2. Trace Analysis (Process Quality) - NEW
            trace_analysis = self.evaluate_trace(trace)

            # 3. LLM Critique (Fidelity & Integrity)
            output_sample = ""
            if isinstance(output_df, pd.DataFrame) and not output_df.empty:
                # Deduplicate columns to avoid "orient='records'" crash
                temp_df = output_df.copy()
                if not temp_df.columns.is_unique:
                    new_cols = []
                    seen = {}
                    for col in temp_df.columns:
                        col_str = str(col)
                        if col_str not in seen:
                            seen[col_str] = 0
                            new_cols.append(col_str)
                        else:
                            seen[col_str] += 1
                            new_cols.append(f"{col_str}_{seen[col_str]}")
                    temp_df.columns = new_cols
                output_sample = temp_df.head(10).to_json(orient='records')
            
            pipeline_block = ""
            if pipeline_evals_summary:
                pipeline_block = f"""
            DETERMINISTIC PIPELINE EVALS (Tier 1 — treat critical gate failures as hard integrity issues):
            {pipeline_evals_summary}
            """

            prompt = f"""
            You are a Senior Data Auditor evaluating the output of an Agentic Structure Inference system.
            
            INPUT SAMPLE (Markdown Grid):
            {input_sample}
            
            OUTPUT SAMPLE (JSON):
            {output_sample}
            
            INFERRED SCHEMA:
            {json.dumps(schema, indent=2, default=safe_serialize)}
            {pipeline_block}
            CRITERIA:
            1. FIDELITY: Are all values in the output derived from the input? (No hallucination).
            2. INTEGRITY: Does the output data match the types and roles defined in the schema?
            3. CORRECTNESS: If unpivoting occurred, are the values correctly associated with their new labels?
            
            Return a JSON object:
            {{
                "fidelity": 0.0-1.0,
                "integrity": 0.0-1.0,
                "task_success": true/false,
                "verdict": "PASS" | "FAIL" | "REVIEW",
                "critique": "Short explanation of the score"
            }}
            """
            full_prompt = f"{self.system_prompt}\n\n{prompt}" if self.system_prompt else prompt
            
            if observer:
                with observer.trace("llm_judge") as t:
                    t.system_prompt = self.system_prompt
                    t.user_prompt = prompt
                    t.full_prompt = full_prompt
                    
                    logger.info(
                        "LLMJudge: starting output critique (rows=%s, cols=%s)",
                        len(output_df) if isinstance(output_df, pd.DataFrame) else 0,
                        len(output_df.columns) if isinstance(output_df, pd.DataFrame) else 0,
                    )
                    response = self._generate_json(full_prompt)
                    t.raw_response = response.text
                    logger.info(
                        "LLMJudge: output critique response received (%s chars)",
                        len(response.text or "") if hasattr(response, "text") else 0,
                    )
                    
                    critique_data = json.loads(response.text)
                    t.parsed_output = critique_data
                    
                    score = JudgeScore(
                        fidelity=critique_data.get("fidelity", 0.0),
                        flatness=flatness_score,
                        integrity=critique_data.get("integrity", 0.0),
                        tool_accuracy=trace_analysis.get("tool_accuracy", 0.0),
                        trajectory_success=trace_analysis.get("trajectory_score", 0.0),
                        task_success=critique_data.get("task_success", False),
                        token_efficiency=self._calculate_token_efficiency(output_df, trace_analysis, schema),
                        verdict=critique_data.get("verdict", "REVIEW"),
                        critique=critique_data.get("critique", "No critique provided."),
                        metrics={
                            "total_rows": len(output_df),
                            "total_cols": len(output_df.columns),
                            "steps_taken": len(trace),
                            "total_tokens": trace_analysis.get("total_tokens", 0)
                        },
                        trace_analysis=trace_analysis
                    )
                    t.confidence_score = (score.fidelity + score.integrity) / 2
                    return score
            else:
                logger.info(
                    "LLMJudge: starting output critique (rows=%s, cols=%s)",
                    len(output_df) if isinstance(output_df, pd.DataFrame) else 0,
                    len(output_df.columns) if isinstance(output_df, pd.DataFrame) else 0,
                )
                response = self._generate_json(full_prompt)
                logger.info(
                    "LLMJudge: output critique response received (%s chars)",
                    len(response.text or "") if hasattr(response, "text") else 0,
                )
                critique_data = json.loads(response.text)
                
                return JudgeScore(
                    fidelity=critique_data.get("fidelity", 0.0),
                    flatness=flatness_score,
                    integrity=critique_data.get("integrity", 0.0),
                    tool_accuracy=trace_analysis.get("tool_accuracy", 0.0),
                    trajectory_success=trace_analysis.get("trajectory_score", 0.0),
                    task_success=critique_data.get("task_success", False),
                    token_efficiency=self._calculate_token_efficiency(output_df, trace_analysis, schema),
                    verdict=critique_data.get("verdict", "REVIEW"),
                    critique=critique_data.get("critique", "No critique provided."),
                    metrics={
                        "total_rows": len(output_df),
                        "total_cols": len(output_df.columns),
                        "steps_taken": len(trace)
                    },
                    trace_analysis=trace_analysis
                )
            
        except Exception as e:
            logger.error(f"Judge evaluation failed: {e}")
            return JudgeScore(0.0, 0.0, 0.0, 0.0, 0.0, False, 0.0, "ERROR", f"Judge failed: {e}", {}, {"overall_summary": f"System Error: {e}"})

    def _calculate_token_efficiency(self, df: pd.DataFrame, trace_analysis: Dict, schema: Dict) -> float:
        """
        Heuristic for token efficiency.
        Compares total tokens used vs complexity of the result.
        """
        total_tokens = trace_analysis.get("total_tokens", 0)
        if total_tokens == 0: return 0.0
        
        # Complexity factor: rows * cols * number of schema fields
        complexity = len(df) * len(df.columns) * len(schema.get("fields", []))
        
        # Heuristic: 100 tokens per complexity unit is "Efficient" (1.0)
        # If complexity is tiny (e.g. 5x5 table) but tokens are 10,000, efficiency is low.
        if complexity == 0: return 0.1
        
        efficiency = (complexity * 100) / total_tokens
        return max(0.0, min(1.0, efficiency))

    def _calculate_flatness(self, df: pd.DataFrame) -> float:
        """Rule-based flatness heuristic."""
        if df.empty: return 0.0
        # Heuristic: 
        # - Fewer columns + more rows usually indicates better unpivoting for hierarchical data.
        # - No 'Unnamed' columns.
        # - No sparse rows (>50% empty).
        unnamed_cols = [c for c in df.columns if 'unnamed' in str(c).lower()]
        penalty = len(unnamed_cols) / len(df.columns)
        
        sparsity = df.isna().sum().sum() / (df.shape[0] * df.shape[1])
        
        score = 1.0 - (penalty * 0.5) - (sparsity * 0.5)
        return max(0.0, min(1.0, score))
