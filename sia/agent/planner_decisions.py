"""
Apply analyst answers from planner review ``approval_items`` to context and tool plans.

Plan review collects structured choices (``analyst_confirm``, ``resolution_note``) but until
this module runs they were only appended to job notes — not used to adjust mappings or
``transform.calculate`` / ``transform.rename`` steps.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .base import ExtractionPlan
from ..tools.tool_validator import normalize_tool_name

logger = logging.getLogger(__name__)

KEEP_DECISIONS = {"keep", "approved", "primary", "supporting", "metadata", "context", "use as context"}

_SINGLE_SPEND_PATTERNS = (
    r"\bsingle\b",
    r"\bone\s+column\b",
    r"\bbudget\s*total\b",
    r"\bbudgets?_total\b",
    r"\bheader\s+total\b",
    r"\bparent\s+row\b",
    r"\bblock\s+total\b",
    r"\buse\s+[`'\"]?\w+[`'\"]?\s+as\s+spend",
    r"\bonly\s+[`'\"]?\w+[`'\"]?\b",
    r"\bdo\s+not\s+sum\b",
    r"\bno\s+combine\b",
    r"\brename\s+only\b",
)

_COMBINE_SPEND_PATTERNS = (
    r"\bcombine\b",
    r"\bsum\b",
    r"\badd\b",
    r"\bmultiple\b",
    r"\bthree\b",
    r"\bcomponents?\b",
    r"\bfees?\b.*\bmedia\b",
    r"\brollup\b",
    r"\baggregate\b",
)

_TEMPLATE_STUB_RE = re.compile(
    r"\b(spends?|impressions?|clicks?|views?)_\d+\b",
    re.IGNORECASE,
)


def merge_resolved_approval_items(
    original_items: List[Dict[str, Any]],
    resolution_notes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Merge UI resolution payloads back onto the planner's approval item definitions."""
    merged: List[Dict[str, Any]] = []
    originals = [dict(x) for x in (original_items or []) if isinstance(x, dict)]
    resolutions = [dict(x) for x in (resolution_notes or []) if isinstance(x, dict)]
    for idx, orig in enumerate(originals):
        row = dict(orig)
        res = resolutions[idx] if idx < len(resolutions) else {}
        if res.get("analyst_confirm") is not None:
            row["analyst_confirm"] = res.get("analyst_confirm")
        if res.get("resolution_note"):
            row["resolution_note"] = res.get("resolution_note")
        if res.get("index") is not None:
            row["resolution_index"] = res.get("index")
        merged.append(row)
    for extra in resolutions[len(originals) :]:
        merged.append(dict(extra))
    return merged


def duplicate_target_sources(
    approved_mappings: List[Dict[str, Any]],
) -> Dict[str, List[str]]:
    """Template target -> physical source columns (multiple sources may map to one target)."""
    by_target: Dict[str, List[str]] = {}
    for item in approved_mappings or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision", "")).strip().lower()
        if decision not in KEEP_DECISIONS:
            continue
        target = str(item.get("target_column") or "").strip()
        source = str(item.get("source_column") or item.get("column_name") or "").strip()
        if not target or target == "No match" or not source:
            continue
        by_target.setdefault(target, [])
        if source not in by_target[target]:
            by_target[target].append(source)
    return {k: v for k, v in by_target.items() if len(v) > 1}


def _text_blob(item: Dict[str, Any]) -> str:
    parts = [
        str(item.get("analyst_confirm") or ""),
        str(item.get("resolution_note") or ""),
        str(item.get("question") or ""),
        str(item.get("summary") or ""),
        str(item.get("description") or ""),
    ]
    for opt in item.get("options") or item.get("choices") or []:
        if isinstance(opt, dict):
            parts.append(str(opt.get("id") or ""))
            parts.append(str(opt.get("label") or ""))
    return " ".join(parts).lower()


def _match_choice_label(item: Dict[str, Any], choice_id: str) -> str:
    for opt in item.get("options") or item.get("choices") or []:
        if not isinstance(opt, dict):
            continue
        oid = str(opt.get("id") or opt.get("value") or "").strip()
        if oid and oid == choice_id:
            return str(opt.get("label") or opt.get("text") or "").strip()
    return ""


