"""
Output Verifier - LLM component that verifies if a DataFrame is properly flattened.
"""
import logging
import json
from typing import Dict, List, Any, Optional
import pandas as pd
from .base import VerificationResult, load_prompt_from_file
from .llm_handler import LLMCallWrapper, RetryConfig, robust_json_parse
from ..debug.llm_observer import get_observer

logger = logging.getLogger(__name__)

# Tools that actually change grain toward weekly output (planner obligation).
_WEEKLY_ROLLUP_TOOLS = frozenset(
    {
        "transform.aggregate_weekly",
        "transform.date_range_to_weekly",
        "transform.expand_period_to_daily",
        "transform.expand_date_range_to_daily",
        "transform.infer_granularity_expand_to_daily",
    }
)


def _output_df_indicates_weekly_cadence(output_df: Optional[pd.DataFrame]) -> bool:
    """
    If the output table's primary date-like column already shows ~weekly spacing
    (median gap ~7 days), treat the weekly contract as satisfied even when
    tools_history is incomplete (defense in depth).
    """
    if output_df is None or output_df.empty or not isinstance(output_df, pd.DataFrame):
        return False
    try:
        from sia.agent.date_column_inference import infer_date_cadence_fast
    except Exception:
        return False
    preferred_substrings = (
        "week_start",
        "calendar_date",
        "week_end",
        "period_start",
        "date",
    )
    ordered: List[Any] = []
    for c in output_df.columns:
        cl = str(c).lower()
        if any(s in cl for s in preferred_substrings):
            ordered.append(c)
    tail = [c for c in output_df.columns if c not in ordered]
    for col in ordered + tail:
        try:
            prof = infer_date_cadence_fast(output_df[col], str(col))
        except Exception:
            continue
        cad = str(prof.get("inferred_cadence") or "").strip().lower()
        conf = float(prof.get("confidence") or 0.0)
        if cad == "weekly" and conf >= 0.45:
            return True
    return False


def deterministic_weekly_contract_issues(
    date_granularity_alignment: Optional[Dict[str, Any]],
    tools_history: Optional[List[Dict[str, Any]]],
    output_df: Optional[pd.DataFrame] = None,
) -> List[Dict[str, Any]]:
    """
    If the template + Guided Setup contract says a weekly rollup is required but the
    executed chain never ran a rollup-capable tool, treat verification as failed.

    This complements the LLM verifier (prompt §4a) which often misses cadence when the
    table still looks structurally flat.
    """
    if not isinstance(date_granularity_alignment, dict):
        return []
    nt = str(date_granularity_alignment.get("normalized_target") or "").strip().lower()
    if nt != "weekly":
        return []
    if not date_granularity_alignment.get("requires_weekly_rollup_step"):
        return []
    ran = {h.get("tool") for h in (tools_history or []) if isinstance(h, dict)}
    if ran & _WEEKLY_ROLLUP_TOOLS:
        return []
    if _output_df_indicates_weekly_cadence(output_df):
        return []
    return [
        {
            "issue_type": "granularity_mismatch",
            "description": (
                "Target date_granularity is WEEKLY and Guided Setup implies a rollup is required "
                "(monthly/quarterly/daily/range → weekly), but tools_history shows no recognized "
                "weekly rollup / expand tools (e.g. transform.aggregate_weekly, "
                "transform.date_range_to_weekly, transform.expand_period_to_daily, "
                "transform.expand_date_range_to_daily, transform.infer_granularity_expand_to_daily) "
                "and the output date column cadence did not read as weekly. "
                "The pipeline may still be at source grain (e.g. one row per month)."
            ),
            "affected_columns": [],
            "severity": "high",
        }
    ]


