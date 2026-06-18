"""
Structure Inference Agent Orchestrator - Main agent class.
Coordinates all modules in a DAG pipeline with HITL routing.
"""
import logging
import uuid
import json
import contextlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

import pandas as pd
import yaml

try:
    import google.generativeai as genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False



from sia.models.cell import VisualGrid, DataBlock
from sia.models.schema import InferredSchema, ColumnSchema
from sia.models.confidence import ProcessingTrace, HITLDecision
from sia.modules.visual_normalizer import VisualNormalizer
from sia.agent.target_template_utils import normalize_target_template
from sia.utils.llm_clients import OpenAILLMWrapper, AzureOpenAILLMWrapper, OPENAI_AVAILABLE
from sia.debug.llm_observer import init_observer, get_observer, clear_observer

logger = logging.getLogger(__name__)





class StructureInferenceAgent:
    """
    Main orchestrator for the Structure Inference Agent.
    Coordinates the pipeline: Load -> Locate -> Classify -> Normalize -> Merge
    """
    
    def __init__(
        self,
        config_path: str = None,
        api_key: str = None,
        model_name: str = None,
        debug_enabled: bool = True,
        enable_llm_judge: bool = True,
        **kwargs,
    ):
        """
        Initialize the agent with configuration.
        
        Args:
            config_path: Path to semantic_config.yaml
            api_key: LLM API key
            model_name: Optional model name override
            debug_enabled: Whether to capture detailed debug events
            **kwargs: Additional config values (azure_endpoint, etc.)
        """
        import os
        
        # Load config first to check for keys
        self.config = self._load_config(config_path)
        # Merge kwargs into config if they are model-related
        for k, v in kwargs.items():
            if k.startswith('azure_'):
                self.config[k] = v

        # RESOLVE API KEY PRIORITY:
        # 1. Key passed as argument (CLI --api-key)
        # 2. Environment variables / .env (recommended for local dev and CI)
        # 3. Non-secret settings from config/user_config.json (api_key never loaded from disk)
        
        config_key = self.config.get('api_key')
        if config_key:
            self.api_key = config_key
            print(f"[AGENT_INIT] Using API key from config", flush=True)
        else:
            self.api_key = api_key
            print(f"[AGENT_INIT] Using passed API key argument", flush=True)

        print(f"\n[AGENT_INIT] api_key present: {bool(self.api_key)}, env keys: AZURE={bool(os.environ.get('AZURE_OPENAI_API_KEY'))}", flush=True)
        if not self.api_key:
             # Try environment variables if key is still None
             self.api_key = os.environ.get('AZURE_OPENAI_API_KEY') or \
                            os.environ.get('OPENAI_API_KEY') or \
                            os.environ.get('GEMINI_API_KEY')
        
        print(f"[AGENT_INIT] Final Resolved api_key: {bool(self.api_key)}", flush=True)
        self.llm_client = None
        self.debug_enabled = debug_enabled
        self.enable_llm_judge = bool(enable_llm_judge)

        # Initialize LLM client
        # Prioritize model_name arg -> config 'llm_model' -> config 'llm_config.model' -> default
        self.model_name = model_name or \
                          self.config.get('llm_model') or \
                          self.config.get('llm_config', {}).get('model', 'gemini-3-flash-preview')
        
        model_to_use = self.model_name
        
        # Check Azure environment variables if missing in config
        if model_to_use and model_to_use.startswith('azure-'):
            if not self.config.get('azure_endpoint'):
                self.config['azure_endpoint'] = os.environ.get('AZURE_OPENAI_ENDPOINT')
            if not self.config.get('azure_api_version'):
                self.config['azure_api_version'] = os.environ.get('AZURE_OPENAI_API_VERSION')
            if not self.config.get('azure_deployment'):
                self.config['azure_deployment'] = os.environ.get('AZURE_OPENAI_DEPLOYMENT')
        
        # Note: These logs appear in debug_output.txt
        logger.info(f"LLM INIT - api_key provided/found: {bool(self.api_key)}")
        logger.info(f"LLM INIT - model_to_use: {model_to_use}")
        logger.info(f"LLM INIT - GENAI_AVAILABLE: {GENAI_AVAILABLE}")
        
        # LLM Initialization
        if model_to_use and model_to_use.startswith('azure-'):
            # Azure OpenAI support
            if self.api_key and OPENAI_AVAILABLE:
                try:
                    deployment = self.config.get('azure_deployment') or model_to_use.replace('azure-', '')
                    endpoint = self.config.get('azure_endpoint', '').strip().rstrip('/')
                    if '/openai/' in endpoint:
                        endpoint = endpoint.split('/openai/')[0]
                    version = self.config.get('azure_api_version', '2024-02-15-preview')
                    verify_ssl = self.config.get('azure_ssl_verify', True)
                    
                    self.llm_client = AzureOpenAILLMWrapper(self.api_key, deployment, endpoint, version, verify_ssl=verify_ssl)
                    logger.info(f"LLM INIT - SUCCESS: Azure OpenAI model initialized: {deployment} (ssl_verify={verify_ssl})")
                except Exception as e:
                    logger.exception("Azure OpenAI init", e)
        elif model_to_use and model_to_use.startswith('gpt-'):
            # OpenAI model support (gpt-4o, gpt-4-turbo, etc.)
            if self.api_key and OPENAI_AVAILABLE:
                try:
                    self.llm_client = OpenAILLMWrapper(self.api_key, model_to_use)
                    logger.info(f"LLM INIT - SUCCESS: OpenAI model initialized: {model_to_use}")
                except Exception as e:
                    logger.exception("OpenAI init", e)
        elif self.api_key and GENAI_AVAILABLE:
            try:
                genai.configure(api_key=self.api_key)
                
                # Use the model name as provided in config
                model_name = model_to_use
                
                # Disable safety filters for data analysis
                safety_settings = [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ]
                
                # Pull settings from config or set saner defaults
                llm_cfg = self.config.get('llm_config', {})
                generation_config = {
                    "temperature": llm_cfg.get('temperature', 0.0),
                    "max_output_tokens": llm_cfg.get('max_tokens', 4096)
                }
                
                self.llm_client = genai.GenerativeModel(
                    model_name,
                    generation_config=generation_config
                )
                logger.info(f"LLM INIT - SUCCESS: Gemini model initialized: {model_name} (temp={generation_config['temperature']})")
            except Exception as e:
                logger.error(f"LLM INIT - FAILED: Gemini model initialization: {e}")
                self.llm_client = None
        else:
            logger.warning(f"LLM INIT - FAILED: No valid model/key combination found for {model_to_use}")
            self.llm_client = None

        if model_to_use and not self.llm_client and model_to_use != 'rule-based':
             msg = f"Failed to initialize LLM client for model '{model_to_use}'. "
             if not self.api_key:
                 msg += "API Key is missing."
             elif model_to_use.startswith('azure-') and not self.config.get('azure_endpoint'):
                 msg += "Azure Endpoint is missing."
             else:
                 msg += "Check logs for details."
             logger.error(msg)
             # We store the error to be used in process_file
             self.init_error = msg
        else:
             self.init_error = None
             logger.info(f"LLM INIT - SKIPPED: api_key={bool(api_key)}, GENAI_AVAILABLE={GENAI_AVAILABLE}")
        
        # Initialize core modules
        self.visual_normalizer = VisualNormalizer()
        logger.info("Core modules initialized")
        
        # Initialize Judge for Evaluation (optional; finalize_node also checks enable_llm_judge)
        try:
            from sia.agent.judge import LLMJudge
            if self.llm_client and self.enable_llm_judge:
                self.judge = LLMJudge(self.llm_client)
                logger.info("LLM Judge initialized")
            else:
                self.judge = None
                if not self.enable_llm_judge:
                    logger.info("LLM Judge disabled (enable_llm_judge=False)")
                else:
                    logger.warning("LLM Judge omitted (no LLM client)")
        except Exception as e:
            logger.error(f"Failed to init Judge: {e}")
            self.judge = None
        
        # Confidence thresholds
        thresholds = self.config.get('confidence_thresholds', {})
        self.auto_approve_threshold = thresholds.get('auto_approve', 0.9)
        self.review_threshold = thresholds.get('flag_for_review', 0.6)
    
    def _load_config(self, config_path: str = None) -> Dict[str, Any]:
        """Load configuration from YAML file."""
        if config_path and Path(config_path).exists():
            try:
                with open(config_path, 'r') as f:
                    return yaml.safe_load(f)
            except Exception as e:
                logger.warning(f"Failed to load config: {e}")
        return {}
    
    def _plan_to_blocks(self, grid, extraction_plan) -> List:
        """Convert LLM extraction plan to DataBlock objects for tool execution."""
        from sia.models.cell import DataBlock
        
        blocks = []
        for block_spec in extraction_plan.blocks:
            try:
                block = DataBlock(
                    start_row=block_spec.get('start_row', 0),
                    end_row=block_spec.get('end_row', grid.total_rows - 1),
                    start_col=block_spec.get('start_col', 0),
                    end_col=block_spec.get('end_col', grid.total_cols - 1),
                    header_row=block_spec.get('header_row', 0),
                    confidence=block_spec.get('confidence', 0.8)
                )
                blocks.append(block)
            except Exception as e:
                logger.warning(f"Failed to create block from plan: {e}")
        return blocks
    
    def _get_data_preview(self, data) -> Dict:
        """Get a preview of data for Tool Inspector debugging."""
        import re
        
        def format_date_str(s):
            """Format datetime string to date only (YYYY-MM-DD)."""
            s = str(s)
            # Match datetime patterns and strip time portion
            if re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}', s):
                return s[:10]
            return s
        
        try:
            if isinstance(data, pd.DataFrame):
                # Convert sample to JSON-safe format (replace NaN with None)
                sample = []
                if len(data) > 0:
                    sample_df = data.head(30).copy()  # 30 rows for preview
                    # Get columns in original order
                    columns_ordered = [format_date_str(str(c)[:30]) for c in sample_df.columns]
                    # Replace NaN with None for JSON compatibility
                    sample_df = sample_df.where(pd.notnull(sample_df), None)
                    
                    # Convert to records maintaining column order
                    for _, row in sample_df.iterrows():
                        safe_record = {}
                        for orig_col, col in zip(sample_df.columns, columns_ordered):
                            v = row[orig_col]
                            # Convert any remaining non-serializable values
                            if pd.isna(v) if hasattr(pd, 'isna') else (v != v):
                                safe_record[col] = None
                            elif isinstance(v, (int, float, str, bool, type(None))):
                                # Format dates in values too
                                if isinstance(v, str):
                                    safe_record[col] = format_date_str(v)
                                else:
                                    safe_record[col] = v
                            else:
                                safe_record[col] = format_date_str(str(v))
                        sample.append(safe_record)
                    
                    # Store column order for frontend
                    column_order = columns_ordered
                else:
                    column_order = []
                
                return {
                    "type": "DataFrame",
                    "rows": len(data),
                    "columns": len(data.columns),
                    "column_names": [format_date_str(str(c)[:30]) for c in list(data.columns)[:10]],
                    "column_order": column_order,  # Full ordered column list
                    "sample": sample
                }
            elif hasattr(data, 'total_rows'):  # VisualGrid
                return {
                    "type": "Grid",
                    "rows": data.total_rows,
                    "columns": data.total_cols
                }
            else:
                return {"type": str(type(data).__name__)}
        except Exception as e:
            return {"error": str(e)}
    
    def _execute_tool_calls(self, grid, tool_calls: List[Dict], trace=None) -> pd.DataFrame:
        """
        DEPRECATED: Use execute_tools_node via LangGraph instead.
        
        This method lacks tool validation, HITL pause/resume, rollback support,
        and inter-tool dependency checking that execute_tools_node provides.
        Kept for backward compatibility with non-LangGraph execution paths.
        
        Args:
            grid: VisualGrid object
            tool_calls: List of tool call specifications from the plan
            trace: ProcessingTrace for logging
            
        Returns:
            Transformed DataFrame
        """
        from ..tools.transformation_tools import TransformationTools, ToolResult
        from ..utils.mcp_client import get_mcp_client
        
        logger.info(f"Executing {len(tool_calls)} tool calls via MCP")
        
        # Initialize MCP client
        mcp_client = get_mcp_client()
        
        current_data = grid
        
        # Tools that modify data integrity and require human review
        SENSITIVE_TOOLS = {
            'transform.filter_summaries', 
            'layout.filter_header_repeats', 
            'transform.filter_empty',
            'transform.drop_columns',
            'transform.fill_merged',
            'transform.unpivot',
            # Backward compat aliases:
            'filter_summary_rows', 'filter_header_rows', 'filter_empty_rows',
            'filter_summary_columns', 'fill_merged_cells', 'unpivot_columns',
            'xls.data.filter_summaries', 'xls.header.filter_repeats',
            'xls.data.filter_empty', 'xls.header.fill_merged', 'xls.reshape.melt'
        }
        
        triggered_sensitive_tools = set()
        
        for tool_call in sorted(tool_calls, key=lambda x: x.get("step", 0)):
            tool_name = tool_call.get("tool", "")
            params = tool_call.get("params", {})
            description = tool_call.get("description", "")
            
            logger.info(f"  Step {tool_call.get('step', '?')}: {tool_name} - {description}")
            
            # Capture input preview for Tool Inspector
            input_preview = self._get_data_preview(current_data)
            
            try:
                # ===== PREPARE DATA FOR MCP =====
                # Logic: 
                # 1. If tool is an extraction tool or stack_tables, it needs the FULL GRID (nested list)
                # 2. Otherwise, it needs the CURRENT DATAFRAME (list of records)
                
                grid_based_tools = [
                    'layout.extract', 'layout.stack', 'layout.unpivot_matrix',
                    # Backward compat aliases:
                    'extract_data_block', 'stack_tables', 'crosstab_unpivot',
                    'xls.layout.extract', 'xls.layout.stack', 'xls.reshape.unpivot_matrix'
                ]
                
                if tool_name in grid_based_tools and hasattr(grid, 'data'):
                    # Use raw grid data (nested list)
                    data_for_mcp = grid.data
                    logger.info(f"Passing GRID (raw data) to {tool_name}")
                elif tool_name in grid_based_tools and hasattr(grid, 'to_dataframe'):
                    # Fallback for grids without .data
                    data_for_mcp = grid.to_dataframe().values.tolist()
                    logger.info(f"Passing GRID (converted list) to {tool_name}")
                elif isinstance(current_data, pd.DataFrame):
                    # Use current DataFrame data (list of records)
                    data_for_mcp = current_data.to_dict(orient='records')
                    logger.info(f"Passing DATAFRAME (records) to {tool_name}")
                else:
                    data_for_mcp = current_data
                
                # Add data to params for MCP call
                mcp_params = {**params, 'data': data_for_mcp}

                
                # Execute via MCP
                mcp_result = mcp_client.call_tool(tool_name, mcp_params)
                
                # Convert MCP result to ToolResult format
                if mcp_result.get('ok') or mcp_result.get('success'):
                    # Reconstruct DataFrame from result
                    data_preview = mcp_result.get('data_preview', [])
                    if data_preview and isinstance(data_preview, list):
                        current_data = pd.DataFrame(data_preview)
                    
                    result = ToolResult(
                        success=True,
                        data=current_data,
                        message=mcp_result.get('message', 'Success')
                    )
                else:
                    context = mcp_result.get('context', {})
                    msg = context.get('summary') or mcp_result.get('message', 'MCP call failed')
                    result = ToolResult(
                        success=False,
                        data=current_data,
                        message=msg
                    )
                
                # Log to observer for Debug UI
                from sia.debug.llm_observer import get_observer
                observer = get_observer()
                
                if result.success:
                    current_data = result.data
                    # Capture output preview for Tool Inspector
                    output_preview = self._get_data_preview(current_data)
                    
                    # Save snapshot for export
                    snapshot_path = None
                    if isinstance(current_data, pd.DataFrame) and len(current_data) > 0:
                        try:
                            from pathlib import Path
                            output_folder = Path(__file__).parent.parent.parent / 'output'
                            output_folder.mkdir(exist_ok=True)
                            step_num = tool_call.get('step', len(tool_calls))
                            snapshot_file = output_folder / f"step_{step_num}_{tool_name}.xlsx"
                            current_data.to_excel(snapshot_file, index=False, engine='openpyxl')
                            snapshot_path = str(snapshot_file)
                            logger.info(f"    Saved snapshot: {snapshot_file.name}")
                        except Exception as e:
                            logger.info(f"    Failed to save snapshot: {e}")
                    
                    logger.info(f"    Success (MCP): {result.message}")
                    if trace:
                        trace.add_step("tool_execution", tool_name, result.message, {"score": 1.0})
                else:
                    logger.info(f"    Failed (MCP): {result.message}")
                    if trace:
                        trace.add_warning(f"Tool {tool_name} failed: {result.message}")
                
                # Check for sensitive tools
                if tool_name in SENSITIVE_TOOLS:
                    triggered_sensitive_tools.add(tool_name)
                    
            except Exception as e:
                logger.info(f"    Error: {e}")
                logger.exception(f"tool_{tool_name}", e)
                if trace:
                    trace.add_error(f"Tool {tool_name} error: {str(e)}")
        
        # Flag human review if sensitive tools were used
        if triggered_sensitive_tools and trace:
            tools_str = ", ".join(triggered_sensitive_tools)
            trace.add_warning(f"Data Integrity Check: Tools requiring confirmation were used ({tools_str})")
            if hasattr(trace, 'requires_review'):
                 trace.requires_review = True
                 trace.review_reason = f"Data integrity changed by: {tools_str}"
        
        # Ensure we return a DataFrame
        if isinstance(current_data, pd.DataFrame):
            logger.info(f"Tool execution complete (MCP): {len(current_data)} rows")
            return current_data
        else:
            logger.info("Warning: Tool execution did not produce a DataFrame")
            return pd.DataFrame()

    
    def process_file(
        self,
        file_path: str,
        resume_state: Dict = None,
        target_template_path: str = None,
        context_packet: Dict = None,
    ) -> Tuple[InferredSchema, pd.DataFrame, ProcessingTrace]:
        """
        Process an Excel file using LangGraph.
        
        Args:
            file_path: Path to the Excel file
            resume_state: Optional state to resume from (for HITL continuation)
            target_template_path: Optional path to job-specific JSON target template
            context_packet: Optional structured metadata and rule context for this run
            
        Returns:
            Tuple of (schema, dataframe, trace)
            If HITL pause is needed, trace.hitl_pending will be True
        """
        trace_id = str(uuid.uuid4())[:8]
        trace = ProcessingTrace(trace_id=trace_id)
        
        # Load optional template
        target_template = None
        if target_template_path and Path(target_template_path).exists():
            try:
                with open(target_template_path, 'r') as f:
                    target_template = normalize_target_template(json.load(f))
                logger.info(f"Loaded job-specific target template from {target_template_path}")
            except Exception as e:
                logger.warning(f"Failed to load target template {target_template_path}: {e}")

        if context_packet and not target_template and context_packet.get("target_template"):
            target_template = context_packet.get("target_template")

        # Initialize LLM Observer for local debugging
        from sia.debug.llm_observer import init_observer
        init_observer(run_id=trace_id, model_id=self.model_name, enabled=self.debug_enabled)
        # Attach observer events into the processing trace for easier debugging output
        try:
            from sia.debug.llm_observer import get_observer
            trace.attach_debug_session(get_observer())
        except Exception:
            # Non-fatal: continue even if observer attachment fails
            logger.debug("LLM observer attach skipped or failed")
        
        # Check for initialization error
        if hasattr(self, 'init_error') and self.init_error:
            trace.add_error(self.init_error)
            logger.error(f"Processing aborted: {self.init_error}")
            return InferredSchema("error"), pd.DataFrame(), trace

        logger.info(f"========== PROCESSING FILE (LangGraph): {file_path} ==========")
        
        # Initialize Graph
        from sia.agent.graph import create_graph
        app = create_graph()
        
        # Initial State (or resume from saved state)
        from sia.agent.hitl import HITLManager
        _cp_sm = ((context_packet or {}).get("source_metadata") or {}) if isinstance(context_packet, dict) else {}
        _seed_sid = _cp_sm.get("source_id") if isinstance(_cp_sm, dict) else None
        initial_state = {
            "file_path": file_path,
            "sheet_name": "Sheet1", # Default to Sheet1 or handle logic to find best sheet
            "target_template": target_template, # NEW: Optional template
            "context_packet": context_packet,
            "source_metadata": (context_packet or {}).get("source_metadata"),
            "source_id": str(_seed_sid).strip() if _seed_sid else None,
            "approved_mappings": (context_packet or {}).get("approved_mappings", []),
            "business_rules": (context_packet or {}).get("business_rules", []),
            "mapping_summary": None,
            "relationship_proposals": [],
            "approved_relationships": (context_packet or {}).get("file_relationships", []),
            "grid": None,
            "structure_analysis": None,
            "extraction_plan": None,
            "current_df": None,
            "scoped_source": None,
            "is_flat": False,
            "iteration": 1,
            "max_iterations": 3,
            "suggested_tools": [],
            "verifier_issues": [], # NEW: Initialize verifier issues
            "final_schema": None,
            "trace_steps": [],
            "errors": [],
            "warnings": [],
            "requires_review": False,
            "review_reason": "",
            "llm_client": self.llm_client,
            "debug_enabled": self.debug_enabled,
            # Default to requiring explicit approval for destructive operations.
            "destructive_approved": False,
            # Ensure HITL checkpoint logic is active unless caller overrides.
            "hitl_manager": HITLManager(),
            "tools_history": [], # Initialize lists for operator.add
            "issues_history": [],
            "confidence_trajectory": [],
            "hitl_checkpoints": [],
            "escalation_reason": "",
            "resume_mode": None,
            "hitl_pause_type": None,
            "multi_source_active_batch": False,
            "multi_block_active_batch": False,
            "process_blocks_separately": False,
        }
        if isinstance(context_packet, dict):
            if bool(context_packet.get("process_blocks_separately")):
                initial_state["process_blocks_separately"] = True
            if bool(context_packet.get("multi_block_batch")):
                initial_state["multi_block_active_batch"] = True
        
        if resume_state:
            from sia.agent.graph_resume import (
                can_skip_pipeline_after_load,
                prepare_plan_review_resume_state,
                resolve_resume_graph_entry,
            )

            resume_state = prepare_plan_review_resume_state(
                resume_state,
                use_existing_plan=str(resume_state.get("resume_mode") or "") == "use_existing_plan",
                materialized_clean_active=bool(resume_state.get("materialized_clean_active")),
            )
            initial_state.update(resume_state)
            if bool(resume_state.get("multi_source_active_batch")):
                initial_state["multi_source_active_batch"] = True
            if bool(resume_state.get("multi_block_active_batch")):
                initial_state["multi_block_active_batch"] = True
            if bool(resume_state.get("process_blocks_separately")):
                initial_state["process_blocks_separately"] = True
            entry_node = resolve_resume_graph_entry(initial_state)
            initial_state["resume_graph_from"] = entry_node
            if not initial_state.get("source_id"):
                smx = initial_state.get("source_metadata") or {}
                if isinstance(smx, dict) and smx.get("source_id"):
                    initial_state["source_id"] = str(smx.get("source_id")).strip()
                elif isinstance(initial_state.get("context_packet"), dict):
                    csm = (initial_state["context_packet"].get("source_metadata") or {})
                    if isinstance(csm, dict) and csm.get("source_id"):
                        initial_state["source_id"] = str(csm.get("source_id")).strip()
            if "destructive_approved" in resume_state:
                logger.info(f"Resuming with approval status: {resume_state.get('destructive_approved')}")
            trace.add_lifecycle_event(
                phase="resume",
                status="info",
                message=(
                    f"Resuming graph at {entry_node}"
                    + (
                        " → execute_tools after load (plan-review fast path)"
                        if can_skip_pipeline_after_load(initial_state)
                        else (" (skipped load/analyze/mapping/plan)" if entry_node == "execute_tools" else "")
                    )
                ),
                metadata={
                    "resume_keys": sorted(list(resume_state.keys())),
                    "resume_graph_from": entry_node,
                    "resume_skip_pipeline_after_load": bool(
                        initial_state.get("resume_skip_pipeline_after_load")
                    ),
                    "hitl_resume_from": initial_state.get("hitl_resume_from"),
                    "resume_mode": initial_state.get("resume_mode"),
                },
            )
        else:
            trace.add_lifecycle_event(
                phase="setup",
                status="info",
                message="Starting new graph run",
                metadata={"file_path": file_path},
            )

        # Upload metadata may still point at the original file; pipeline_workbook_* is what load_file uses.
        sm = dict(initial_state.get("source_metadata") or {})
        sm["pipeline_workbook_path"] = initial_state.get("file_path")
        sm["pipeline_sheet_name"] = initial_state.get("sheet_name")
        initial_state["source_metadata"] = sm
        if not initial_state.get("source_id") and sm.get("source_id"):
            initial_state["source_id"] = str(sm.get("source_id")).strip()
        initial_state["enable_llm_judge"] = self.enable_llm_judge

        try:
            # Execute Graph
            print(f"[PROCESS] Invoking graph for job...", flush=True)
            final_state = app.invoke(initial_state)
            print(f"[PROCESS] Graph finished! Trace steps: {[s.get('step') for s in final_state.get('trace_steps', [])]}", flush=True)
            
            # Surface verifier issues from final state
            trace.verifier_issues = final_state.get("verifier_issues", [])
            trace.approval_items = final_state.get("approval_items", [])
            trace.planned_rule_actions = final_state.get("planned_rule_actions", [])

            # ===== NEW: Check for HITL Pause =====
            if final_state.get("hitl_pending_approval", False):
                logger.info("Graph paused for HITL review/approval")
                pause_type = final_state.get("hitl_pause_type") or "plan_review"
                trace.add_lifecycle_event(
                    phase=("plan_review" if pause_type == "plan_review" else "execute_pause"),
                    status="pending",
                    message=f"Graph paused for HITL ({pause_type})",
                    metadata={
                        "checkpoints": [
                            {
                                "id": cp.get("checkpoint_id"),
                                "type": cp.get("checkpoint_type"),
                                "severity": cp.get("severity"),
                            }
                            for cp in (final_state.get("hitl_checkpoints") or [])
                            if isinstance(cp, dict)
                        ],
                        "pause_type": pause_type,
                    },
                )
                trace.hitl_pending = True
                trace.hitl_pause_type = final_state.get("hitl_pause_type")
                trace.deletion_previews = final_state.get("deletion_previews", [])
                trace.low_confidence_items = final_state.get("low_confidence_items", [])
                trace.hitl_checkpoints = final_state.get("hitl_checkpoints", [])
                pause_reason = (
                    final_state.get("escalation_reason")
                    or final_state.get("review_reason")
                    or (
                        f"Paused for approval of {len(trace.deletion_previews)} destructive operations"
                        if trace.deletion_previews else
                        "Paused for human review"
                    )
                )
                trace.review_reason = pause_reason
                trace.pending_state = final_state  # Save state for resumption
                trace.add_step("hitl_pause", "awaiting_approval", 
                              pause_reason, 
                              {"score": 0.5})
                
                # Return partial results with pause flag
                partial_df = final_state.get("current_df") if final_state.get("current_df") is not None else pd.DataFrame()
                partial_schema = InferredSchema("pending_hitl")
                return partial_schema, partial_df, trace
            
            # Extract Results
            schema = final_state.get("final_schema") or InferredSchema("error")
            df = final_state.get("current_df") if final_state.get("current_df") is not None else pd.DataFrame()
            
            # Derive lifecycle events from trace_steps for the Debug timeline
            _phase_map = {
                "load_file": ("load", "ok", "Loaded source file"),
                "analyze_structure": ("analyze", "ok", "Structure analysis complete"),
                "resolve_mapping": ("mapping", "ok", "Column mapping resolved"),
                "infer_relationships": ("relationships", "ok", "Source relationships inferred"),
                "discovery.propose_file_relationships": ("relationships", "ok", "Relationship proposals (tool)"),
                "generate_plan": ("plan", "ok", "Plan generated"),
                "execute_tools": ("execute", "ok", "Tools executed"),
                "hitl_pause": ("execute_pause", "pending", "HITL pause during execution"),
                "verify_output": ("verify", "ok", "Output verification complete"),
                "replan": ("replan", "warning", "Replanning after verification"),
                "finalize": ("finalize", "ok", "Finalization complete"),
                "finalize_crash": ("error", "error", "Finalization crashed"),
            }
            for step in final_state.get("trace_steps", []):
                if not isinstance(step, dict):
                    continue
                key = str(step.get("step", "")).strip()
                if key in _phase_map:
                    phase, status, default_msg = _phase_map[key]
                    message = default_msg
                    if key == "execute_tools" and "tools_executed" in step:
                        message = f"Executed {step.get('tools_executed', 0)} tools"
                    elif key == "generate_plan" and "tools_count" in step:
                        message = f"Plan with {step.get('tools_count', 0)} tools"
                    elif key == "resolve_mapping" and "mappings_count" in step:
                        message = f"Resolved {step.get('mappings_count', 0)} mappings"
                    elif key == "infer_relationships" and "relationship_count" in step:
                        message = f"Relationships: {step.get('relationship_count', 0)}"
                    elif key == "discovery.propose_file_relationships" and "relationship_count" in step:
                        message = f"Relationship tool: {step.get('relationship_count', 0)} proposal(s)"
                    elif key == "verify_output" and "is_flat" in step:
                        message = f"Output flat: {step.get('is_flat')}"
                    trace.add_lifecycle_event(
                        phase=phase,
                        status=status,
                        message=message,
                        metadata={"confidence": step.get("confidence"), "raw_step": key},
                    )
            if final_state.get("output_file"):
                trace.add_lifecycle_event(
                    phase="output_ready",
                    status="ok",
                    message="Excel output ready for download",
                    metadata={"output_file": final_state.get("output_file")},
                )

            # Convert graph trace steps to legacy ProcessingTrace for UI compatibility
            for step in final_state.get("trace_steps", []):
                # Mapping graph steps to UI-friendly output
                module = step.get("step", "unknown")
                output_msg = ""
                # Robsut confidence extraction
                confidence = step.get("confidence")
                if confidence is None:
                    confidence = 1.0
                
                if module == "load_file":
                    fp = step.get("file_path") or ""
                    base = Path(str(fp)).name if fp else "workbook"
                    sh = step.get("sheet") or ""
                    rows, cols = step.get("rows"), step.get("cols")
                    if sh:
                        output_msg = f"{base} · {sh} — {rows}×{cols} scoped rows"
                    else:
                        output_msg = f"{base} — {rows}×{cols} scoped rows"
                elif module == "analyze_structure":
                    output_msg = f"Found {step.get('tables_found')} tables"
                elif module == "resolve_mapping":
                    output_msg = f"Resolved {step.get('mappings_count', 0)} mappings"
                elif module == "infer_relationships":
                    output_msg = f"Inferred {step.get('relationship_count', 0)} relationships"
                elif module == "discovery.propose_file_relationships":
                    output_msg = f"Relationship proposals: {step.get('relationship_count', 0)}"
                elif module == "generate_plan":
                    output_msg = f"Plan with {step.get('tools_count')} tools"
                elif module == "execute_tools":
                    output_msg = f"Executed {step.get('tools_executed')} tools"
                elif module == "verify_output":
                     output_msg = f"Output flat: {step.get('is_flat')}"
                elif module == "finalize":
                    output_msg = f"Finalized schema with {step.get('fields')} fields"
                    # Capture output_file if present in the finalize step result or final_state
                    trace.output_file = final_state.get("output_file")
                    
                trace.add_step(module, "graph_execution", output_msg, {"score": confidence})
            
            # Map HITL status
            if final_state.get("requires_review"):
                trace.requires_review = True
                trace.review_reason = final_state.get("review_reason")
            
            trace.judge_result = final_state.get("judge_result")
            trace.deferred_post_collate_tools = list(final_state.get("deferred_post_collate_tools") or [])
            
            trace.calculate_overall_confidence()
            return schema, df, trace
            
        except Exception as e:
            logger.error(f"Graph execution failed: {e}")
            # Run Judge on Crash (Trace Evaluation only)
            try:
                # Attempt to extract trace steps from partial StateSnapshot if available, or use empty list
                # Since we don't have access to the graph state object here easily without a checkpointer,
                # we rely on what might have been logged or stored.
                
                # RECOVERY: Fetch from global observer
                from sia.debug.llm_observer import get_observer
                observer = get_observer()
                
                crash_trace = []
                if observer:
                    # Convert observer traces to simplified trace steps for the Judge
                    for t in observer.get_all_traces():
                        crash_trace.append({
                            "step": t.component,
                            "input": t.user_prompt[:500], # Truncate for safety
                            "output": t.raw_response[:500],
                            "error": t.error_message
                        })
                    # Add tool executions
                    for tool in observer.get_tool_executions():
                         crash_trace.append({
                            "step": "tool_execution",
                            "tool": tool["tool"],
                            "params": tool["params"],
                            "result": tool["result"][:500],
                            "success": tool["success"]
                        })
                
                if not crash_trace:
                     crash_trace = [{"step": "crash", "error": str(e), "message": "Graph crashed hard. No trace available."}]
                
                # Mock a judge result for the UI
                from .judge import JudgeScore
                
                # If we have the judge instance (self.judge), use it!
                if self.enable_llm_judge and hasattr(self, 'judge') and self.judge:
                    # Create a mock schema/df for context
                    mock_schema = InferredSchema("error_schema")
                    mock_df = pd.DataFrame() # Empty
                    try:
                        # Attempt a real evaluation of the failed trace
                        judge_result_json = self.judge.evaluate_trace(crash_trace)
                        
                        # Convert JSON result to JudgeScore object
                        from sia.agent.judge import JudgeScore
                        judge_result = JudgeScore(
                            verdict=judge_result_json.get("verdict", "FAIL"),
                            critique=judge_result_json.get("overall_summary", str(e)),
                            fidelity=judge_result_json.get("process_score", 0.0),
                            tool_accuracy=judge_result_json.get("tool_accuracy", 0.0),
                            trajectory_success=judge_result_json.get("trajectory_score", 0.0),
                            metrics=judge_result_json.get("metrics", {"steps": len(crash_trace)}),
                            trace_analysis=judge_result_json
                        )
                        trace.judge_result = judge_result
                        logger.info("Successfully ran Judge on crashed trace.")
                    except Exception as judge_err:
                        logger.error(f"Judge failed on crash recovery: {judge_err}")
                        raise judge_err # Fallback to manual creation
                elif not self.enable_llm_judge:
                    trace.judge_result = JudgeScore(
                        fidelity=0.0,
                        flatness=0.0,
                        integrity=0.0,
                        tool_accuracy=0.0,
                        trajectory_success=0.0,
                        task_success=False,
                        token_efficiency=0.0,
                        verdict="UNKNOWN",
                        critique=f"LLM judge disabled. Original error: {str(e)}",
                        metrics={"steps": len(crash_trace)},
                        trace_analysis={
                            "process_score": 0.0,
                            "overall_summary": "LLM judge is turned off in settings.",
                            "error_handling": str(e),
                            "logic_critique": "N/A",
                            "tool_critique": "N/A",
                        },
                    )
                    logger.info("Crash recovery: skipped LLM judge (disabled).")
                else:
                    # Fallback if no judge instance
                    raise Exception("No judge instance available")

            except Exception as recovery_err:
                # Fallback manual creation
                from sia.agent.judge import JudgeScore
                trace.judge_result = JudgeScore(
                    fidelity=0.0,
                    flatness=0.0,
                    integrity=0.0,
                    tool_accuracy=0.0,
                    trajectory_success=0.0,
                    task_success=False,
                    token_efficiency=0.0,
                    verdict="FAIL",
                    critique=f"System Crash: {str(e)}",
                    metrics={"steps": 0},
                    trace_analysis={
                        "process_score": 0.0,
                        "overall_summary": f"CRITICAL SYSTEM FAILURE: {str(e)}",
                        "error_handling": "System crashed unrecoverably.",
                        "logic_critique": "Analysis failed.",
                        "tool_critique": "N/A"
                    }
                )
            except Exception as judge_err:
                logger.error(f"Judge also failed during crash recovery: {judge_err}")

            import traceback
            traceback.print_exc()
            trace.add_error(str(e))
            return InferredSchema("error"), pd.DataFrame(), trace

    
    def _extract_dataframe(self, grid: VisualGrid, block: DataBlock,
                           columns: List[ColumnSchema]) -> pd.DataFrame:
        """
        Extract data from a block as a pandas DataFrame.
        """
        data = {}
        
        for col in columns:
            col_idx = col.source_coordinates.col_index if col.source_coordinates else 0
            col_values = []
            
            for row_idx in range(block.header_row + 1, block.end_row + 1):
                cell = grid.get_cell(row_idx, col_idx)
                col_values.append(cell.value if cell else None)
            
            data[col.name] = col_values
        
        return pd.DataFrame(data)
    
    def get_hitl_decision(self, confidence: float) -> HITLDecision:
        """
        Determine HITL routing based on confidence score.
        """
        if confidence >= self.auto_approve_threshold:
            return HITLDecision.AUTO_APPROVE
        elif confidence >= self.review_threshold:
            return HITLDecision.FLAG_FOR_REVIEW
        else:
            return HITLDecision.STOP_AND_ASK
    
    def save_results(self, schema: InferredSchema, df: pd.DataFrame,
                     output_dir: str, format: str = "parquet") -> Dict[str, str]:
        """
        Save schema and data to files.
        
        Args:
            schema: The inferred schema
            df: The normalized DataFrame
            output_dir: Output directory
            format: Output format (parquet, csv, json)
            
        Returns:
            Dictionary of output file paths
        """
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        
        outputs = {}
        
        # Save schema
        schema_path = output_path / f"{schema.schema_name}.json"
        with open(schema_path, 'w') as f:
            f.write(schema.to_json(indent=2))
        outputs['schema'] = str(schema_path)
        
        # Save data
        if format == "parquet":
            data_path = output_path / f"{schema.schema_name}.parquet"
            df.to_parquet(data_path, index=False)
        elif format == "csv":
            data_path = output_path / f"{schema.schema_name}.csv"
            df.to_csv(data_path, index=False)
        else:
            data_path = output_path / f"{schema.schema_name}_data.json"
            df.to_json(data_path, orient='records', indent=2)
        
        outputs['data'] = str(data_path)
        
        logger.info(f"Saved results to: {outputs}")
        return outputs
    
    def add_training_example(self, grid_snippet: str, metadata: Dict,
                             schema_output: Dict) -> bool:
        """
        Add a labeled example to the vector store for future retrieval.
        """
        example_id = f"example_{uuid.uuid4().hex[:8]}"
        return self.vector_store.add_example(
            example_id, grid_snippet, metadata, schema_output
        )
