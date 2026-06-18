"""
Internal LLM Handling Utilities - Localized for the Agent layer.
"""
import json
import logging
import re
import time
from typing import Dict, List, Any, Optional, Tuple, Callable
from dataclasses import dataclass
from ..utils.errors import LLMError
from ..debug.llm_observer import get_observer

logger = logging.getLogger(__name__)

@dataclass
class RetryConfig:
    max_attempts: int = 3
    initial_delay: float = 1.0
    backoff_factor: float = 2.0
    retryable_statuses: List[int] = None

class JSONParseError(LLMError):
    """Raised when JSON parsing of LLM response fails."""
    pass

def safe_get(data: Dict, key: str, expected_type: Any, default: Any = None) -> Any:
    """
    Safely extract a value from a dictionary with type enforcement.
    Coerces types if possible, otherwise returns default.
    """
    if not isinstance(data, dict):
        return default
        
    val = data.get(key)
    if val is None:
        return default
    
    # Special case: if expected_type is Any, return the value as-is
    # This is needed because isinstance() cannot work with typing.Any
    if expected_type is Any:
        return val
        
    # Direct type match
    if isinstance(val, expected_type):
        return val
        
    # Coercion logic
    try:
        if expected_type == str:
            return str(val)
        if expected_type == float:
            return float(val)
        if expected_type == int:
            return int(val)
        if expected_type == list and not isinstance(val, list):
            return [val] if val else []
        if expected_type == dict and not isinstance(val, dict):
            return {}
    except (ValueError, TypeError):
        pass
        
    return default