def infer_block_metric_allocation(item: Dict[str, Any]) -> Optional[Dict[str, Optional[str]]]:
    """
    Parse analyst choice for block/merged metric allocation.

    Returns ``{"method": "equal"|"by_weight", "weight_col": str|None}`` or None.
    """
    if str(item.get("decision_type") or "").strip().lower() != "block_metric_allocation":
        blob = _text_blob(item)
        if "block metric allocation" not in blob and "merged" not in blob:
            if not any(
                k in blob
                for k in (
                    "split equally",
                    "equal split",
                    "by weight",
                    "by_weight",
                    "helper column",
                )
            ):
                return None

    choice_id = str(item.get("analyst_confirm") or "").strip().lower()
    label = _match_choice_label(item, choice_id).lower() if choice_id else ""
    if choice_id == "equal" or "equal" in label and "weight" not in label:
        return {"method": "equal", "weight_col": None}
    if choice_id.startswith("by_weight") or "by weight" in label or "weighted" in label:
        weight_col: Optional[str] = None
        if choice_id.startswith("by_weight__"):
            weight_col = choice_id.split("__", 1)[-1].strip()
        if not weight_col:
            note = str(item.get("resolution_note") or "")
            for quoted in re.findall(r"['\"]([^'\"]{2,})['\"]", note):
                q = str(quoted).strip()
                if q:
                    weight_col = q
                    break
        if not weight_col:
            note = str(item.get("resolution_note") or "")
            skip_tokens = frozenset(
                {
                    "choice",
                    "split",
                    "spends",
                    "spend",
                    "using",
                    "weight",
                    "helper",
                    "column",
                    "row_metric",
                    "block_total",
                    "sum_weights",
                    "equal",
                    "block",
                    "merged",
                    "range",
                    "notes",
                }
            )
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", note):
                if token.lower() in skip_tokens:
                    continue
                if len(token) >= 3:
                    weight_col = token
                    break
        return {"method": "by_weight", "weight_col": weight_col}
    return None


def infer_metric_target_strategy(item: Dict[str, Any]) -> Optional[str]:
    """
    Return ``single_column`` or ``combine_sources`` when the approval item is about
    how to populate a template metric from multiple source columns.
    """
    target = str(item.get("target_column") or item.get("target") or "").strip().lower()
    blob = _text_blob(item)
    if target not in {"spends", "spend", "impressions", "clicks", "views"} and not any(
        k in blob for k in ("spend", "budget", "impression", "metric", "combine", "single column")
    ):
        return None

    choice_id = str(item.get("analyst_confirm") or "").strip().lower()
    choice_label = _match_choice_label(item, choice_id).lower() if choice_id else ""
    scan = f"{choice_id} {choice_label} {blob}"

    if any(re.search(p, scan) for p in _COMBINE_SPEND_PATTERNS):
        if not any(re.search(p, scan) for p in _SINGLE_SPEND_PATTERNS):
            return "combine_sources"
    if any(re.search(p, scan) for p in _SINGLE_SPEND_PATTERNS):
        return "single_column"
    if choice_id in {"combine", "combine_sources", "sum_components", "sum_columns", "multi_column"}:
        return "combine_sources"
    if choice_id in {
        "single_column",
        "single_source",
        "budget_total",
        "use_budget_total",
        "one_column",
        "rename_only",
    }:
        return "single_column"
    return None


def _pick_primary_source(
    sources: List[str],
    item: Dict[str, Any],
) -> str:
    """Choose one physical source when analyst picked a single-column strategy."""
    choice_id = str(item.get("analyst_confirm") or "").strip().lower()
    label = _match_choice_label(item, choice_id)
    blob = f"{choice_id} {label}".lower()
    spend_like = ("budget", "total", "spend", "cost", "fee", "billing")
    for src in sources:
        sl = src.lower()
        if any(k in sl for k in spend_like):
            if sl.replace("_", " ") in blob or sl in blob:
                return src
    for src in sources:
        sl = src.lower()
        if any(k in sl for k in spend_like):
            return src
    return sources[0]


