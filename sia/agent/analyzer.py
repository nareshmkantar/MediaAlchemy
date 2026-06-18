"""
Structure Analyzer - LLM component that analyzes grid structure.
"""
import logging
import json
import time
from typing import Dict, Any, List, Optional
from .base import load_prompt_from_file
from .llm_handler import LLMCallWrapper, RetryConfig, robust_json_parse, JSONParseError
from ..debug.llm_observer import get_observer

logger = logging.getLogger(__name__)

class StructureAnalyzer:
    """
    LLM component that analyzes grid structure.
    Does NOT extract data - only reasons about structure.
    
    Enhanced with retry logic and robust parsing.
    """
    
    def __init__(self, llm_client, retry_config: RetryConfig = None):
        # Wrap LLM client with retry logic
        self.llm_wrapper = LLMCallWrapper(
            llm_client,
            retry_config=retry_config or RetryConfig(max_attempts=3)
        )
        self.llm_client = llm_client  # Keep reference for backward compatibility
        
        # Load prompt from external file
        self.SYSTEM_PROMPT, self.PROMPT_VERSION = load_prompt_from_file("structure_analyzer")
        if not self.SYSTEM_PROMPT:
            # Fallback to minimal prompt
            self.SYSTEM_PROMPT = "You are an expert data engineer analyzing spreadsheet structures."
            self.PROMPT_VERSION = "0.0"
    
    def analyze(self, grid_summary: str, visual_patterns: Dict = None) -> Dict:
        """
        Analyze grid structure and return structured analysis.
        
        Args:
            grid_summary: Text representation of the grid
            visual_patterns: Detected visual patterns (merged cells, etc.)
        
        Returns:
            Structured analysis with identified tables, headers, patterns
        """
        observer = get_observer()
        
        user_prompt = f"""Analyze this spreadsheet grid:

```
{grid_summary}
```

Visual Patterns Detected:
{json.dumps(visual_patterns or {}, indent=2)}

Based on the grid summary above:
1. Identify ALL distinct data blocks by looking for anchor rows (marked with [A])
2. Determine the coordinates for each block (header_row, data_start_row, data_end_row, col_start, col_end)
3. Identify ANY blank or mostly empty columns (especially those marked as 'EMPTY' in the statistics)
4. Classify each block's shape (flat or crosstab)
5. Identify hierarchy parents and exclusion rows
6. **Detect grouped-row layouts**: If `metric_layout_signals` or the grid show metrics (Spend/Budget/Cost) populated on parent rows only with blanks on sibling rows, set `hierarchy.type` to `grouped_rows` and flag `block_metric_allocation` in `uncertainty`

IMPORTANT: The grid summary intentionally includes only a **sample** of rows (header, subsampled
“anchor” rows where column 0 is non-empty, and a few following sample rows). It does not list
every sheet row. Each included anchor marked [A] may start a distinct block.

    Respond with JSON following this order:
1. `tables` - List of identified blocks (include `hierarchy` when grouped_rows / block metrics apply)
2. `column_analysis` - Analysis of columns (include `is_blank`: true for empty columns; note block-sparse metrics)
3. `overall_structure` - Classification
4. `confidence` - (0.0 to 1.0)

Respond with JSON only, no markdown formatting."""

        if observer:
            with observer.trace("structure_analyzer") as t:
                t.system_prompt = self.SYSTEM_PROMPT
                t.user_prompt = user_prompt
                t.full_prompt = f"{self.SYSTEM_PROMPT}\n\n{user_prompt}"
                t.prompt_version = self.PROMPT_VERSION
                t.input_context = {
                    "grid_summary_length": len(grid_summary),
                    "has_visual_patterns": bool(visual_patterns)
                }
                
                try:
                    t0 = time.monotonic()
                    # Configuration-driven generation config
                    # LLMCallWrapper will handle normalization between OpenAI/Gemini
                    # IMPORTANT: Always ensure sufficient max_output_tokens for complex responses
                    gen_config = {"max_output_tokens": 8192, "temperature": 0.0}  # Increased default
                    
                    # MERGE with client config if available (don't replace!)
                    if hasattr(self.llm_client, 'generation_config') and self.llm_client.generation_config:
                        client_config = self.llm_client.generation_config
                        if isinstance(client_config, dict):
                            # Only override if client specifies a HIGHER limit
                            if client_config.get('max_output_tokens', 0) > gen_config['max_output_tokens']:
                                gen_config['max_output_tokens'] = client_config['max_output_tokens']
                            if 'temperature' in client_config:
                                gen_config['temperature'] = client_config['temperature']
                    full_len = len(t.full_prompt)
                    model_hint = getattr(self.llm_client, "model_name", None) or getattr(self.llm_client, "model", None) or "unknown"
                    logger.info(
                        "[structure_analyzer] LLM request starting model=%s prompt_chars=%s grid_summary_chars=%s",
                        model_hint,
                        full_len,
                        len(grid_summary),
                    )
                    response = self.llm_wrapper.generate_content(t.full_prompt, generation_config=gen_config)
                    elapsed = time.monotonic() - t0
                    response_text = response.text
                    t.raw_response = response_text
                    
                    # Try to capture model ID
                    if hasattr(self.llm_client, 'model_name'):
                         t.model_id = self.llm_client.model_name.replace('models/', '')
                    
                    logger.info(
                        "[structure_analyzer] LLM request finished in %.2fs response_chars=%s",
                        elapsed,
                        len(response_text or ""),
                    )
                except Exception as e:
                    logger.error(
                        "[structure_analyzer] LLM request failed after %.2fs: %s",
                        time.monotonic() - t0,
                        e,
                    )
                    logger.error(f"StructureAnalyzer unexpected error: {e}")
                    t.error_message = str(e)
                    t.success = False
                    if not t.raw_response:
                        t.raw_response = f"Unexpected Error: {e}"
                    response_text = ""
                
                analysis = self._parse_analysis(response_text)
                t.parsed_output = analysis
                
                from .llm_handler import safe_get
                
                # Robust confidence extraction
                conf = safe_get(analysis, "confidence", float) or \
                       safe_get(analysis, "overall_confidence", float, 0.0)
                       
                if conf == 0 and analysis.get("tables"):
                    conf = 0.8  # If we found tables, we have some confidence
                t.confidence_score = conf
                
                return analysis
        else:
            # No observer, just run with retry wrapper
            full_prompt = f"{self.SYSTEM_PROMPT}\n\n{user_prompt}"
            model_hint = getattr(self.llm_client, "model_name", None) or getattr(self.llm_client, "model", None) or "unknown"
            logger.info(
                "[structure_analyzer] LLM request starting (no observer) model=%s prompt_chars=%s",
                model_hint,
                len(full_prompt),
            )
            t0 = time.monotonic()
            response = self.llm_wrapper.generate_content(full_prompt)
            logger.info(
                "[structure_analyzer] LLM request finished in %.2fs response_chars=%s",
                time.monotonic() - t0,
                len(response.text or ""),
            )
            return self._parse_analysis(response.text)
    
    def _parse_analysis(self, response_text: str) -> Dict:
        """Parse LLM response into structured analysis using robust parser."""
        logger.info(f"Parsing LLM response ({len(response_text)} chars)")
        
        try:
            # Use robust JSON parser
            result = robust_json_parse(response_text)
            
            # Handle list response (LLM returned list of tables directly)
            if isinstance(result, list):
                logger.warning("Analyzer returned a list directly (not JSON object). wrapping it as 'tables'.")
                result = {
                    "tables": result,
                    "column_analysis": [],
                    "overall_structure": "inferred_from_list",
                    "confidence": 0.5
                }
                
            # Log what we got
            logger.info(f"Parsed JSON keys: {list(result.keys())}")
            
            # Check for tables under various possible key names
            tables_keys = ['tables', 'data_blocks', 'blocks', 'regions', 'data_regions']
            for key in tables_keys:
                if key in result and result[key]:
                    if key != 'tables':
                        logger.info(f"Found tables under key '{key}', normalizing to 'tables'")
                        result['tables'] = result[key]
                    break
            
            # If still no tables, check if there's column_analysis that implies a table
            if not result.get('tables') and result.get('column_analysis'):
                logger.warning("No tables array but found column_analysis, creating implicit table")
                result['tables'] = [{
                    "start_row": 0,
                    "end_row": 1000,
                    "start_col": 0,
                    "end_col": len(result.get('column_analysis', [])) - 1,
                    "header_row": 0,
                    "description": "Implicit table from column analysis",
                    "detection_reason": "LLM provided column_analysis but no explicit table bounds"
                }]
            
            logger.info(f"Parsed successfully: {len(result.get('tables', []))} tables found")
            return result
            
        except JSONParseError as e:
            logger.error(f"JSON parse error after all strategies: {e}")
            logger.error(f"Attempted to parse: {response_text[:300]}...")
        
        # Return fallback with raw response for debugging
        return {
            "tables": [],
            "column_analysis": [],
            "overall_structure": "Parse Error",
            "confidence": 0.0,
            "raw": response_text,
            "parse_error": True
        }
