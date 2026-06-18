"""
Shared base artifacts and utilities for the agent components.
"""
import logging
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class ExtractionPlan:
    """Plan for extracting data from a grid using tool calls."""
    blocks: List[Dict] = field(default_factory=list)
    tool_calls: List[Dict] = field(default_factory=list)  # Sequence of tools to execute
    column_mappings: List[Dict] = field(default_factory=list)
    business_rule_actions: List[Dict] = field(default_factory=list)
    approval_items: List[Dict] = field(default_factory=list)
    expected_columns: List[str] = field(default_factory=list)  # Expected output columns
    confidence: float = 0.0
    reasoning: str = ""  # LLM's explanation of the plan
    requires_human_review: bool = False
    review_reason: str = ""
    raw_analysis: str = ""

@dataclass
class VerificationResult:
    """Result of output verification."""
    is_flat: bool = False
    confidence: float = 0.0
    issues: List[Dict] = field(default_factory=list)
    rule_issues: List[Dict] = field(default_factory=list)
    suggested_tools: List[Dict] = field(default_factory=list)
    summary: str = ""

def load_prompt_from_file(prompt_name: str) -> tuple[str, str]:
    """
    Load a system prompt from an external markdown file.
    
    Args:
        prompt_name: Name of the prompt file (without .md extension)
    
    Returns:
        Tuple of (prompt_text, version)
    """
    # Assuming this file is in sia/agent/base.py
    prompts_dir = Path(__file__).parent.parent.parent / "prompts"
    prompt_file = prompts_dir / f"{prompt_name}.md"
    
    if not prompt_file.exists():
        logger.warning(f"Prompt file not found: {prompt_file}")
        return "", "0.0"
    
    try:
        content = prompt_file.read_text(encoding="utf-8")
        
        # Parse YAML frontmatter
        version = "1.0"
        body = content
        
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1]
                body = parts[2].strip()
                
                # Extract version from frontmatter
                version_match = re.search(r'version:\s*["\']?([^"\'\\n]+)["\']?', frontmatter)
                if version_match:
                    version = version_match.group(1)
        
        return body, version
    except Exception as e:
        logger.error(f"Failed to load prompt {prompt_name}: {e}")
        return "", "0.0"

def robust_bool(val: Any) -> bool:
    """
    Standardized conversion of LLM-returned 'truthiness' to boolean.
    Handles strings like 'YES', 'passed', '1', 'True' etc.
    """
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    
    s = str(val).lower().strip()
    return s in ('true', 'yes', 'y', '1', 'passed', 'pass', 'success', 'ok', 'flat')