def _patch_approved_mappings_for_strategy(
    approved_mappings: List[Dict[str, Any]],
    target_metric: str,
    strategy: str,
    resolved_item: Dict[str, Any],
    duplicate_sources: List[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    if not duplicate_sources:
        return approved_mappings, notes

    target_metric = str(target_metric or "spends").strip()
    primary = _pick_primary_source(duplicate_sources, resolved_item)
    patched: List[Dict[str, Any]] = []

    for row in approved_mappings:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        src = str(item.get("source_column") or item.get("column_name") or "").strip()
        tgt = str(item.get("target_column") or "").strip()
        if tgt != target_metric or src not in duplicate_sources:
            patched.append(item)
            continue
        if strategy == "single_column":
            if src == primary:
                item["decision"] = "keep"
                item["role"] = item.get("role") or "primary"
                patched.append(item)
            else:
                item["decision"] = "discard"
                item["target_column"] = "No match"
                item["target_match_confidence"] = 0.0
                patched.append(item)
            continue
        patched.append(item)

    if strategy == "single_column":
        notes.append(
            f"Planner review: use single source '{primary}' for template '{target_metric}' "
            f"(demoted: {', '.join(s for s in duplicate_sources if s != primary)})."
        )
    elif strategy == "combine_sources":
        notes.append(
            f"Planner review: combine sources [{', '.join(duplicate_sources)}] into '{target_metric}'."
        )
    return patched, notes


def _strip_invalid_calculate_steps(
    tool_calls: List[Dict[str, Any]],
    allowed_source_names: Set[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    notes: List[str] = []
    kept: List[Dict[str, Any]] = []
    for tool in tool_calls:
        if not isinstance(tool, dict):
            continue
        norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
        if norm != "transform.calculate":
            kept.append(tool)
            continue
        params = dict(tool.get("params") or {})
        expr = str(params.get("expression") or "")
        target_col = str(params.get("target_column") or "")
        if _TEMPLATE_STUB_RE.search(expr):
            notes.append(
                f"Removed transform.calculate for '{target_col}' (expression used template stubs: {expr})."
            )
            continue
        tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr))
        missing = [t for t in tokens if t not in allowed_source_names and t not in {"and", "or"}]
        if missing and target_col.lower() in {"spends", "spend", "impressions", "clicks"}:
            notes.append(
                f"Removed transform.calculate for '{target_col}' (missing columns: {', '.join(missing)})."
            )
            continue
        kept.append(tool)
    return kept, notes


def _upsert_combine_calculate(
    tool_calls: List[Dict[str, Any]],
    target_metric: str,
    source_columns: List[str],
) -> List[Dict[str, Any]]:
    if len(source_columns) < 2:
        return tool_calls
    safe_cols = [c for c in source_columns if c]
    expr = " + ".join(f"`{c}`" if " " in c else c for c in safe_cols)
    step = {
        "tool": "transform.calculate",
        "params": {
            "target_column": target_metric,
            "expression": expr,
            "source_columns": safe_cols,
        },
        "description": (
            f"Sum source columns into template metric '{target_metric}' per planner review decision."
        ),
    }
    out: List[Dict[str, Any]] = []
    replaced = False
    for tool in tool_calls:
        if not isinstance(tool, dict):
            continue
        norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
        if norm == "transform.calculate":
            params = dict(tool.get("params") or {})
            if str(params.get("target_column") or "").strip() == target_metric:
                if not replaced:
                    out.append(step)
                    replaced = True
                continue
        out.append(dict(tool))
    if not replaced:
        insert_at = 0
        for i, tool in enumerate(out):
            norm, _ = normalize_tool_name(str(tool.get("tool") or "").strip())
            if norm == "transform.rename":
                insert_at = i + 1
        out.insert(insert_at, step)
    for idx, tool in enumerate(out, start=1):
        tool["step"] = idx
    return out


def apply_resolved_approvals_to_context(
    context_packet: Optional[Dict[str, Any]],
    resolved_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Persist structured decisions and patch ``approved_mappings`` when possible."""
    cp = dict(context_packet or {})
    items = [dict(x) for x in (resolved_items or []) if isinstance(x, dict)]
    if not items:
        return cp

    cp["resolved_planner_decisions"] = items
    approved = [dict(x) for x in (cp.get("approved_mappings") or []) if isinstance(x, dict)]
    if not cp.get("duplicate_target_mappings"):
        pre_dupes = duplicate_target_sources(approved)
        if pre_dupes:
            cp["duplicate_target_mappings"] = pre_dupes
    dupes = duplicate_target_sources(approved)
    notes: List[str] = list(cp.get("planner_decision_notes") or [])

    for item in items:
        strategy = infer_metric_target_strategy(item)
        if not strategy:
            continue
        target = str(item.get("target_column") or item.get("target") or "spends").strip()
        sources = dupes.get(target) or []
        if not sources and target.lower() in dupes:
            sources = dupes.get(target.lower()) or []
        if not sources:
            for tgt, srcs in dupes.items():
                if tgt.lower() == target.lower():
                    sources = srcs
                    target = tgt
                    break
        if not sources:
            continue
        approved, patch_notes = _patch_approved_mappings_for_strategy(
            approved, target, strategy, item, sources
        )
        notes.extend(patch_notes)

    if approved != list(cp.get("approved_mappings") or []):
        cp["approved_mappings"] = approved
    if dup_map := duplicate_target_sources(approved):
        cp["duplicate_target_mappings"] = dup_map
    if notes:
        cp["planner_decision_notes"] = notes
    return cp


def _remap_weight_column_to_dataframe_name(
    weight_col: Optional[str],
    context_packet: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Map questionnaire weight column (source or target name) to post-rename column."""
    if not weight_col:
        return weight_col
    col = str(weight_col).strip()
    if not col:
        return weight_col
    src_to_tgt: Dict[str, str] = {}
    tgt_to_src: Dict[str, str] = {}
    for item in list((context_packet or {}).get("approved_mappings") or []):
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision", "")).strip().lower()
        if decision not in ("keep", "approved", "accept"):
            continue
        src = str(item.get("source_column") or "").strip()
        tgt = str(item.get("target_column") or "").strip()
        if not src or not tgt or tgt == "No match":
            continue
        src_to_tgt[src] = tgt
        tgt_to_src.setdefault(tgt, src)
    if col in src_to_tgt:
        return src_to_tgt[col]
    return col


def _patch_block_metric_allocation_tools(
    tools: List[Dict[str, Any]],
    target_metric: str,
    allocation: Dict[str, Optional[str]],
    context_packet: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Apply equal/by_weight allocation to expand_grouped_block and allocate_block_metric steps."""
    notes: List[str] = []
    method = str(allocation.get("method") or "equal").strip().lower()
    if method not in ("equal", "by_weight"):
        method = "equal"
    weight_col = _remap_weight_column_to_dataframe_name(
        allocation.get("weight_col"),
        context_packet,
    )
    target_metric = str(target_metric or "spends").strip()
    patched: List[Dict[str, Any]] = []

    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_copy = dict(tool)
        norm, _ = normalize_tool_name(str(tool_copy.get("tool") or "").strip())
        params = dict(tool_copy.get("params") or {})

        if norm == "transform.expand_grouped_block":
            alloc_list = list(params.get("allocations") or [])
            if not alloc_list:
                spec: Dict[str, Any] = {
                    "metric_col": target_metric,
                    "method": method,
                }
                if method == "by_weight" and weight_col:
                    spec["weight_col"] = weight_col
                alloc_list = [spec]
            else:
                new_allocs: List[Dict[str, Any]] = []
                for spec in alloc_list:
                    if not isinstance(spec, dict):
                        continue
                    spec_copy = dict(spec)
                    mc = str(spec_copy.get("metric_col") or "").strip()
                    if mc.lower() != target_metric.lower():
                        new_allocs.append(spec_copy)
                        continue
                    spec_copy["method"] = method
                    if method == "by_weight" and weight_col:
                        spec_copy["weight_col"] = weight_col
                    else:
                        spec_copy.pop("weight_col", None)
                    new_allocs.append(spec_copy)
                alloc_list = new_allocs or alloc_list
            params["allocations"] = alloc_list
            params["auto_detect_block_metrics"] = False
            tool_copy["params"] = params
            notes.append(
                f"Planner review: expand_grouped_block allocates {target_metric!r} via {method!r}"
                + (f" (weight {weight_col!r})" if method == "by_weight" and weight_col else "")
                + "."
            )

        elif norm == "transform.allocate_block_metric":
            mc = str(params.get("metric_col") or "").strip()
            if mc.lower() == target_metric.lower():
                params["method"] = method
                if method == "by_weight" and weight_col:
                    params["weight_col"] = weight_col
                else:
                    params.pop("weight_col", None)
                tool_copy["params"] = params
                notes.append(
                    f"Planner review: allocate_block_metric for {target_metric!r} uses {method!r}"
                    + (f" (weight {weight_col!r})" if method == "by_weight" and weight_col else "")
                    + "."
                )

        patched.append(tool_copy)
    return patched, notes


def apply_resolved_approvals_to_plan(
    plan: ExtractionPlan,
    context_packet: Optional[Dict[str, Any]],
) -> ExtractionPlan:
    """Adjust tool_calls to honour resolved planner review decisions."""
    if not isinstance(plan, ExtractionPlan):
        return plan

    cp = context_packet or {}
    items = list(cp.get("resolved_planner_decisions") or [])
    if not items:
        return plan

    approved = list(cp.get("approved_mappings") or [])
    dupes = dict(cp.get("duplicate_target_mappings") or duplicate_target_sources(approved))
    tools = [dict(t) for t in (plan.tool_calls or []) if isinstance(t, dict)]
    present_sources: Set[str] = set()
    for row in approved:
        if isinstance(row, dict):
            sc = str(row.get("source_column") or row.get("column_name") or "").strip()
            if sc:
                present_sources.add(sc)

    notes: List[str] = list(plan.reasoning.split(". ") if plan.reasoning else [])

    for item in items:
        alloc = infer_block_metric_allocation(item)
        if alloc:
            target = str(item.get("target_column") or item.get("target") or "spends").strip()
            tools, alloc_notes = _patch_block_metric_allocation_tools(
                tools, target, alloc, context_packet=cp
            )
            notes.extend(alloc_notes)
            continue

        strategy = infer_metric_target_strategy(item)
        if not strategy:
            continue
        target = str(item.get("target_column") or item.get("target") or "spends").strip()
        sources = list(dupes.get(target) or [])
        if not sources:
            for tgt, srcs in dupes.items():
                if tgt.lower() == target.lower():
                    sources = list(srcs)
                    target = tgt
                    break
        if strategy == "single_column":
            tools, strip_notes = _strip_invalid_calculate_steps(tools, present_sources)
            notes.extend(strip_notes)
        elif strategy == "combine_sources" and len(sources) >= 2:
            tools = _upsert_combine_calculate(tools, target, sources)
            notes.append(
                f"Planner review: transform.calculate sums {', '.join(sources)} → {target}."
            )

    for step_idx, tool in enumerate(tools, start=1):
        tool["step"] = step_idx
    plan.tool_calls = tools
    if notes:
        plan.reasoning = ". ".join(dict.fromkeys(n.strip() for n in notes if n.strip()))
    return plan


def format_resolved_decisions_for_prompt(
    resolved_items: List[Dict[str, Any]],
) -> str:
    """Human-readable block for planner / replanner prompts."""
    lines: List[str] = []
    for idx, item in enumerate(resolved_items or [], start=1):
        if not isinstance(item, dict):
            continue
        q = str(item.get("question") or item.get("summary") or item.get("description") or "").strip()
        choice = str(item.get("analyst_confirm") or "").strip()
        label = _match_choice_label(item, choice) if choice else ""
        note = str(item.get("resolution_note") or "").strip()
        target = str(item.get("target_column") or item.get("target") or "").strip()
        strategy = infer_metric_target_strategy(item)
        lines.append(f"{idx}. [{target or 'general'}] {q}")
        if label:
            lines.append(f"   → Analyst chose: **{label}** (`{choice}`)")
        elif choice:
            lines.append(f"   → Analyst chose: `{choice}`")
        if strategy:
            lines.append(f"   → Interpreted strategy: `{strategy}`")
        alloc = infer_block_metric_allocation(item)
        if alloc:
            lines.append(
                f"   → Block metric allocation: `{alloc.get('method')}`"
                + (
                    f" (weight `{alloc.get('weight_col')}`)"
                    if alloc.get("weight_col")
                    else ""
                )
            )
        if note:
            lines.append(f"   → Note: {note}")
    if not lines:
        return ""
    return (
        "\n## Analyst decisions from planner review (MANDATORY — do not contradict)\n"
        "These answers were collected in the **Decisions needed** questionnaire. "
        "Your tool_calls must follow them; do not re-ask the same question.\n"
        + "\n".join(lines)
        + "\n"
    )