def robust_json_parse(text: str) -> Dict[str, Any]:
    """
    Extract and parse JSON from LLM response with multi-layer fallback strategy.
    Handles markdown blocks, trailing commas, and leading/trailing 'garbage' text.
    """
    if not text:
        return {}
    
    # Pre-clean: remove BOM and normalize whitespace
    text = text.strip().lstrip('\ufeff')
    
    # Strategy 1: Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    
    # Strategy 2: Find largest { } or [ ] block
    json_blocks = []
    
    # Look for markdown code blocks first
    md_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if md_matches:
        for block in md_matches:
            json_blocks.append(block.strip())
            
    # Also look for any sequence starting with { and ending with }
    # OR starting with [ and ending with ]
    potential_matches = re.findall(r'(\{[\s\S]*\}|\[[\s\S]*\])', text)
    if potential_matches:
        # Sort by length, longest first
        for block in sorted(potential_matches, key=len, reverse=True):
            json_blocks.append(block.strip())
            
    for block in json_blocks:
        # Try direct parse
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass
            
        # Try cleaning: Remove trailing commas before closing braces/brackets
        cleaned = re.sub(r',\s*([\]}])', r'\1', block)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
            
        # Try cleaning: Fix unquoted numeric keys (e.g., { 0: -> { "0":)
        # This is common in some LLM outputs and technically valid in JS but not JSON
        cleaned = re.sub(r'([{\[,])\s*(\d+)\s*:', r'\1 "\2":', cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
            
        # Try cleaning: Handle unescaped newlines in strings (common LLM error)
        # This is high-risk, so we do it last
        cleaned = re.sub(r'([^\\])\n', r'\1\\n', cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
            
        # Try cleaning: Replace single quotes with double quotes (Python dict style)
        # We use a very simple regex that avoids replacing single quotes inside already double-quoted strings
        cleaned = re.sub(r"'(.*?)'", r'"\1"', block)
        try:
             return json.loads(cleaned)
        except json.JSONDecodeError:
             pass
            
    # Strategy 3: Handle Truncated JSON (New)
    # 1. Peel back to the last potentially complete field
    # Try the original text and also any identified blocks that might be truncated
    repair_candidates = [text] + json_blocks
    
    for candidate in repair_candidates:
        repair_block = candidate.strip()
        if not repair_block: continue
        
        # If it ends in a comma, remove it
        if repair_block.endswith(','):
            repair_block = repair_block[:-1].strip()
        
        # Try different peel-back points
        peel_back_points = [len(repair_block)] # Start with full block
        # Find all commas, braces, and brackets
        for m in re.finditer(r'[,{\[]', repair_block):
            peel_back_points.append(m.start() + 1)
        
        # Sort by latest first
        peel_back_points.sort(reverse=True)
        
        for point in peel_back_points:
            temp_block = repair_block[:point].strip()
            if not temp_block: continue
            
            # If it ends in a comma, remove it
            if temp_block.endswith(','):
                temp_block = temp_block[:-1].strip()
            
            # Close open string if needed
            quotes = re.findall(r'(?<!\\)"', temp_block)
            if len(quotes) % 2 != 0:
                temp_block += '"'
            
            # Close open blocks in reverse order
            stack = []
            is_in_string = False
            escaped = False
            for char in temp_block:
                if char == '"' and not escaped:
                    is_in_string = not is_in_string
                    continue
                if is_in_string:
                    if char == '\\': escaped = not escaped
                    else: escaped = False
                    continue
                if char == '{': stack.append('}')
                elif char == '[': stack.append(']')
                elif char == '}' and stack and stack[-1] == '}': stack.pop()
                elif char == ']' and stack and stack[-1] == ']': stack.pop()
            
            while stack:
                temp_block += stack.pop()
            
            try:
                parsed = json.loads(temp_block)
                
                # FINAL HARDENING: Ensure we return a dictionary
                if not isinstance(parsed, dict):
                    if isinstance(parsed, list):
                        parsed = {"tool_calls": parsed}
                    else:
                        parsed = {"raw_output": parsed}

                # Validation heuristic: If it has tool_calls, ensure it's a list and filter invalid ones
                if "tool_calls" in parsed:
                    tool_calls = parsed["tool_calls"]
                    if not isinstance(tool_calls, list):
                        tool_calls = [tool_calls] if tool_calls else []
                    
                    # Filter for validity
                    valid_tools = [t for t in tool_calls if isinstance(t, dict) and t.get("tool")]
                    if len(valid_tools) < len(tool_calls):
                        logger.warning(f"Filtered out {len(tool_calls) - len(valid_tools)} invalid/truncated tool calls")
                    parsed["tool_calls"] = valid_tools
                
                return parsed
            except json.JSONDecodeError:
                continue

    logger.error(f"Failed to parse JSON even with robust strategies. Raw response:\n{text}")
    raise JSONParseError("Failed to parse JSON from LLM response", details={"raw_text": text})

class LLMCallWrapper:
    """
    Wrapper for LLM calls with retry logic and observer integration.
    Expects a client that has a generate_content method.
    """
    def __init__(self, client: Any, retry_config: RetryConfig = None):
        self.client = client
        self.retry_config = retry_config or RetryConfig()
        
    def generate_content(self, prompt: str, **kwargs) -> Any:
        """
        Proxy for client.generate_content with retry logic and parameter normalization.
        """
        last_error = None
        delay = self.retry_config.initial_delay
        
        # Determine provider-specific parameters
        # Most SDKs support a specific naming convention
        is_gemini = hasattr(self.client, 'generate_content') and not hasattr(self.client, 'chat')
        
        # Normalize generation config
        gen_config = kwargs.get("generation_config", {})
        if not gen_config and "max_tokens" in kwargs:
             gen_config = {"max_output_tokens": kwargs.get("max_tokens")}
             
        if is_gemini:
            # Gemini SDK expects 'generation_config' with 'max_output_tokens'
            if "max_completion_tokens" in kwargs:
                gen_config["max_output_tokens"] = kwargs.pop("max_completion_tokens")
            elif "max_tokens" in kwargs:
                gen_config["max_output_tokens"] = kwargs.pop("max_tokens")
            kwargs["generation_config"] = gen_config
        else:
            # OpenAI/Azure/etc. wrappers in llm_clients.py now handle the logic
            # Just ensure we don't have conflicting token parameters
            if "max_output_tokens" in gen_config:
                if "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
                    kwargs["max_tokens"] = gen_config.pop("max_output_tokens")
            if "temperature" in gen_config and "temperature" not in kwargs:
                kwargs["temperature"] = gen_config.pop("temperature")

        for attempt in range(self.retry_config.max_attempts):
            try:
                # Call the underlying client
                return self.client.generate_content(prompt, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(f"LLM call attempt {attempt + 1} failed: {e}")
                
                if attempt < self.retry_config.max_attempts - 1:
                    time.sleep(delay)
                    delay *= self.retry_config.backoff_factor
                else:
                    break
        
        logger.error(f"LLM call failed after {self.retry_config.max_attempts} attempts: {last_error}")
        raise LLMError(f"LLM call failed: {last_error}") from last_error
