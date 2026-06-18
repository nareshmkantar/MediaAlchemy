import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import pandas as pd
from pandas.api.types import (
    is_bool_dtype,
    is_datetime64_any_dtype,
    is_numeric_dtype,
    is_string_dtype,
)

from .base import load_prompt_from_file
from .llm_handler import LLMCallWrapper, RetryConfig, robust_json_parse
from .scoped_source import (
    build_scoped_source,
    _is_probable_subheader_row,
    _multi_level_header_candidate,
    _row_text_numeric_filled,
)

logger = logging.getLogger(__name__)


class StructureDemarcator:
    """
    Deterministic-first spreadsheet demarcation.

    Python owns candidate block geometry (connected components + bbox), drops
    strictly contained thin/small fragments inside a larger block's rectangle, and
    infers header rows only for Main Data blocks. AI classifies ambiguous
    candidates and may suggest a header row for Main Data only; the UI approves.
    """

    # Heuristic confidence from Python (see _heuristic_block_role): above this, skip LLM.
    HEURISTIC_TRUST_THRESHOLD = 0.8
    # Below this, keep Python labels but flag the block for human review; do not call LLM.
    HUMAN_REVIEW_THRESHOLD = 0.4

    def __init__(self, llm_client, retry_config: RetryConfig = None):
        self.llm_wrapper = LLMCallWrapper(
            llm_client,
            retry_config=retry_config or RetryConfig(max_attempts=3),
        )
        self.llm_client = llm_client

        self.SYSTEM_PROMPT, self.PROMPT_VERSION = load_prompt_from_file("structure_demarcator")
        if not self.SYSTEM_PROMPT:
            self.SYSTEM_PROMPT = (
                "You classify spreadsheet block candidates as Main Data, Metadata, "
                "Footnote, Comment, or Noise, and may suggest a header row."
            )
            self.PROMPT_VERSION = "0.0"

    async def propose_demarcation(
        self,
        grid_df: pd.DataFrame,
        visual_patterns: Dict = None,
        use_ai_classification: bool = True,
    ) -> Dict:
        """
        Build deterministic candidate blocks from the sheet values and then
        optionally ask the LLM to classify those candidates.
        """
        try:
            normalized_df = self._normalize_dataframe(grid_df)
            candidate_blocks = self.extract_candidate_blocks(normalized_df)
            gating_counts = self._tag_confidence_bands(candidate_blocks)

            classified_blocks, llm_error, gating_meta = self._classify_blocks(
                candidate_blocks,
                visual_patterns=visual_patterns,
                use_ai=use_ai_classification,
            )
            classified_blocks = self._absorb_header_metadata_into_main(classified_blocks)
            self._strip_headers_for_non_main(classified_blocks)

            confidence_gating = {
                "trust_threshold": self.HEURISTIC_TRUST_THRESHOLD,
                "review_threshold": self.HUMAN_REVIEW_THRESHOLD,
                "counts": gating_counts,
                **gating_meta,
            }

            if not use_ai_classification:
                detection_mode = "python_only"
                llm_status = "python_only"
                llm_error = None
            else:
                detection_mode = "python_candidates_confidence_gated_ai"
                if llm_error:
                    llm_status = "fallback_heuristics"
                elif gating_meta.get("llm_skipped_no_ambiguous"):
                    llm_status = "confidence_gated_no_llm"
                elif gating_meta.get("llm_candidate_count", 0) > 0:
                    llm_status = "confidence_gated_classified"
                else:
                    llm_status = "classified"

            return {
                "blocks": classified_blocks,
                "detection_mode": detection_mode,
                "overall_assessment": self._build_overall_assessment(
                    classified_blocks,
                    llm_error=llm_error,
                    ai_used=use_ai_classification,
                    confidence_gating=confidence_gating,
                ),
                "python_summary": self._build_python_summary(classified_blocks),
                "confidence": round(self._overall_confidence(classified_blocks), 3),
                "llm_status": llm_status,
                "llm_error": llm_error,
                "confidence_gating": confidence_gating,
            }
        except Exception as e:
            logger.exception("Demarcation proposal failed: %s", e)
            return {
                "blocks": [],
                "error": str(e),
                "confidence": 0.0,
                "llm_status": "failed",
            }

    def run_python_demarcation_phases(self, grid_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """Extract blocks, tag confidence bands, absorb header metadata — no LLM."""
        normalized_df = self._normalize_dataframe(grid_df)
        candidate_blocks = self.extract_candidate_blocks(normalized_df)
        self._tag_confidence_bands(candidate_blocks)
        merged = self._absorb_header_metadata_into_main(candidate_blocks)
        self._strip_headers_for_non_main(merged)
        return merged

    @staticmethod
    def _composite_block_id(registry_index: int, block_id: Any) -> str:
        return f"{int(registry_index)}#{block_id}"

    def _gating_counts_from_blocks(self, blocks: List[Dict[str, Any]]) -> Dict[str, int]:
        counts = {"high": 0, "ambiguous": 0, "low": 0}
        for block in blocks:
            band = block.get("confidence_band")
            if band in counts:
                counts[band] += 1
        return counts

    def _assemble_sheet_proposal(
        self,
        blocks: List[Dict[str, Any]],
        *,
        visual_patterns: Optional[Dict[str, Any]],
        sheet_name: str,
        filename: str,
        source_id: Optional[str],
        use_ai_classification: bool,
        llm_error: Optional[str],
        gating_meta: Dict[str, Any],
        batch_note: str = "",
    ) -> Dict[str, Any]:
        gating_counts = self._gating_counts_from_blocks(blocks)
        confidence_gating = {
            "trust_threshold": self.HEURISTIC_TRUST_THRESHOLD,
            "review_threshold": self.HUMAN_REVIEW_THRESHOLD,
            "counts": gating_counts,
            **gating_meta,
        }
        if not use_ai_classification:
            detection_mode = "python_only"
            llm_status = "python_only"
            llm_error = None
        else:
            detection_mode = "python_candidates_confidence_gated_ai"
            if llm_error:
                llm_status = "fallback_heuristics"
            elif gating_meta.get("llm_skipped_no_ambiguous"):
                llm_status = "confidence_gated_no_llm"
            elif gating_meta.get("llm_candidate_count", 0) > 0:
                llm_status = "confidence_gated_classified"
            else:
                llm_status = "classified"

        assessment = self._build_overall_assessment(
            blocks,
            llm_error=llm_error,
            ai_used=use_ai_classification,
            confidence_gating=confidence_gating,
        )
        if batch_note:
            assessment = f"{assessment} {batch_note}"

        self._strip_headers_for_non_main(blocks)

        return {
            "blocks": blocks,
            "detection_mode": detection_mode,
            "overall_assessment": assessment,
            "python_summary": self._build_python_summary(blocks),
            "confidence": round(self._overall_confidence(blocks), 3),
            "llm_status": llm_status,
            "llm_error": llm_error,
            "confidence_gating": confidence_gating,
            "filename": filename,
            "sheet_name": sheet_name,
            "source_id": source_id,
        }

    async def batch_propose_demarcation(
        self,
        sheets: List[Dict[str, Any]],
        use_ai_classification: bool = True,
    ) -> Tuple[Dict[str, Dict[str, Any]], Optional[str], Dict[str, Any]]:
        """
        Run Python demarcation for many sheets, then a single LLM call for all
        ambiguous blocks across sheets. `sheets` items must include:
        registry_index, source_id, sheet_name, filename, grid_df, visual_patterns.
        Returns (proposals_by_source_id, shared_llm_error, batch_meta).
        """
        batch_meta: Dict[str, Any] = {
            "source_count": len(sheets),
            "ambiguous_total": 0,
            "batch_llm_called": False,
        }
        proposals_by_id: Dict[str, Dict[str, Any]] = {}

        working: List[Dict[str, Any]] = []
        for row in sheets:
            idx = int(row["registry_index"])
            try:
                blocks = self.run_python_demarcation_phases(row["grid_df"])
            except Exception as exc:
                logger.warning("Python demarcation failed for source %s: %s", row.get("source_id"), exc)
                blocks = []
            working.append(
                {
                    "registry_index": idx,
                    "source_id": row.get("source_id"),
                    "sheet_name": row.get("sheet_name"),
                    "filename": row.get("filename"),
                    "blocks": blocks,
                    "visual_patterns": row.get("visual_patterns") or {},
                }
            )

        ambiguous_synthetic: List[Dict[str, Any]] = []
        synthetic_ids: Set[str] = set()
        for row in working:
            ridx = row["registry_index"]
            for block in row["blocks"]:
                if block.get("confidence_band") != "ambiguous":
                    continue
                bid = block.get("id")
                cid = self._composite_block_id(ridx, bid)
                synthetic = dict(block)
                synthetic["id"] = cid
                ambiguous_synthetic.append(synthetic)
                synthetic_ids.add(cid)

        batch_meta["ambiguous_total"] = len(synthetic_ids)
        shared_llm_error: Optional[str] = None

        if (
            use_ai_classification
            and self.llm_client
            and ambiguous_synthetic
        ):
            batch_meta["batch_llm_called"] = True
            payload: Dict[str, Any] = {
                "instructions": {
                    "do_not_change_coordinates": True,
                    "classify_each_candidate": True,
                    "allowed_categories": ["Main Data", "Metadata", "Footnote", "Comment", "Noise"],
                    "allowed_suggestions": ["Keep", "Discard", "Use as Context"],
                    "note": (
                        "Multi-sheet batch: each candidate id is 'sheetIndex#blockId'. "
                        "Classify every listed candidate; do not merge sheets."
                    ),
                },
                "candidates": [],
            }
            for block in ambiguous_synthetic:
                reg_idx = int(str(block["id"]).split("#", 1)[0])
                src_row = next((w for w in working if w["registry_index"] == reg_idx), working[0] if working else {})
                payload["candidates"].append(
                    {
                        "id": block.get("id"),
                        "source_context": {
                            "sheet_name": src_row.get("sheet_name"),
                            "file_name": src_row.get("filename"),
                            "source_id": src_row.get("source_id"),
                        },
                        "coordinates": block.get("coordinates"),
                        "python_role": ((block.get("review_basis") or {}).get("python_role")),
                        "python_suggestion": ((block.get("review_basis") or {}).get("python_suggestion")),
                        "python_summary": ((block.get("review_basis") or {}).get("python_summary")),
                        "python_detection": self._compact_python_detection_for_llm(block.get("python_detection")),
                        "heuristic_confidence": block.get("heuristic_confidence"),
                    }
                )

            try:
                user_prompt = (
                    "Classify uncertain spreadsheet blocks from multiple sheets (single batch). "
                    "Each id is 'sheetIndex#blockId'. Do not change coordinates.\n\n"
                    f"{json.dumps(payload, indent=2)}"
                )
                full_prompt = f"{self.SYSTEM_PROMPT}\n\n{user_prompt}"
                response = self.llm_wrapper.generate_content(
                    full_prompt,
                    generation_config={"max_output_tokens": 8192, "temperature": 0.0},
                )
                parsed = robust_json_parse(response.text)
                ai_blocks = parsed.get("blocks") or parsed.get("block_reviews") or []
                if not isinstance(ai_blocks, list):
                    raise ValueError("AI demarcation response did not include a block list.")
                merged_syn = self._merge_ai_classification(
                    ambiguous_synthetic,
                    ai_blocks,
                    llm_candidate_ids=set(synthetic_ids),
                )
                syn_by_id = {str(b.get("id")): b for b in merged_syn}
            except Exception as exc:
                logger.warning("Batch AI block classification failed: %s", exc)
                shared_llm_error = str(exc)
                syn_by_id = {}
                for block in ambiguous_synthetic:
                    block.setdefault("python_detection", {}).setdefault("cleanup_actions", []).append(
                        "llm_fallback_to_python"
                    )
        else:
            syn_by_id = {}
            if use_ai_classification and not ambiguous_synthetic:
                shared_llm_error = None
            elif use_ai_classification and not self.llm_client:
                shared_llm_error = "No LLM client configured; using heuristic block roles."

        for row in working:
            ridx = row["registry_index"]
            sid = row.get("source_id") or f"source_{ridx}"
            new_blocks: List[Dict[str, Any]] = []
            sheet_ambiguous = sum(1 for b in row["blocks"] if b.get("confidence_band") == "ambiguous")
            gating_meta = {
                "llm_skipped_no_ambiguous": sheet_ambiguous == 0,
                "llm_candidate_count": sheet_ambiguous,
            }
            for block in row["blocks"]:
                if block.get("confidence_band") != "ambiguous":
                    new_blocks.append(dict(block))
                    continue
                cid = self._composite_block_id(ridx, block.get("id"))
                src = syn_by_id.get(cid)
                if src:
                    mb = dict(src)
                    mb["id"] = block["id"]
                    new_blocks.append(mb)
                else:
                    nb = dict(block)
                    if shared_llm_error:
                        nb.setdefault("python_detection", {}).setdefault("cleanup_actions", []).append(
                            "llm_fallback_to_python"
                        )
                    new_blocks.append(nb)

            batch_note = (
                f"[Batch demarcation: {batch_meta['source_count']} source(s); "
                f"{batch_meta['ambiguous_total']} ambiguous block(s) across sheets in one LLM call.]"
            )
            proposals_by_id[str(sid)] = self._assemble_sheet_proposal(
                new_blocks,
                visual_patterns=row["visual_patterns"],
                sheet_name=row.get("sheet_name") or "",
                filename=row.get("filename") or "",
                source_id=row.get("source_id"),
                use_ai_classification=use_ai_classification,
                llm_error=shared_llm_error,
                gating_meta=gating_meta,
                batch_note=batch_note,
            )

        return proposals_by_id, shared_llm_error, batch_meta

    def extract_candidate_blocks(self, grid_df: pd.DataFrame) -> List[Dict[str, Any]]:
        grid_df = self._normalize_dataframe(grid_df)
        mask = self._build_non_empty_mask(grid_df)
        components = self._connected_components(mask)

        blocks: List[Dict[str, Any]] = []
        for index, component in enumerate(components, start=1):
            block = self._build_candidate_block(grid_df, component, index=index)
            blocks.append(block)

        blocks = self._apply_conservative_cleanup(blocks)
        blocks = self._dedupe_bbox_contained_fragments(blocks)
        return blocks

    def _normalize_dataframe(self, grid_df: pd.DataFrame) -> pd.DataFrame:
        if grid_df is None:
            return pd.DataFrame()
        if isinstance(grid_df, pd.DataFrame):
            return grid_df.copy()
        return pd.DataFrame(grid_df)

    @staticmethod
    def _nonempty_mask_object_1d(col_vals: np.ndarray) -> np.ndarray:
        """Per-cell semantics matching legacy mask: None → empty; str → strip; else pd.notna."""
        n = int(len(col_vals))
        out = np.empty(n, dtype=np.bool_)
        for i in range(n):
            v = col_vals[i]
            if v is None:
                out[i] = False
            elif isinstance(v, str):
                out[i] = bool(v.strip())
            else:
                out[i] = bool(pd.notna(v))
        return out

    def _build_non_empty_mask(self, grid_df: pd.DataFrame) -> List[List[bool]]:
        if grid_df.empty:
            return []

        nrows, ncols = len(grid_df), len(grid_df.columns)
        mask_arr = np.zeros((nrows, ncols), dtype=np.bool_)

        for j, col in enumerate(grid_df.columns):
            ser = grid_df.iloc[:, j]

            if is_string_dtype(ser.dtype):
                sn = ser.astype("string", copy=False)
                mask_arr[:, j] = np.asarray(sn.notna() & sn.str.strip().ne(""), dtype=np.bool_)
                continue

            if is_numeric_dtype(ser.dtype) or is_bool_dtype(ser.dtype):
                vals = ser.to_numpy(copy=False)
                mask_arr[:, j] = np.asarray(~pd.isna(vals), dtype=np.bool_)
                continue

            if is_datetime64_any_dtype(ser.dtype):
                vals = ser.to_numpy(copy=False)
                mask_arr[:, j] = np.asarray(~pd.isna(vals), dtype=np.bool_)
                continue

            if isinstance(ser.dtype, pd.CategoricalDtype):
                vals = ser.astype(object).to_numpy(copy=False)
                mask_arr[:, j] = self._nonempty_mask_object_1d(vals)
                continue

            vals = ser.to_numpy(dtype=object, copy=False)
            mask_arr[:, j] = self._nonempty_mask_object_1d(vals)

        return mask_arr.tolist()

    def _connected_components(self, mask: List[List[bool]]) -> List[List[Tuple[int, int]]]:
        if not mask:
            return []

        rows = len(mask)
        cols = len(mask[0]) if rows else 0
        visited = [[False for _ in range(cols)] for _ in range(rows)]
        components: List[List[Tuple[int, int]]] = []

        for row in range(rows):
            for col in range(cols):
                if not mask[row][col] or visited[row][col]:
                    continue

                stack = [(row, col)]
                visited[row][col] = True
                component: List[Tuple[int, int]] = []

                while stack:
                    current_row, current_col = stack.pop()
                    component.append((current_row, current_col))

                    for next_row, next_col in (
                        (current_row - 1, current_col),
                        (current_row + 1, current_col),
                        (current_row, current_col - 1),
                        (current_row, current_col + 1),
                    ):
                        if (
                            0 <= next_row < rows
                            and 0 <= next_col < cols
                            and mask[next_row][next_col]
                            and not visited[next_row][next_col]
                        ):
                            visited[next_row][next_col] = True
                            stack.append((next_row, next_col))

                components.append(component)

        components.sort(
            key=lambda component: (
                min(cell[0] for cell in component),
                min(cell[1] for cell in component),
            )
        )
        return components

    def _build_candidate_block(
        self,
        grid_df: pd.DataFrame,
        component: List[Tuple[int, int]],
        index: int,
    ) -> Dict[str, Any]:
        rows = [row for row, _ in component]
        cols = [col for _, col in component]
        start_row, end_row = min(rows), max(rows)
        start_col, end_col = min(cols), max(cols)
        block_df = grid_df.iloc[start_row:end_row + 1, start_col:end_col + 1]

        stats = self._summarize_block(
            block_df, start_row=start_row, start_col=start_col, component=component, infer_header=False
        )
        category, suggestion, summary = self._heuristic_block_role(stats)
        mh_coords: Dict[str, Any] = {}
        multi_header_meta: Dict[str, Any] = {}
        if category == "Main Data":
            header_row_candidate, header_candidates = self._infer_header_row(block_df, start_row=start_row)
            stats["header_row_candidate"] = header_row_candidate
            stats["header_candidates"] = header_candidates
            stats["heuristic_reason"] = (
                f"Python detected a {stats['row_count']}x{stats['col_count']} block with fill ratio {stats['fill_ratio']:.2f}, "
                f"{stats['numeric_cells']} numeric cells, and header candidate row {header_row_candidate + 1}."
            )
            mh_coords = self._infer_multi_header_coordinates(
                block_df, block_start_row=start_row, header_row_abs=header_row_candidate
            )
            multi_header_meta = {
                "multi_header_suggested": bool(mh_coords),
                "header_row_end_candidate": mh_coords.get("header_row_end"),
            }

        coords: Dict[str, Any] = {
            "start_row": start_row,
            "end_row": end_row,
            "start_col": start_col,
            "end_col": end_col,
            "header_row": stats.get("header_row_candidate"),
        }
        coords.update(mh_coords)

        return {
            "id": f"block_{index}",
            "name": self._build_block_name(index=index, category=category, stats=stats),
            "category": category,
            "coordinates": coords,
            "summary": summary,
            "ai_suggestion": suggestion,
            "suggestion_reason": stats["heuristic_reason"],
            "confidence": stats["heuristic_confidence"],
            "python_detection": {
                "non_empty_cells": stats["non_empty_cells"],
                "fill_ratio": stats["fill_ratio"],
                "row_count": stats["row_count"],
                "col_count": stats["col_count"],
                "text_cells": stats["text_cells"],
                "numeric_cells": stats["numeric_cells"],
                "header_row_candidate": stats.get("header_row_candidate"),
                "header_candidates": stats["header_candidates"],
                "top_rows": stats["top_rows"],
                "cleanup_actions": [],
                **multi_header_meta,
            },
            "review_basis": {
                "python_role": category,
                "python_suggestion": suggestion,
                "python_summary": summary,
            },
        }

    def _summarize_block(
        self,
        block_df: pd.DataFrame,
        start_row: int,
        start_col: int,
        component: List[Tuple[int, int]],
        *,
        infer_header: bool = True,
    ) -> Dict[str, Any]:
        row_count = len(block_df)
        col_count = len(block_df.columns)
        area = max(row_count * col_count, 1)
        non_empty_cells = len(component)

        text_cells = 0
        numeric_cells = 0
        non_empty_per_row: List[int] = []
        top_rows: List[Dict[str, Any]] = []

        for row_idx in range(row_count):
            values = list(block_df.iloc[row_idx])
            non_empty_values = [value for value in values if self._is_non_empty(value)]
            non_empty_per_row.append(len(non_empty_values))
            if row_idx < 5:
                top_rows.append(
                    {
                        "row_index": start_row + row_idx,
                        "values": [self._serialize_cell(value) for value in values],
                    }
                )
            for value in non_empty_values:
                if self._is_numeric(value):
                    numeric_cells += 1
                else:
                    text_cells += 1

        fill_ratio = round(non_empty_cells / area, 3)
        if infer_header:
            header_row_candidate, header_candidates = self._infer_header_row(block_df, start_row=start_row)
            heuristic_reason = (
                f"Python detected a {row_count}x{col_count} block with fill ratio {fill_ratio:.2f}, "
                f"{numeric_cells} numeric cells, and header candidate row {header_row_candidate + 1}."
            )
        else:
            header_row_candidate, header_candidates = None, []
            heuristic_reason = (
                f"Python detected a {row_count}x{col_count} block with fill ratio {fill_ratio:.2f}, "
                f"{numeric_cells} numeric cells. Header rows are inferred only for Main Data blocks."
            )

        heuristic_confidence = 0.55
        if row_count >= 3 and col_count >= 2:
            heuristic_confidence += 0.15
        if fill_ratio >= 0.45:
            heuristic_confidence += 0.1
        if numeric_cells >= 2:
            heuristic_confidence += 0.1
        heuristic_confidence = min(round(heuristic_confidence, 3), 0.95)

        return {
            "row_count": row_count,
            "col_count": col_count,
            "non_empty_cells": non_empty_cells,
            "fill_ratio": fill_ratio,
            "numeric_cells": numeric_cells,
            "text_cells": text_cells,
            "non_empty_per_row": non_empty_per_row,
            "top_rows": top_rows,
            "header_row_candidate": header_row_candidate,
            "header_candidates": header_candidates,
            "heuristic_reason": heuristic_reason,
            "heuristic_confidence": heuristic_confidence,
        }

    def _infer_header_row(self, block_df: pd.DataFrame, start_row: int) -> Tuple[int, List[Dict[str, Any]]]:
        top_window = min(len(block_df), 5)
        candidate_scores: List[Dict[str, Any]] = []

        for rel_row in range(top_window):
            values = list(block_df.iloc[rel_row])
            non_empty_values = [value for value in values if self._is_non_empty(value)]
            non_empty_count = len(non_empty_values)
            if non_empty_count == 0:
                continue

            text_count = sum(1 for value in non_empty_values if not self._is_numeric(value))
            numeric_count = sum(1 for value in non_empty_values if self._is_numeric(value))
            next_row_non_empty = 0
            if rel_row + 1 < len(block_df):
                next_row_values = list(block_df.iloc[rel_row + 1])
                next_row_non_empty = sum(1 for value in next_row_values if self._is_non_empty(value))

            score = 0.0
            score += min(non_empty_count, 6) * 2.0
            score += text_count * 1.5
            score -= numeric_count * 2.0
            if next_row_non_empty >= max(non_empty_count - 1, 1):
                score += 2.0
            if non_empty_count == 1 and text_count == 1 and rel_row == 0 and len(block_df) > 1:
                score -= 4.0

            candidate_scores.append(
                {
                    "row_index": start_row + rel_row,
                    "score": round(score, 3),
                    "non_empty_count": non_empty_count,
                    "text_count": text_count,
                    "numeric_count": numeric_count,
                }
            )

        candidate_scores.sort(key=lambda item: (-item["score"], item["row_index"]))
        if not candidate_scores:
            return start_row, []

        header_row_candidate = candidate_scores[0]["row_index"]
        return header_row_candidate, candidate_scores[:3]

    @staticmethod
    def _infer_multi_header_coordinates(
        block_df: pd.DataFrame,
        block_start_row: int,
        header_row_abs: Optional[int],
    ) -> Dict[str, Any]:
        """
        Set ``header_mode`` / ``header_row_end`` on demarcation blocks when a two-row header band is likely.

        Matches ``scoped_source.load_scoped_dataframe`` heuristics where possible (strict), plus a relaxed
        rule: top row is label-heavy and the next row passes the sub-header profile (typical merged Excel).
        """
        if header_row_abs is None:
            return {}
        rel = int(header_row_abs) - int(block_start_row)
        if rel < 0 or rel >= len(block_df) - 1:
            return {}
        width = len(block_df.columns)
        row_top = block_df.iloc[rel]
        row_sub = block_df.iloc[rel + 1]
        strict = _multi_level_header_candidate(row_top) and _is_probable_subheader_row(row_sub, width)
        if not strict:
            t_top, n_top, f_top = _row_text_numeric_filled(row_top)
            relaxed = (
                f_top > 0
                and t_top >= n_top
                and _is_probable_subheader_row(row_sub, width)
            )
            if not relaxed:
                return {}
        return {
            "header_mode": "multi",
            "header_row_end": int(header_row_abs) + 1,
        }

    def _heuristic_block_role(self, stats: Dict[str, Any]) -> Tuple[str, str, str]:
        row_count = stats["row_count"]
        col_count = stats["col_count"]
        non_empty_cells = stats["non_empty_cells"]
        fill_ratio = stats["fill_ratio"]
        numeric_cells = stats["numeric_cells"]
        text_cells = stats["text_cells"]

        if non_empty_cells <= 2 and row_count <= 2 and col_count <= 2:
            return "Noise", "Discard", "Tiny isolated block with too little structure to be useful."

        if row_count >= 3 and col_count >= 2 and fill_ratio >= 0.35 and non_empty_cells >= max(6, col_count * 2):
            return "Main Data", "Keep", "Dense candidate table with repeated rows and a likely header row."

        if text_cells >= numeric_cells and row_count <= 4:
            return "Metadata", "Use as Context", "Compact text-heavy block that looks like contextual metadata."

        return "Comment", "Discard", "Loose block without enough repeated structure for primary processing."

    def _build_block_name(self, index: int, category: str, stats: Dict[str, Any]) -> str:
        if category == "Main Data":
            return f"Candidate Data Block {index}"
        if category == "Metadata":
            return f"Candidate Context Block {index}"
        if category == "Noise":
            return f"Candidate Noise Block {index}"
        return f"Candidate Block {index}"

    def _apply_conservative_cleanup(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not blocks:
            return blocks

        for block in blocks:
            coords = block.get("coordinates", {})
            python_detection = block.setdefault("python_detection", {})
            cleanup_actions = python_detection.setdefault("cleanup_actions", [])
            row_count = python_detection.get("row_count", 0)
            col_count = python_detection.get("col_count", 0)
            non_empty_cells = python_detection.get("non_empty_cells", 0)

            if non_empty_cells <= 2 and row_count <= 2 and col_count <= 2:
                block["category"] = "Noise"
                block["ai_suggestion"] = "Discard"
                block["suggestion_reason"] = "Python marked this as tiny isolated noise."
                cleanup_actions.append("marked_tiny_isolated_noise")

            header_candidate = python_detection.get("header_row_candidate")
            cat = str(block.get("category", "")).strip().lower()
            if cat == "main data" and header_candidate is not None:
                coords["header_row"] = int(header_candidate)
            else:
                coords.pop("header_row", None)

        return blocks

    @staticmethod
    def _bbox_area(coords: Dict[str, Any]) -> int:
        sr = int(coords.get("start_row", 0))
        er = int(coords.get("end_row", 0))
        sc = int(coords.get("start_col", 0))
        ec = int(coords.get("end_col", 0))
        return max(er - sr + 1, 0) * max(ec - sc + 1, 0)

    @staticmethod
    def _bbox_strictly_contains(outer: Dict[str, Any], inner: Dict[str, Any]) -> bool:
        if StructureDemarcator._bbox_area(outer) <= StructureDemarcator._bbox_area(inner):
            return False
        return (
            int(inner.get("start_row", 0)) >= int(outer.get("start_row", 0))
            and int(inner.get("end_row", 0)) <= int(outer.get("end_row", 0))
            and int(inner.get("start_col", 0)) >= int(outer.get("start_col", 0))
            and int(inner.get("end_col", 0)) <= int(outer.get("end_col", 0))
        )

    def _dedupe_bbox_contained_fragments(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Drop small disconnected components whose bounding box lies strictly inside a larger
        block's bbox (e.g. a marginal note column H4:H6 inside a table drawn as A1:H10).
        """
        if len(blocks) < 2:
            return blocks

        frag_ratio = 0.48
        to_remove: Set[int] = set()

        for j, inner in enumerate(blocks):
            inner_cd = inner.get("coordinates") or {}
            inner_cells = int((inner.get("python_detection") or {}).get("non_empty_cells") or 0)
            ir = int(inner_cd.get("end_row", 0)) - int(inner_cd.get("start_row", 0)) + 1
            ic = int(inner_cd.get("end_col", 0)) - int(inner_cd.get("start_col", 0)) + 1
            thin_strip = min(ir, ic) == 1 and max(ir, ic) <= 30

            for i, outer in enumerate(blocks):
                if i == j:
                    continue
                outer_cd = outer.get("coordinates") or {}
                if not self._bbox_strictly_contains(outer_cd, inner_cd):
                    continue
                outer_cells = int((outer.get("python_detection") or {}).get("non_empty_cells") or 0)
                if outer_cells < 1:
                    continue
                ratio = inner_cells / outer_cells
                if ratio < frag_ratio or thin_strip:
                    to_remove.add(j)
                    outer.setdefault("python_detection", {}).setdefault("cleanup_actions", []).append(
                        f"suppressed_contained_fragment:{inner.get('id')}"
                    )
                    inner.setdefault("python_detection", {}).setdefault("cleanup_actions", []).append(
                        "dropped_bbox_contained_fragment"
                    )
                    break

        filtered = [b for idx, b in enumerate(blocks) if idx not in to_remove]
        return self._renumber_candidate_blocks(filtered)

    def _renumber_candidate_blocks(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for index, block in enumerate(blocks, start=1):
            b = dict(block)
            cat = b.get("category") or "Comment"
            det = b.get("python_detection") or {}
            b["id"] = f"block_{index}"
            b["name"] = self._build_block_name(
                index=index,
                category=cat,
                stats={
                    "row_count": det.get("row_count", 0),
                    "col_count": det.get("col_count", 0),
                },
            )
            out.append(b)
        return out

    def _strip_headers_for_non_main(self, blocks: List[Dict[str, Any]]) -> None:
        """Header row applies to Main Data only; clear for Metadata, Noise, Footnote, Comment."""
        for block in blocks:
            cat = str(block.get("category", "")).strip().lower()
            if cat == "main data":
                continue
            coords = block.setdefault("coordinates", {})
            coords.pop("header_row", None)
            pdet = block.setdefault("python_detection", {})
            pdet["header_row_candidate"] = None
            pdet["header_candidates"] = []
            review = block.get("ai_review")
            if isinstance(review, dict):
                review["header_row"] = None

    def _tag_confidence_bands(self, blocks: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        Tag each block using heuristic confidence:
        - > HEURISTIC_TRUST_THRESHOLD: trust Python, no LLM.
        - HUMAN_REVIEW_THRESHOLD .. HEURISTIC_TRUST_THRESHOLD: ambiguous → LLM when enabled.
        - < HUMAN_REVIEW_THRESHOLD: flag human_review_required, no LLM.
        """
        counts = {"high": 0, "ambiguous": 0, "low": 0}
        for block in blocks:
            c = self._safe_float(block.get("confidence"), default=0.5)
            block["heuristic_confidence"] = round(c, 3)
            if c > self.HEURISTIC_TRUST_THRESHOLD:
                block["confidence_band"] = "high"
                block["human_review_required"] = False
                block["classification_source"] = "heuristic_high"
                counts["high"] += 1
            elif c < self.HUMAN_REVIEW_THRESHOLD:
                block["confidence_band"] = "low"
                block["human_review_required"] = True
                block["classification_source"] = "heuristic_low"
                counts["low"] += 1
            else:
                block["confidence_band"] = "ambiguous"
                block["human_review_required"] = False
                block["classification_source"] = "heuristic_ambiguous"
                counts["ambiguous"] += 1
        return counts

    @staticmethod
    def _compact_python_detection_for_llm(det: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Strip heavy arrays from python_detection before JSON payload to the LLM."""
        if not det or not isinstance(det, dict):
            return {}
        compact: Dict[str, Any] = {}
        for key in (
            "non_empty_cells",
            "fill_ratio",
            "row_count",
            "col_count",
            "text_cells",
            "numeric_cells",
            "header_row_candidate",
            "header_candidates",
            "top_rows",
            "cleanup_actions",
        ):
            if key in det:
                compact[key] = det[key]
        per_row = det.get("non_empty_per_row")
        if isinstance(per_row, list) and per_row:
            compact["non_empty_per_row_head"] = per_row[:5]
            compact["non_empty_per_row_tail"] = per_row[-3:] if len(per_row) > 8 else []
        return compact

    def _classify_blocks(
        self,
        blocks: List[Dict[str, Any]],
        visual_patterns: Optional[Dict[str, Any]] = None,
        use_ai: bool = True,
    ) -> Tuple[List[Dict[str, Any]], Optional[str], Dict[str, Any]]:
        meta: Dict[str, Any] = {
            "llm_skipped_no_ambiguous": False,
            "llm_candidate_count": 0,
        }
        if not blocks:
            return [], None, meta
        if not use_ai:
            return blocks, None, meta
        if self.llm_client is None:
            return blocks, "No LLM client configured; using heuristic block roles.", meta

        ambiguous = [b for b in blocks if b.get("confidence_band") == "ambiguous"]
        ambiguous_ids: Set[str] = {str(b.get("id")) for b in ambiguous if b.get("id")}
        meta["llm_candidate_count"] = len(ambiguous_ids)

        if not ambiguous:
            meta["llm_skipped_no_ambiguous"] = True
            return blocks, None, meta

        payload = {
            "instructions": {
                "do_not_change_coordinates": True,
                "classify_each_candidate": True,
                "allowed_categories": ["Main Data", "Metadata", "Footnote", "Comment", "Noise"],
                "allowed_suggestions": ["Keep", "Discard", "Use as Context"],
                "note": (
                    "Only uncertain candidates are listed (heuristic confidence in the middle band). "
                    "High-confidence blocks already use Python labels; low-confidence blocks are flagged for human review."
                ),
            },
            "visual_patterns": visual_patterns or {},
            "candidates": [
                {
                    "id": block.get("id"),
                    "coordinates": block.get("coordinates"),
                    "python_role": ((block.get("review_basis") or {}).get("python_role")),
                    "python_suggestion": ((block.get("review_basis") or {}).get("python_suggestion")),
                    "python_summary": ((block.get("review_basis") or {}).get("python_summary")),
                    "python_detection": self._compact_python_detection_for_llm(block.get("python_detection")),
                    "heuristic_confidence": block.get("heuristic_confidence"),
                }
                for block in ambiguous
            ],
        }

        try:
            user_prompt = (
                "Classify the following uncertain spreadsheet block candidates (middle confidence band only). "
                "Do not invent new coordinates or merge/split blocks. "
                "Only for blocks you classify as Main Data, suggest the most likely header row within that block; "
                "for Metadata, Footnote, Comment, or Noise, do not suggest a header row.\n\n"
                f"{json.dumps(payload, indent=2)}"
            )
            full_prompt = f"{self.SYSTEM_PROMPT}\n\n{user_prompt}"

            response = self.llm_wrapper.generate_content(
                full_prompt,
                generation_config={"max_output_tokens": 4096, "temperature": 0.0},
            )
            parsed = robust_json_parse(response.text)
            ai_blocks = parsed.get("blocks") or parsed.get("block_reviews") or []
            if not isinstance(ai_blocks, list):
                raise ValueError("AI demarcation response did not include a block list.")
            return (
                self._merge_ai_classification(blocks, ai_blocks, llm_candidate_ids=ambiguous_ids),
                None,
                meta,
            )
        except Exception as exc:
            logger.warning("AI block classification failed, using heuristic defaults: %s", exc)
            for block in blocks:
                block.setdefault("python_detection", {}).setdefault("cleanup_actions", []).append("llm_fallback_to_python")
            return blocks, str(exc), meta

    def _merge_ai_classification(
        self,
        blocks: List[Dict[str, Any]],
        ai_blocks: List[Dict[str, Any]],
        llm_candidate_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        ai_by_id = {
            str(block.get("id")): block
            for block in ai_blocks
            if isinstance(block, dict) and block.get("id")
        }

        merged: List[Dict[str, Any]] = []
        for block in blocks:
            bid = str(block.get("id")) if block.get("id") is not None else ""
            ai_block = ai_by_id.get(bid, {})
            merged_block = dict(block)

            if llm_candidate_ids is None or bid in llm_candidate_ids:
                merged_block["category"] = str(ai_block.get("category") or block.get("category") or "Comment")
                merged_block["ai_suggestion"] = str(ai_block.get("ai_suggestion") or block.get("ai_suggestion") or "Discard")
                merged_block["summary"] = str(ai_block.get("summary") or block.get("summary") or "")
                merged_block["suggestion_reason"] = str(
                    ai_block.get("suggestion_reason")
                    or block.get("suggestion_reason")
                    or "No explanation provided."
                )
                merged_block["confidence"] = self._safe_float(
                    ai_block.get("confidence"),
                    default=block.get("confidence", 0.5),
                )

                ai_header_row = None
                ai_coords = ai_block.get("coordinates")
                if isinstance(ai_coords, dict):
                    ai_header_row = ai_coords.get("header_row")
                ai_header_row = ai_block.get("header_row", ai_header_row)
                merged_cat = str(merged_block.get("category", "")).strip().lower()
                if merged_cat == "main data" and ai_header_row is not None:
                    merged_block.setdefault("coordinates", {})["header_row"] = self._safe_int(
                        ai_header_row,
                        default=merged_block["coordinates"].get("header_row", merged_block["coordinates"].get("start_row", 0)),
                    )
                elif merged_cat != "main data":
                    merged_block.setdefault("coordinates", {}).pop("header_row", None)
                    mpd = merged_block.get("python_detection")
                    if isinstance(mpd, dict):
                        mpd["header_row_candidate"] = None
                        mpd["header_candidates"] = []

                if llm_candidate_ids is not None and bid in llm_candidate_ids:
                    merged_block["classification_source"] = "ai" if ai_block else "heuristic_fallback"
            else:
                merged_block["suggestion_reason"] = str(
                    block.get("suggestion_reason") or "No explanation provided."
                )

            hdr = merged_block.get("coordinates", {}).get("header_row")
            if str(merged_block.get("category", "")).strip().lower() != "main data":
                hdr = None
            merged_block["ai_review"] = {
                "category": merged_block["category"],
                "suggestion": merged_block["ai_suggestion"],
                "header_row": hdr,
                "confidence": merged_block["confidence"],
            }
            merged.append(merged_block)
        return merged

    def _build_python_summary(self, blocks: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "block_count": len(blocks),
            "main_candidates": sum(1 for block in blocks if block.get("category") == "Main Data"),
            "context_candidates": sum(1 for block in blocks if block.get("category") in {"Metadata", "Footnote"}),
            "noise_candidates": sum(1 for block in blocks if block.get("category") in {"Noise", "Comment"}),
        }

    def _build_overall_assessment(
        self,
        blocks: List[Dict[str, Any]],
        llm_error: Optional[str],
        ai_used: bool = True,
        confidence_gating: Optional[Dict[str, Any]] = None,
    ) -> str:
        summary = self._build_python_summary(blocks)
        assessment = (
            f"Python detected {summary['block_count']} candidate blocks: "
            f"{summary['main_candidates']} main-data, "
            f"{summary['context_candidates']} context, "
            f"{summary['noise_candidates']} noise/comment."
        )
        if not ai_used:
            cg = confidence_gating or {}
            counts = cg.get("counts") or {}
            band_note = ""
            if counts:
                band_note = (
                    f" Heuristic bands: {int(counts.get('high', 0) or 0)} high, "
                    f"{int(counts.get('ambiguous', 0) or 0)} uncertain, "
                    f"{int(counts.get('low', 0) or 0)} low (flagged for review)."
                )
            return f"{assessment} Block roles use Python heuristics only (AI off).{band_note}"

        cg = confidence_gating or {}
        counts = cg.get("counts") or {}
        high_n = int(counts.get("high", 0) or 0)
        amb_n = int(counts.get("ambiguous", 0) or 0)
        low_n = int(counts.get("low", 0) or 0)
        gated_parts = [
            f"Confidence gating: {high_n} high (>{self.HEURISTIC_TRUST_THRESHOLD}, Python only), "
            f"{amb_n} uncertain ([{self.HUMAN_REVIEW_THRESHOLD}, {self.HEURISTIC_TRUST_THRESHOLD}], AI when enabled), "
            f"{low_n} low (<{self.HUMAN_REVIEW_THRESHOLD}, flagged for human review, no AI).",
        ]
        if llm_error:
            return f"{assessment} {' '.join(gated_parts)} AI call failed; ambiguous blocks kept Python defaults."
        if cg.get("llm_skipped_no_ambiguous"):
            return f"{assessment} {' '.join(gated_parts)} No ambiguous blocks in the middle band, so the LLM was not called."
        if amb_n > 0 and cg.get("llm_candidate_count", 0) > 0:
            return f"{assessment} {' '.join(gated_parts)} The LLM refined only the uncertain band."
        return f"{assessment} {' '.join(gated_parts)}"

    def _overall_confidence(self, blocks: List[Dict[str, Any]]) -> float:
        if not blocks:
            return 0.0
        return sum(self._safe_float(block.get("confidence"), default=0.5) for block in blocks) / len(blocks)

    def _serialize_cell(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped[:120]
        try:
            if pd.isna(value):
                return None
        except (TypeError, ValueError):
            pass
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (int, float, bool)):
            return value
        return str(value)[:120]

    def _is_non_empty(self, value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        return bool(pd.notna(value))

    def _is_numeric(self, value: Any) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            if not text:
                return False
            try:
                float(text)
                return True
            except ValueError:
                return False
        return False

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _absorb_header_metadata_into_main(self, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        If a metadata/context block is a header row directly above Main Data with nearly
        the same column span, fold it into the Main Data block.
        """
        if not blocks:
            return blocks

        def _coords(block: Dict[str, Any]) -> Dict[str, int]:
            c = block.get("coordinates", {}) if isinstance(block, dict) else {}
            return {
                "start_row": int(c.get("start_row", 0)),
                "end_row": int(c.get("end_row", 0)),
                "start_col": int(c.get("start_col", 0)),
                "end_col": int(c.get("end_col", 0)),
            }

        def _is_main(block: Dict[str, Any]) -> bool:
            return str(block.get("category", "")).strip().lower() == "main data"

        def _is_metadata_like(block: Dict[str, Any]) -> bool:
            cat = str(block.get("category", "")).strip().lower()
            suggestion = str(block.get("ai_suggestion", "")).strip().lower()
            return cat == "metadata" or suggestion in {"use as context", "context"}

        def _overlap_ratio(a: Dict[str, int], b: Dict[str, int]) -> float:
            left = max(a["start_col"], b["start_col"])
            right = min(a["end_col"], b["end_col"])
            overlap = max(0, right - left + 1)
            width_a = max(1, a["end_col"] - a["start_col"] + 1)
            width_b = max(1, b["end_col"] - b["start_col"] + 1)
            return overlap / max(width_a, width_b)

        absorbed_ids = set()
        for main in blocks:
            if not _is_main(main):
                continue

            main_c = _coords(main)
            for meta in blocks:
                if meta is main or not _is_metadata_like(meta):
                    continue
                meta_c = _coords(meta)

                meta_height = meta_c["end_row"] - meta_c["start_row"] + 1
                contiguous_above = meta_c["end_row"] + 1 == main_c["start_row"]
                similar_span = _overlap_ratio(meta_c, main_c) >= 0.9
                compact_header_band = meta_height <= 2

                if contiguous_above and similar_span and compact_header_band:
                    # Expand Main Data upward to include header row/band.
                    main["coordinates"]["start_row"] = meta_c["start_row"]
                    main["coordinates"]["header_row"] = meta_c["start_row"]
                    # Keep boundary stable for full-width table interpretation.
                    main["coordinates"]["start_col"] = min(main_c["start_col"], meta_c["start_col"])
                    main["coordinates"]["end_col"] = max(main_c["end_col"], meta_c["end_col"])
                    main_c = _coords(main)
                    absorbed_ids.add(meta.get("id"))

                    # Ensure this block is treated as primary table.
                    if not main.get("ai_suggestion"):
                        main["ai_suggestion"] = "Keep"
                    if "summary" in main and isinstance(main["summary"], str):
                        if "header" not in main["summary"].lower():
                            main["summary"] += " Includes detected header row."
                    main.setdefault("python_detection", {}).setdefault("cleanup_actions", []).append(
                        f"absorbed_header_band:{meta.get('id')}"
                    )

        if not absorbed_ids:
            return blocks

        return [b for b in blocks if b.get("id") not in absorbed_ids]

    def apply_demarcation(self, grid_df, blocks, decisions):
        """
        Filter or annotate the grid based on user decisions.
        This will be used when moving to Step 0.
        """
        if grid_df is None:
            return None

        if not isinstance(grid_df, pd.DataFrame):
            grid_df = pd.DataFrame(grid_df)

        normalized_blocks = []
        for idx, block in enumerate(blocks or []):
            normalized = dict(block)
            decision = normalized.get("decision")
            if isinstance(decisions, dict):
                decision = (
                    decisions.get(idx)
                    or decisions.get(normalized.get("label"))
                    or decisions.get(normalized.get("id"))
                    or decision
                )
            if decision is not None:
                normalized["decision"] = decision
            normalized_blocks.append(normalized)

        scoped_source = build_scoped_source(
            sheet_name=None,
            total_rows=len(grid_df),
            total_cols=len(grid_df.columns),
            blocks=normalized_blocks,
        )
        bounds = scoped_source.get("analysis_bounds", {})

        if not scoped_source.get("requires_extraction"):
            return grid_df.copy()

        return grid_df.iloc[
            bounds.get("start_row", 0):bounds.get("end_row", len(grid_df) - 1) + 1,
            bounds.get("start_col", 0):bounds.get("end_col", len(grid_df.columns) - 1) + 1,
        ].copy()