class OutputVerifier:
    """
    LLM component that verifies if a DataFrame is properly flattened.
    Suggests additional transformations if needed.
    
    Enhanced with retry logic and context-rich verification.
    """
    
    def __init__(self, llm_client, retry_config: RetryConfig = None):
        # Wrap LLM client with retry logic
        self.llm_wrapper = LLMCallWrapper(
            llm_client,
            retry_config=retry_config or RetryConfig(max_attempts=3)
        )
        self.llm_client = llm_client  # Backward compat
        
        self.SYSTEM_PROMPT, self.PROMPT_VERSION = load_prompt_from_file("output_verifier")
        if not self.SYSTEM_PROMPT:
            self.SYSTEM_PROMPT = "You are a data quality validator checking if a table is properly flattened."
            self.PROMPT_VERSION = "0.0"
    
    def verify(
        self, 
        df, 
        column_info: List[Dict] = None,
        expected_columns: List[str] = None,
        tools_history: List[Dict] = None,
        issues_history: List[Dict] = None,
        business_rules: List[Dict] = None,
        planned_rule_actions: List[Dict] = None,
        iteration: int = 1,
        max_iterations: int = 3,
        date_granularity_alignment: Optional[Dict[str, Any]] = None,
        target_date_granularity: Optional[str] = None,
        *,
        skip_weekly_grain_contract: bool = False,
        sparse_dimension_columns: Optional[List[str]] = None,
    ) -> VerificationResult:
        """
        Verify if DataFrame is a proper flat table.
        
        Args:
            df: pandas DataFrame to verify
            column_info: Optional column metadata
            expected_columns: Expected columns from plan
            tools_history: Tools that were executed
            issues_history: Issues found in previous iterations
            business_rules: Active business rules for the run
            planned_rule_actions: Business rule actions surfaced by the planner
            iteration: Current iteration number
            max_iterations: Maximum iterations
            skip_weekly_grain_contract: When True, skip weekly template/deterministic checks (multi-source
                per-source phase where weekly rollup runs after union/collation).
            sparse_dimension_columns: Source columns known to be sparse (merged-cell layouts); used with
                skip_weekly_grain_contract to avoid false unfilled_parent failures.
        
        Returns:
            VerificationResult with is_flat status and any issues
        """
        
        observer = get_observer()
        
        # Generate DataFrame summary for LLM
        df_summary = self._generate_df_summary(df)
        
        # Build context-rich prompt with history
        context_parts = []
        
        if iteration > 1:
            context_parts.append(f"## Verification Context")
            context_parts.append(f"- **Iteration**: {iteration} of {max_iterations}")
            context_parts.append(f"- **Previous iterations have been attempted**")

        if skip_weekly_grain_contract:
            context_parts.append(
                "\n## Date granularity (scope — multi-source per-source)\n"
                "- This frame is normalized **per workbook** before stack/union with other sources.\n"
                "- If the job template targets **weekly** output, that contract is enforced on the **combined** "
                "dataset after collation (rollup may run there).\n"
                "- **Do not** emit `granularity_mismatch` solely because this frame still has daily / `date` / "
                "`calendar_date` rows.\n"
            )
            sparse_list = [
                str(c).strip()
                for c in (sparse_dimension_columns or [])
                if c is not None and str(c).strip()
            ]
            if sparse_list:
                context_parts.append(
                    "\n## Sparse dimensions (merged-cell layout — per-source)\n"
                    f"- Known sparse dimension columns: {', '.join(sparse_list[:12])}\n"
                    "- Blank cells in these columns **before** `transform.expand_grouped_block` or "
                    "`transform.fill_merged` are expected; do **not** emit high-severity `unfilled_parent` "
                    "for every blank row.\n"
                    "- If those layout tools are **absent** from Recently Executed Tools, emit one "
                    "`missing_layout_cleanup` issue (high) suggesting `expand_grouped_block` or "
                    "dimension-only `fill_merged` — **not** `aggregate_weekly`.\n"
                    "- If those tools **ran**, only flag `unfilled_parent` when blanks remain on rows "
                    "that still carry numeric metric values.\n"
                )

        prompt_alignment = None if skip_weekly_grain_contract else date_granularity_alignment
        prompt_target = None if skip_weekly_grain_contract else target_date_granularity
        
        # NOTE: expected_columns removed from verifier context.
        # The plan generator often hallucinates expected column names that don't match
        # the actual data (e.g., predicting "Metric", "Value" when data has "Impression", "Spend").
        # Column name matching is not a valid criterion for flatness verification.
        # The verifier should only check structural properties, not schema alignment.
        

        if tools_history:
            tail = 20 if (prompt_alignment or prompt_target) else 5
            recent_tools = tools_history[-tail:]
            context_parts.append(f"\n## Recently Executed Tools (last {len(recent_tools)})")
            for tool in recent_tools:
                status = "✓" if tool.get("success") else "✗"
                context_parts.append(f"- {status} {tool.get('tool')}: {tool.get('message', '')}")

        if prompt_target or prompt_alignment:
            context_parts.append("\n## Date granularity contract (mandatory)")
            if prompt_target:
                context_parts.append(
                    f"- **Target template x_scope date_granularity**: `{prompt_target}`"
                )
            if isinstance(prompt_alignment, dict) and prompt_alignment:
                slim = {
                    k: prompt_alignment.get(k)
                    for k in (
                        "source_date_granularity",
                        "target_date_granularity",
                        "normalized_source",
                        "normalized_target",
                        "requires_weekly_rollup_step",
                        "recommended_primary_tool",
                        "planner_obligation",
                    )
                    if prompt_alignment.get(k) not in (None, "", [], {})
                }
                context_parts.append(f"- **Alignment (Guided Setup vs template)**:\n{json.dumps(slim, indent=2)}")
            context_parts.append(
                "\nIf normalized_target is `weekly` and dates still look like **one row per calendar month** "
                "(or any cadence coarser than a week), you MUST emit `granularity_mismatch`, set `is_flat` to "
                "false, and suggest the appropriate rollup tools from the system prompt."
            )
        
        if issues_history:
            # Group issues to detect repeats
            issue_types = {}
            for issue in issues_history:
                itype = issue.get("issue_type", "unknown")
                issue_types[itype] = issue_types.get(itype, 0) + 1
            
            context_parts.append(f"\n## Issues Found in Previous Iterations")
            for itype, count in issue_types.items():
                if count > 1:
                    context_parts.append(f"- ⚠️ **{itype}**: Found {count} times (REPEATED)")
                else:
                    context_parts.append(f"- {itype}")
            context_parts.append("\n**Important**: If the same issue appears multiple times, the previous fix did not work. Suggest a DIFFERENT approach.")

        if business_rules:
            context_parts.append(f"\n## Active Business Rules")
            for rule in business_rules[:10]:
                context_parts.append(
                    f"- {rule.get('target_column')}: {rule.get('rule_type')} -> {rule.get('rule_expression')}"
                )

        if planned_rule_actions:
            context_parts.append(f"\n## Planned Rule Actions")
            for action in planned_rule_actions[:10]:
                context_parts.append(f"- {json.dumps(action)}")
        
        context_section = "\n".join(context_parts) if context_parts else ""

        weekly_check = ""
        tgt_w = str(prompt_target or "").strip().lower()
        nt_w = ""
        if isinstance(prompt_alignment, dict):
            nt_w = str(prompt_alignment.get("normalized_target") or "").strip().lower()
        if not skip_weekly_grain_contract and (tgt_w in ("weekly", "week") or nt_w == "weekly"):
            weekly_check = """
6. **Weekly template contract**: If the alignment block shows `normalized_target: weekly` and dates are still **one row per calendar month or quarter** (or any cadence clearly coarser than a week), set `is_flat` to false and add `granularity_mismatch` (high). A Monday-per-month pattern is NOT weekly output.
"""

        user_prompt = f"""Verify if this DataFrame is a proper flat table:

{context_section}

## DataFrame Summary
{df_summary}

## Column Info
{json.dumps(column_info or [], indent=2)}

## Check for:
1. Blank cells in dimension columns (should be filled)
2. Proper metric labeling (numeric values should be associated with a metric name/indicator)
   - Note: Columns do NOT need to be named exactly "Metric" or "Value".
   - Accept names like "Indicator", "Measure", "Amount", "Data", etc.
3. One observation per row
4. No hierarchical structure remaining
5. Active business rules appear satisfied, especially formatting and blank/default handling
{weekly_check}
Respond with JSON indicating if the table is flat and any issues found."""

        try:
            if observer:
                with observer.trace("output_verifier") as t:
                    t.system_prompt = self.SYSTEM_PROMPT
                    t.user_prompt = user_prompt
                    t.full_prompt = f"{self.SYSTEM_PROMPT}\n\n{user_prompt}"
                    t.prompt_version = self.PROMPT_VERSION
                    t.input_context = {
                        "iteration": iteration,
                        "tools_executed": len(tools_history or []),
                        "previous_issues": len(issues_history or [])
                    }
                    
                    try:
                        # Use wrapper with retry logic
                        response = self.llm_wrapper.generate_content(t.full_prompt)
                        response_text = response.text
                        t.raw_response = response_text
                        
                        if hasattr(self.llm_client, 'model_name'):
                             t.model_id = self.llm_client.model_name.replace('models/', '')
                             
                        logger.info(f"Captured LLM response for output_verifier: {len(response_text)} chars")
                    except Exception as e:
                        logger.error(f"OutputVerifier unexpected error: {e}")
                        t.error_message = str(e)
                        t.success = False
                        if not t.raw_response:
                            t.raw_response = f"Unexpected Error: {e}"
                        raise e
                    
                    parsed = self._parse_verification_response(response_text)
                    t.confidence_score = parsed.confidence
                    merged = self._merge_weekly_contract(
                        parsed,
                        date_granularity_alignment,
                        tools_history,
                        df,
                        skip_weekly_grain_contract=skip_weekly_grain_contract,
                    )
                    # Debug UI shows raw_response (model JSON). LangGraph uses merged + later
                    # business-rule overrides — surface both so Trace tab matches routing.
                    weekly_extra = len(merged.issues or []) - len(parsed.issues or [])
                    t.parsed_output = {
                        "model_is_flat": bool(parsed.is_flat),
                        "after_weekly_contract_merge_is_flat": bool(merged.is_flat),
                        "weekly_contract_issues_added": max(0, weekly_extra),
                        "model_issue_count": len(parsed.issues or []),
                        "merged_issue_count": len(merged.issues or []),
                    }
                    # Sanitize zero confidence on merged result
                    if merged.confidence == 0 and merged.summary:
                        t.confidence_score = 0.7
                        merged.confidence = 0.7
                        t.parsed_output["confidence_adjusted"] = True
                    else:
                        t.confidence_score = float(merged.confidence or 0.0)
                    return merged
            else:
                response = self.llm_wrapper.generate_content(
                    f"{self.SYSTEM_PROMPT}\n\n{user_prompt}"
                )
                parsed = self._parse_verification_response(response.text)
                return self._merge_weekly_contract(
                    parsed,
                    date_granularity_alignment,
                    tools_history,
                    df,
                    skip_weekly_grain_contract=skip_weekly_grain_contract,
                )
                
        except Exception as e:
            logger.error(f"Output verification failed: {e}")
            return VerificationResult(
                is_flat=False,
                confidence=0.0,
                issues=[{"issue_type": "verification_error", "description": str(e)}],
                rule_issues=[],
                summary=f"Verification failed: {e}"
            )

    def _merge_weekly_contract(
        self,
        result: VerificationResult,
        date_granularity_alignment: Optional[Dict[str, Any]],
        tools_history: Optional[List[Dict[str, Any]]],
        output_df: Optional[pd.DataFrame] = None,
        *,
        skip_weekly_grain_contract: bool = False,
    ) -> VerificationResult:
        if skip_weekly_grain_contract:
            return result
        extras = deterministic_weekly_contract_issues(
            date_granularity_alignment, tools_history, output_df
        )
        if not extras:
            return result
        issues = list(result.issues or []) + extras
        sug = list(result.suggested_tools or [])
        if not sug:
            sug = [
                {
                    "tool": "transform.expand_period_to_daily",
                    "params": {},
                    "reason": "Use when each row is a month/quarter total and the template requires weekly output.",
                },
                {
                    "tool": "transform.aggregate_weekly",
                    "params": {},
                    "reason": "Aggregate row-level dates into Monday-aligned weeks (see template metric_rules).",
                },
            ]
        note = " Deterministic check: template/Guided Setup required a weekly rollup but no rollup tool ran."
        return VerificationResult(
            is_flat=False,
            confidence=min(result.confidence or 1.0, 0.55),
            issues=issues,
            rule_issues=list(result.rule_issues or []),
            suggested_tools=sug,
            summary=(result.summary or "").strip() + note,
        )

    def _generate_df_summary(self, df) -> str:
        """Generate a summary of the DataFrame for LLM verification."""
        
        lines = []
        lines.append(f"Shape: {df.shape[0]} rows × {df.shape[1]} columns")
        lines.append(f"\nColumns: {list(df.columns)[:15]}{'...' if len(df.columns) > 15 else ''}")
        
        # Check for blanks in each column
        lines.append("\nBlank cells per column:")
        # Use iloc to safely handle duplicate column names (which return a DataFrame on df[col], causing ambiguity)
        for i in range(min(10, len(df.columns))):
            col_name = df.columns[i]
            col_data = df.iloc[:, i]
            blank_count = col_data.isna().sum() + (col_data == '').sum()
            if blank_count > 0:
                lines.append(f"  {col_name}: {blank_count} blanks ({blank_count/len(df)*100:.1f}%)")
        
        # Sample rows
        lines.append(f"\nFirst 5 rows:")
        lines.append(df.head(5).to_string())
        
        # Sample of unique values in first few columns
        lines.append("\nUnique values in first columns:")
        # Use iloc to safely handle duplicate column names
        for i in range(min(5, len(df.columns))):
            col_name = df.columns[i]
            unique = df.iloc[:, i].dropna().unique()[:5]
            lines.append(f"  {col_name}: {list(unique)}")
        
        return "\n".join(lines)
    
    def _parse_verification_response(self, response_text: str) -> VerificationResult:
        """Parse LLM response into VerificationResult."""
        try:
            # Use robust JSON parser
            result = robust_json_parse(response_text)
            
            # Handle list response (LLM returned list of issues directly)
            if isinstance(result, list):
                logger.warning("Verifier returned list directly. Assuming list of issues.")
                result = {
                    "is_flat": len(result) == 0, # If empty list, assumed flat
                    "issues": result,
                    "confidence": 0.5,
                    "summary": "Parsed from direct list of issues"
                }
            
            from .llm_handler import safe_get
            from .base import robust_bool
            
            # Use object type to get the raw value (is_flat can be bool or str like "true")
            # Then use robust_bool to convert it
            is_flat_raw = safe_get(result, "is_flat", object) or safe_get(result, "flat", object, False)
            is_flat_bool = robust_bool(is_flat_raw)

                
            return VerificationResult(
                is_flat=is_flat_bool,
                confidence=safe_get(result, "confidence", float) or safe_get(result, "score", float, 0.0),
                issues=safe_get(result, "issues", list, []),
                rule_issues=safe_get(result, "rule_issues", list, []),
                suggested_tools=safe_get(result, "suggested_tools", list, []),
                summary=safe_get(result, "summary", str, "")
            )
        except Exception as e:
            logger.warning(f"Failed to parse verification response: {e}")
            return VerificationResult(
                is_flat=False,
                confidence=0.0,
                issues=[{"issue_type": "parse_error", "description": str(e)}],
                rule_issues=[],
                summary=f"Failed to parse: {e}"
            )
