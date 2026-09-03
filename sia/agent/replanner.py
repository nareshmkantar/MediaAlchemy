"""
Replanner - LLM component for intelligent retry when verification fails.
"""
import logging
import json
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
from .base import ExtractionPlan, VerificationResult, load_prompt_from_file
from .context_packet import effective_target_date_granularity
from .llm_handler import LLMCallWrapper, RetryConfig, robust_json_parse, JSONParseError
from ..debug.llm_observer import get_observer
from ..tools.tool_validator import (
    dedupe_redundant_tool_calls,
    normalize_tool_name,
    validate_tool_sequence,
    sort_tool_calls_by_pipeline_stage,
)
from ..tools.pipeline_catalog import augment_system_prompt_with_catalog
from .target_template_utils import normalize_target_template

logger = logging.getLogger(__name__)


def tool_plan_fingerprint(tool_calls: List[Dict[str, Any]]) -> str:
    """Stable semantic identity for a plan, excluding step labels and prose."""
    canonical: List[Dict[str, Any]] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            continue
        tool, _ = normalize_tool_name(str(call.get("tool") or ""))
        canonical.append(
            {
                "tool": tool,
                "params": call.get("params") if isinstance(call.get("params"), dict) else {},
            }
        )
    return json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)


def _enrich_replanned_tools(
    tool_calls: List[Dict[str, Any]],
    target_template: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Fill deterministic template-owned params before tool validation."""
    template = normalize_target_template(target_template) if target_template else {}
    column_rules = (
        list((template.get("business_logic") or {}).get("column_rules") or [])
        if isinstance(template.get("business_logic"), dict)
        else []
    )
    enriched: List[Dict[str, Any]] = []
    for call in tool_calls or []:
        if not isinstance(call, dict):
            enriched.append(call)
            continue
        item = dict(call)
        params = dict(item.get("params") or {})
        canonical, _ = normalize_tool_name(str(item.get("tool") or ""))
        if canonical == "verify.schema" and template.get("properties"):
            params["schema"] = template
        elif canonical == "transform.apply_column_rules" and column_rules and not params.get("column_rules"):
            params["column_rules"] = column_rules
        item["params"] = params
        enriched.append(item)
    return enriched


def _format_granularity_alignment_for_replan(alignment: Optional[Dict[str, Any]]) -> str:
    if not isinstance(alignment, dict) or not str(alignment.get("planner_obligation") or "").strip():
        return ""
    rec = alignment.get("recommended_primary_tool") or ""
    rec_line = f"\n- **Preferred tool path**: {rec}" if rec else ""
    return f"""
### Date granularity alignment (carry into revised plan)
- **Source (Guided Setup)**: {alignment.get('source_date_granularity') or alignment.get('normalized_source') or 'not specified'}
- **Target template**: {alignment.get('target_date_granularity') or alignment.get('normalized_target') or 'not specified'}
- **Obligation**: {alignment.get('planner_obligation')}{rec_line}
"""


def _debug_log(hypothesis_id: str, message: str, data: Dict[str, Any]) -> None:
    try:
        payload = {
            "sessionId": "3fc92d",
            "runId": str(data.get("job_id") or "unknown"),
            "hypothesisId": hypothesis_id,
            "location": "sia/agent/replanner.py",
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
        }
        with open(Path(__file__).parent.parent.parent / "debug-3fc92d.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass

class Replanner:
    """
    LLM component for intelligent retry when verification fails.
    
    This is the ReAct "Think" step that was missing in the original flow.
    It:
    1. Reviews execution history (what ran and whether it succeeded)
    2. Identifies why previous attempts failed or degraded
    3. Produces a corrected plan — including reusing tools when params or upstream context changed
    
    Enhanced with retry logic and context-rich prompting.
    """
    
    def __init__(self, llm_client, retry_config: RetryConfig = None):
        # Wrap LLM client with retry logic
        self.llm_wrapper = LLMCallWrapper(
            llm_client,
            retry_config=retry_config or RetryConfig(max_attempts=3)
        )
        self.llm_client = llm_client  # Backward compat
        
        self.SYSTEM_PROMPT, self.PROMPT_VERSION = load_prompt_from_file("replanner")
        if not self.SYSTEM_PROMPT:
            self.SYSTEM_PROMPT = """You are an expert data engineer troubleshooting failed data transformations.

Your task is to analyze what went wrong and propose a corrected plan.

Key principles:
1. Do not repeat the exact same tool+params that already failed without a material change; rerunning the same tool with fixed params or after upstream fixes is often correct
2. Consider whether columns, layout, or types were misunderstood initially
3. Look for patterns in what tools succeeded vs failed
4. When stuck, consider more aggressive transformations or accepting partial success

You must respond in JSON format with:
{
    "analysis": "Brief analysis of what went wrong",
    "root_cause": "Likely root cause of the repeated issue",
    "new_strategy": "Description of the revised approach",
    "tool_calls": [...],
    "confidence": 0.0-1.0,
    "recommendation": "proceed" | "accept_partial" | "escalate_to_human"
}"""
            self.PROMPT_VERSION = "1.0"
    
    def replan(
        self,
        current_df,
        previous_tools: List[Dict],
        previous_issues: List[Dict],
        verification_result: Optional[VerificationResult],
        iteration: int,
        max_iterations: int,
        target_template: Optional[Dict] = None,
        suggested_tools: Optional[List[Dict]] = None,
        date_granularity_alignment: Optional[Dict[str, Any]] = None,
        defer_union_weekly_rollup: bool = False,
    ) -> ExtractionPlan:
        """
        Generate a new plan after verification failure.
        
        Args:
            current_df: Current state of the DataFrame
            previous_tools: Tools that have been executed
            previous_issues: Issues found in previous iterations
            verification_result: Latest verification result
            iteration: Current iteration number
            max_iterations: Maximum iterations allowed
            target_template: Optional target schema template for enrichment-aware replanning
            
        Returns:
            New ExtractionPlan with a revised tool sequence
        """
        observer = get_observer()
        
        # Build context-rich prompt
        df_summary = self._generate_df_summary(current_df)
        tools_summary = self._summarize_tools(previous_tools)
        issues_summary = self._summarize_issues(previous_issues)
        
        # Build target template context if available
        template_context = ""
        if target_template:
            props = target_template.get("properties", {})
            mandatory = list(props.keys())
            enums = {k: v.get("enum") for k, v in props.items() if isinstance(v, dict) and v.get("enum")}
            date_cols = [k for k, v in props.items() if isinstance(v, dict) and v.get("format") == "date"]
            xs = target_template.get("x_scope") if isinstance(target_template.get("x_scope"), dict) else {}
            uid_h = xs.get("uid_hierarchy") or []
            target_granularity = str(effective_target_date_granularity(xs) or "").strip().lower()
            tpl_metrics = xs.get("metrics") or []
            tpl_agg = target_template.get("aggregation_logic") or xs.get("aggregation_logic") or {}

            granularity_guidance = ""
            if defer_union_weekly_rollup:
                granularity_guidance = (
                    "\n- **Multi-source union (weekly rollup deferred)**: This sheet is normalized **before** stack/union. "
                    "The template may target **weekly** grain for the **final combined** output — that rollup runs **after collation**, not necessarily in this tool list. "
                    "**Do not** insist on `transform.aggregate_weekly` or a `week_start` column here unless it already exists in the current data; keep the physical date column "
                    "(e.g. `date` / `calendar_date`) consistent with mappings."
                )
            elif target_granularity in ("weekly", "week"):
                rule_map: Dict[str, str] = {}
                if isinstance(tpl_agg, dict):
                    raw_rules = tpl_agg.get("metric_rules") or {}
                    if isinstance(raw_rules, dict):
                        for metric_name, rule in raw_rules.items():
                            rule_map[str(metric_name)] = str(rule or "sum").lower()
                for metric_name in tpl_metrics:
                    rule_map.setdefault(str(metric_name), "sum")
                rule_text = ", ".join(f"{m}={r}" for m, r in rule_map.items()) or "sum per template metric"
                granularity_guidance = (
                    "\n- **Target granularity is WEEKLY**: the final output MUST have one Monday-aligned weekly date column (`week_start` by default) rather than raw daily dates."
                    " If the date is split across parts (for example year/month/day or year/quarter), insert `transform.build_date_from_parts` first."
                    " If the current rows are monthly or quarterly totals, insert `transform.expand_period_to_daily` before weekly aggregation."
                    " If dimension columns are sparse because the source relies on merged cells or parent-row labels, insert `transform.fill_merged` before weekly aggregation."
                    f" If the current dataframe still has daily/date rows, insert `transform.aggregate_weekly` with metric_rules={{{rule_text}}}"
                    " (use `transform.date_range_to_weekly` instead if the source rows represent start/end date ranges)."
                )

            template_context = f"""
### Target Template
- **Required output columns**: {mandatory}
- **UID hierarchy**: {list(uid_h)}
- **Target date granularity**: {target_granularity or 'not specified'}
- **Template metrics**: {list(tpl_metrics)}
- **Enum constraints**: {json.dumps(enums, indent=2) if enums else 'None'}
- **Date format columns**: {date_cols} (must be YYYY-MM-DD)
- Use this to guide enrichment fixes: rename columns to match, map_values for enums, format for dates.{granularity_guidance}"""

        # Build suggested tools context from verifier
        suggested_tools_context = ""
        if suggested_tools:
            import json as _json
            suggested_tools_context = f"""
### Verifier Suggested Tools
The output verifier has suggested these specific tools to fix the issues:
```json
{_json.dumps(suggested_tools, indent=2)}
```
Consider using these suggestions OR propose a better alternative if you think they won't work."""

        user_prompt = f"""## Transformation Retry Analysis

### Current Status
- **Iteration**: {iteration} of {max_iterations}
- **Remaining attempts**: {max_iterations - iteration}

### Current DataFrame
{df_summary}

### Execution history (diagnosis)
{tools_summary}

### Issues Found (History)
{issues_summary}

### Latest Verification Result
{verification_result.summary if verification_result else "No verification result available"}
Issues: {verification_result.issues if verification_result else []}
{suggested_tools_context}
{template_context}
{_format_granularity_alignment_for_replan(date_granularity_alignment)}
### Your Task
1. Analyze WHY the previous attempts failed
2. Identify the ROOT CAUSE of the persistent issue
3. Build a fresh, complete tool_calls sequence from the original grid based on the current DataFrame, latest issues, and target template
4. Treat execution history only as failure evidence; do not copy, append to, or patch the previous tool sequence
5. Avoid retrying the exact same failing tool+params, but reuse tools freely when parameters or upstream data justify it
6. If we're on the last iteration, consider accepting partial success or escalating

Respond with JSON containing your analysis and new tool_calls."""

        try:
            if observer:
                with observer.trace("replanner") as t:
                    t.system_prompt = self.SYSTEM_PROMPT
                    t.user_prompt = user_prompt
                    t.full_prompt = f"{augment_system_prompt_with_catalog(self.SYSTEM_PROMPT)}\n\n{user_prompt}"
                    t.prompt_version = self.PROMPT_VERSION
                    t.input_context = {
                        "iteration": iteration,
                        "previous_tools_count": len(previous_tools),
                        "previous_issues_count": len(previous_issues)
                    }
                    
                    try:
                        logger.info(
                            "Replanner: requesting LLM (iteration %s, issues=%s)",
                            iteration,
                            len(previous_issues or []),
                        )
                        response = self.llm_wrapper.generate_content(t.full_prompt)
                        response_text = response.text
                        t.raw_response = response_text
                        
                        if hasattr(self.llm_client, 'model_name'):
                            t.model_id = self.llm_client.model_name.replace('models/', '')
                        
                        logger.info(f"Captured LLM response for replanner: {len(response_text)} chars")
                        
                        plan = self._parse_replan_response(
                            response_text,
                            previous_tools,
                            verification_result=verification_result,
                            target_template=target_template,
                        )
                        
                        # Add detail fields for observer UI
                        t.parsed_output = {
                            "tool_calls": plan.tool_calls,
                            "confidence": plan.confidence,
                            "reasoning": plan.reasoning
                        }
                        t.confidence_score = plan.confidence
                        
                        return plan
                    except Exception as e:
                        logger.error(f"Replanner unexpected error: {e}")
                        t.error_message = str(e)
                        t.success = False
                        if not t.raw_response:
                            t.raw_response = f"Unexpected Error: {e}"
                        return self._fallback_plan(verification_result, previous_tools)
            else:
                full_prompt = f"{augment_system_prompt_with_catalog(self.SYSTEM_PROMPT)}\n\n{user_prompt}"
                logger.info(
                    "Replanner: requesting LLM (iteration %s, issues=%s)",
                    iteration,
                    len(previous_issues or []),
                )
                response = self.llm_wrapper.generate_content(full_prompt)
                return self._parse_replan_response(
                    response.text,
                    previous_tools,
                    verification_result=verification_result,
                    target_template=target_template,
                )
                
        except Exception as e:
            logger.error(f"Replanning failed: {e}")
            return self._fallback_plan(verification_result, previous_tools)
    
    def _generate_df_summary(self, df) -> str:
        """Generate DataFrame summary for LLM."""
        if df is None or df.empty:
            return "DataFrame is empty or None"
        
        lines = []
        lines.append(f"Shape: {df.shape[0]} rows × {df.shape[1]} columns")
        lines.append(f"Columns: {list(df.columns)[:10]}{'...' if len(df.columns) > 10 else ''}")
        
        # Blank analysis
        blank_cols = []
        # Use iloc to safely handle duplicate column names
        for i in range(min(10, len(df.columns))):
            col_name = df.columns[i]
            col_data = df.iloc[:, i]
            blank_count = col_data.isna().sum() + (col_data == '').sum() if col_data.dtype == 'object' else col_data.isna().sum()
            if blank_count > 0:
                blank_cols.append(f"{col_name}: {blank_count}")
        if blank_cols:
            lines.append(f"Columns with blanks: {', '.join(blank_cols)}")
        
        return "\n".join(lines)
    
    def _summarize_tools(self, tools_history: List[Dict]) -> str:
        """Summarize tools that have been executed."""
        if not tools_history:
            return "No tools have been executed yet."
        
        lines = []
        for tool in tools_history[-10:]:  # Last 10 tools
            status = "✓" if tool.get("success") else "✗"
            tool_name = tool.get("tool", "unknown")
            message = tool.get("message", "")[:50]
            rows_change = f"{tool.get('rows_before', '?')} → {tool.get('rows_after', '?')} rows"
            lines.append(f"{status} {tool_name}: {message} ({rows_change})")
        
        return "\n".join(lines)
    
    def _summarize_issues(self, issues_history: List[Dict]) -> str:
        """Summarize issues found across iterations."""
        if not issues_history:
            return "No issues recorded."
        
        # Group by issue type
        issue_counts = {}
        for issue in issues_history:
            itype = issue.get("issue_type", "unknown")
            issue_counts[itype] = issue_counts.get(itype, 0) + 1
        
        lines = []
        for itype, count in sorted(issue_counts.items(), key=lambda x: -x[1]):
            indicator = "⚠️ REPEATED" if count > 1 else ""
            lines.append(f"- {itype}: {count} occurrence(s) {indicator}")
        
        return "\n".join(lines)
    
    def _parse_replan_response(
        self,
        response_text: str,
        previous_tools: List[Dict] = None,
        verification_result: Optional[VerificationResult] = None,
        target_template: Optional[Dict[str, Any]] = None,
    ) -> ExtractionPlan:
        """Parse a fresh, issue-driven replan into an ExtractionPlan."""
        data = {}
        try:
            # Use robust JSON parser
            data = robust_json_parse(response_text)
            
            # Ensure data is a dictionary
            if isinstance(data, str):
                 logger.warning(f"Replanner response parsed as string, attempting to fix: {data[:100]}...")
                 # Try to force parse if it looks like JSON but was returned as string
                 try:
                     data = json.loads(data)
                 except:
                     pass
            
            if not isinstance(data, dict):
                raise JSONParseError(f"Expected JSON object, got {type(data)}")

            if not isinstance(data, dict):
                raise JSONParseError(f"Expected JSON object, got {type(data)}")

            from .llm_handler import safe_get
            
            # Use safe_get for all field extractions
            tool_calls = safe_get(data, "tool_calls", list)
            tool_calls = _enrich_replanned_tools(tool_calls, target_template)

            strategy = safe_get(data, "strategy", str, "")
            if not strategy:
                strategy = safe_get(data, "new_strategy", str, "")
            recommendation = safe_get(data, "recommendation", str, "")
            if not recommendation:
                recommendation = (
                    strategy
                    if strategy in {"accept_partial", "escalate_to_human"}
                    else "proceed"
                )
            confidence = safe_get(data, "confidence", float, 0.5)
            analysis = safe_get(data, "analysis", str, "")
            if not analysis:
                diagnosis = data.get("diagnosis")
                if diagnosis:
                    analysis = json.dumps(diagnosis, ensure_ascii=False)
            
            # Template-owned parameters must exist before validation. Otherwise,
            # valid calls such as verify.schema are discarded as incomplete.
            validated_tools, validation_errors = validate_tool_sequence(tool_calls)
            valid_tools = [t for t in validated_tools if isinstance(t, dict) and t.get("_valid", True)]
            if valid_tools:
                valid_tools = sort_tool_calls_by_pipeline_stage(valid_tools)
                valid_tools, dedupe_notes = dedupe_redundant_tool_calls(valid_tools)
                if dedupe_notes:
                    logger.info(
                        "Replan: dropped %s redundant tool call(s): %s",
                        len(dedupe_notes),
                        "; ".join(dedupe_notes[:8]),
                    )
            if validation_errors:
                logger.warning(f"Replan tool validation errors: {validation_errors}")
            # region agent log
            _debug_log(
                "H9",
                "replanner validated tool sequence",
                {
                    "tool_calls_count": len(tool_calls or []),
                    "validated_count": len(validated_tools or []),
                    "valid_count": len(valid_tools),
                    "validation_errors": validation_errors[:5] if isinstance(validation_errors, list) else validation_errors,
                    "validated_tool_flags": [
                        None if not isinstance(t, dict) else t.get("_valid")
                        for t in (validated_tools or [])
                    ],
                },
            )
            # endregion
            
            # Check for escalation recommendation
            requires_review = recommendation in ["accept_partial", "escalate_to_human"]
            review_reason = ""
            if recommendation == "accept_partial":
                review_reason = "Replanner suggests accepting partial success"
            elif recommendation == "escalate_to_human":
                review_reason = "Replanner recommends human intervention"
            
            # Final confidence adjustment
            if confidence == 0 and tool_calls:
                confidence = 0.5

            return ExtractionPlan(
                blocks=[],
                tool_calls=valid_tools,
                column_mappings=[],
                expected_columns=[],
                confidence=confidence,
                reasoning=f"{analysis} {strategy}".strip(),
                requires_human_review=requires_review,
                review_reason=review_reason,
                raw_analysis=response_text
            )
            
        except JSONParseError as e:
            logger.error(f"Failed to parse replan response: {e}")
            return self._fallback_plan(verification_result, previous_tools)
        except Exception as e:
            logger.error(f"Unexpected error parsing replan: {e}")
            
            # CRITICAL FIX: Ensure reasoning concatenation never fails with TypeError (dict + str)
            # Use f-strings to safely coerce any non-string values to strings
            if isinstance(data, dict):
                from .llm_handler import safe_get
                
                # Extract fields safely using safe_get
                analysis = safe_get(data, "analysis", str, "")
                strategy = safe_get(data, "strategy", str, "")
                if not strategy:
                    strategy = safe_get(data, "new_strategy", str, "")
                recommendation = safe_get(data, "recommendation", str, "")
                if not recommendation:
                    recommendation = (
                        strategy
                        if strategy in {"accept_partial", "escalate_to_human"}
                        else "proceed"
                    )
                confidence = safe_get(data, "confidence", float, 0.5)
                tool_calls = _enrich_replanned_tools(
                    safe_get(data, "tool_calls", list, []),
                    target_template,
                )
                
                # Re-validate tool calls
                validated_tools, _ = validate_tool_sequence(tool_calls)
                valid_tools = [t for t in validated_tools if isinstance(t, dict) and t.get("_valid", True)]
                if valid_tools:
                    valid_tools = sort_tool_calls_by_pipeline_stage(valid_tools)
                    valid_tools, _ = dedupe_redundant_tool_calls(valid_tools)

                return ExtractionPlan(
                    blocks=[],
                    tool_calls=valid_tools,
                    column_mappings=[],
                    expected_columns=[],
                    confidence=confidence,
                    reasoning=f"{analysis} {strategy}".strip() or f"Error: {e}",
                    requires_human_review=recommendation in ["accept_partial", "escalate_to_human"],
                    review_reason=f"Hardened fallback after parsing error: {e}",
                    raw_analysis=response_text
                )

            return ExtractionPlan(
                blocks=[],
                tool_calls=[],
                column_mappings=[],
                expected_columns=[],
                confidence=0.0,
                reasoning=f"Error: {e}",
                requires_human_review=True,
                review_reason=str(e),
                raw_analysis=response_text
            )
    
    def _fallback_plan(
        self, 
        verification_result: Optional[VerificationResult],
        previous_tools: List[Dict]
    ) -> ExtractionPlan:
        """
        Fallback when replanning fails.
        Uses verification suggested tools if available.
        """
        tool_calls = []
        if verification_result and verification_result.suggested_tools:
            tool_calls = verification_result.suggested_tools
        
        return ExtractionPlan(
            blocks=[],
            tool_calls=tool_calls,
            column_mappings=[],
            expected_columns=[],
            confidence=0.0,
            reasoning="Replanning failed, using fallback suggestions",
            raw_analysis=""
        )
