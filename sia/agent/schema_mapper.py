import asyncio
import json
import logging
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from .llm_handler import robust_json_parse
from .value_scale import enrich_mapping_value_scale
from ..debug.llm_observer import get_observer

logger = logging.getLogger(__name__)

# Mapping only needs representative profiles; avoid full-sheet scans during schema mapping.
_PROFILE_HEAD_ROWS = 500
_PROFILE_TAIL_ROWS = 500
_PROFILE_MAX_ROWS = _PROFILE_HEAD_ROWS + _PROFILE_TAIL_ROWS


def _dataframe_for_column_profile(df: pd.DataFrame) -> pd.DataFrame:
    """Sample rows for mapping-time profiling: first N + last N when the sheet is large."""
    n = len(df.index)
    if n <= _PROFILE_MAX_ROWS:
        return df
    head = df.iloc[:_PROFILE_HEAD_ROWS]
    tail = df.iloc[-_PROFILE_TAIL_ROWS:]
    return pd.concat([head, tail], ignore_index=True)


@lru_cache(maxsize=1)
def _load_column_synonyms_from_config() -> Dict[str, List[str]]:
    path = Path(__file__).resolve().parent.parent.parent / "config" / "synonyms.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("column_synonyms") or {}
        return {str(k): (list(v) if isinstance(v, list) else []) for k, v in raw.items()}
    except Exception as e:
        logger.warning("Could not load synonyms.json for column matching: %s", e)
        return {}


class SchemaMapper:
    """
    Handles Step-0 Media Canonical Working Table (MCWT) creation.
    Classifies raw columns into 6 classes using LLM reasoning.
    """
    
    def __init__(self, llm_client=None, prompts_dir: str = "prompts"):
        self.llm_client = llm_client
        self.prompts_dir = Path(prompts_dir)
        self.classifier_prompt = self._build_strict_system_prompt(
            self._load_prompt("mcwt_classifier.md")
        )

    def _load_prompt(self, filename: str) -> str:
        """Load prompt from markdown file."""
        try:
            path = self.prompts_dir / filename
            if path.exists():
                return path.read_text(encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to load prompt {filename}: {e}")
        return ""

    async def propose_mapping(
        self,
        df: pd.DataFrame,
        target_columns: Optional[List[str]] = None,
        allow_heuristic_fallback: bool = True,
        primary_targets: Optional[Iterable[str]] = None,
        combined_field_hints: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Analyze columns and propose a classification for each.
        Returns a list of column mapping dictionaries.
        """
        # Allow mapping if we have columns, even if 0 data rows
        if df is None or len(df.columns) == 0:
            return []

        primary_targets_set = set(primary_targets or [])
        samples_meta = self._get_samples_meta(df)
        heuristic_mappings = self._heuristic_mapping(
            df,
            samples_meta,
            target_columns=target_columns,
            primary_targets=primary_targets_set,
        )
        heuristic_mappings = self._apply_combined_field_hints(
            heuristic_mappings,
            combined_field_hints or [],
            target_columns=target_columns,
            primary_targets=primary_targets_set,
        )
        heuristic_lookup = {m["column_name"]: m for m in heuristic_mappings}
        if self.llm_client and not self._has_unresolved_columns(heuristic_mappings):
            logger.info(
                "SchemaMapper: all columns resolved by deterministic mapping; skipping LLM call."
            )
            return self._annotate_deterministic_mappings(
                heuristic_mappings,
                "All non-blank source columns resolved by exact/synonym/business-rule matching.",
            )

        column_samples = {}
        for col in df.columns:
            meta = samples_meta.get(col, {})
            column_samples[col] = {
                "dtype": str(df[col].dtype),
                "samples": meta.get("unique_values", [])[:3],
                "non_null_count": meta.get("non_null_count", 0),
                "blank_ratio": meta.get("blank_ratio", 0.0),
                "inferred_type": meta.get("inferred_type", "Dimension")
            }

        # If no LLM client is configured for this run
        if not self.llm_client:
            if allow_heuristic_fallback:
                return heuristic_mappings
            raise RuntimeError("LLM client is not configured for schema mapping.")

        user_prompt = self._build_mapping_user_prompt(column_samples, target_columns=target_columns)
        prompt = self._compose_prompt(user_prompt)
        
        try:
            observer = get_observer()
            llm_results = []
            response_text = ""
            source_columns = [str(col) for col in df.columns]

            if observer:
                with observer.trace("schema_mapper") as t:
                    t.system_prompt = self.classifier_prompt
                    t.user_prompt = user_prompt
                    t.full_prompt = prompt
                    t.input_context = {
                        "source_columns_count": len(df.columns),
                        "target_columns_count": len(target_columns or [])
                    }

                    response = await self._generate_mapping_response(prompt)
                    response_text = response.text if hasattr(response, 'text') else str(response)
                    t.raw_response = response_text

                    try:
                        llm_results = self._parse_mapping_response(response_text)
                        llm_results = self._validate_mapping_results(llm_results, source_columns)
                    except Exception as parse_err:
                        logger.error(f"Failed to parse LLM response: {parse_err}")
                        if allow_heuristic_fallback:
                            fallback = self._annotate_fallback_mappings(
                                heuristic_mappings,
                                f"Failed to parse LLM mapping response: {parse_err}"
                            )
                            t.parsed_output = {"fallback": True, "reason": str(parse_err), "mappings_count": len(fallback)}
                            t.confidence_score = 0.35
                            return fallback
                        failed_rows = self._build_failed_mapping_rows(
                            df, samples_meta, target_columns, primary_targets_set, f"Failed to Map: {parse_err}"
                        )
                        t.parsed_output = {"fallback": False, "reason": str(parse_err), "mappings_count": len(failed_rows)}
                        t.confidence_score = 0.0
                        return failed_rows

                    t.parsed_output = {
                        "fallback": False,
                        "mappings_count": len(llm_results),
                        "sample_mappings": llm_results[:5]
                    }
                    t.confidence_score = 0.8 if llm_results else 0.0
            else:
                response = await self._generate_mapping_response(prompt)
                response_text = response.text if hasattr(response, 'text') else str(response)
                try:
                    llm_results = self._parse_mapping_response(response_text)
                    llm_results = self._validate_mapping_results(llm_results, source_columns)
                except Exception as parse_err:
                    logger.error(f"Failed to parse LLM response: {parse_err}")
                    if allow_heuristic_fallback:
                        return self._annotate_fallback_mappings(
                            heuristic_mappings,
                            f"Failed to parse LLM mapping response: {parse_err}"
                        )
                    return self._build_failed_mapping_rows(
                        df, samples_meta, target_columns, primary_targets_set, f"Failed to Map: {parse_err}"
                    )

            # Merge LLM results with samples
            final_mappings = []
            # Create a lookup for LLM results
            llm_lookup = {res.get("column_name"): res for res in llm_results if isinstance(res, dict)}

            for col in df.columns:
                llm_res = llm_lookup.get(col, {})
                meta = samples_meta.get(col, {})
                fallback = heuristic_lookup.get(col, {})
                classification = llm_res.get("classification")
                decision = llm_res.get("decision")
                reasoning = llm_res.get("reasoning")
                confidence = llm_res.get("confidence", 0.5)
                column_type = self._map_to_simple_type(col, classification, df[col].dtype, meta=meta)
                target_suggestion = self._suggest_target_column(str(col), target_columns, meta=meta)

                if column_type == "Blank":
                    classification = "Blank / Empty"
                    decision = "Discard"
                    reasoning = fallback.get("reasoning", "Column is completely blank and should be excluded.")
                
                final_mappings.append(enrich_mapping_value_scale({
                    "column_name": col,
                    "classification": classification,
                    "decision": decision,
                    "reasoning": reasoning,
                    "confidence": confidence,
                    "column_type": column_type,
                    "role": self.infer_column_role(
                        classification,
                        decision,
                        column_type,
                        target_column=target_suggestion.get("target_column"),
                        primary_targets=primary_targets_set,
                    ),
                    **target_suggestion,
                    "unique_values": meta.get("unique_values", []),
                    "stats": meta.get("stats", {})
                }))
            return final_mappings

        except Exception as e:
            logger.error(f"Error during LLM classification: {e}")
            if allow_heuristic_fallback:
                return self._annotate_fallback_mappings(
                    heuristic_mappings,
                    f"LLM mapping failed: {e}"
                )
            return self._build_failed_mapping_rows(
                df, samples_meta, target_columns, primary_targets_set, f"Failed to Map: {e}"
            )

    def _build_strict_system_prompt(self, base_prompt: str) -> str:
        strict_contract = (
            "## STRICT OUTPUT CONTRACT\n"
            "Return only valid JSON. Do not include markdown fences, headings, commentary, or prose.\n"
            "The root value MUST be a JSON array.\n"
            "Return exactly one object for every source column provided, in the same order as `SOURCE_COLUMNS`.\n"
            "Each object MUST contain these keys only: "
            "`classification`, `decision`, `reasoning`, `confidence`.\n"
            "Do not rename, rewrite, or guess source column names in the JSON output.\n"
            "`classification` MUST be a non-empty string.\n"
            "`decision` MUST be either `Keep` or `Discard`.\n"
            "`reasoning` MUST be a short plain-text explanation.\n"
            "`confidence` MUST be a number between 0 and 1.\n"
            "The backend will attach the exact source column names deterministically.\n"
            "If uncertain, still return the best-effort object for that source column.\n"
        )
        return f"{base_prompt}\n\n{strict_contract}".strip()

    def _build_mapping_user_prompt(
        self,
        column_samples: Dict[str, Dict[str, Any]],
        target_columns: Optional[List[str]] = None
    ) -> str:
        source_columns = list(column_samples.keys())
        compact_profiles = self._build_compact_column_profiles(column_samples)
        parts = [
            f"SOURCE_COLUMNS: {json.dumps(source_columns, ensure_ascii=False)}",
            "SOURCE_COLUMN_SUMMARIES:",
            compact_profiles,
            "",
            "Return one JSON object per source column in exactly the same order as SOURCE_COLUMNS.",
            "Do not include source column names in the output; they are attached deterministically by the backend.",
        ]
        if target_columns:
            parts.extend([
                "",
                f"TARGET_COLUMNS: {json.dumps(target_columns, ensure_ascii=False)}",
                "Use target columns as context only. Do not add `target_column` to the JSON output.",
            ])
        parts.extend([
            "",
            "Return the JSON array now.",
        ])
        return "\n".join(parts)

    def _build_compact_column_profiles(self, column_samples: Dict[str, Dict[str, Any]]) -> str:
        lines: List[str] = []
        for idx, (column_name, meta) in enumerate(column_samples.items(), start=1):
            samples = [str(v) for v in (meta.get("samples") or [])[:2]]
            inferred_type = str(meta.get("inferred_type", "Unknown"))
            blank_ratio = float(meta.get("blank_ratio", 0.0) or 0.0)
            sample_text = ", ".join(samples) if samples else "none"
            lines.append(
                f"{idx}. {column_name} | type={inferred_type} | "
                f"blank={round(blank_ratio * 100)}% | samples=[{sample_text}]"
            )
        return "\n".join(lines)

    def _compose_prompt(self, user_prompt: str) -> str:
        return f"{self.classifier_prompt}\n\n{user_prompt}"

    def _validate_mapping_results(
        self,
        llm_results: List[Dict[str, Any]],
        source_columns: List[str]
    ) -> List[Dict[str, Any]]:
        if not llm_results:
            raise ValueError(
                "LLM mapping response did not contain any valid column mapping entries."
            )

        if len(llm_results) != len(source_columns):
            raise ValueError(
                f"Model returned {len(llm_results)} mapping rows for {len(source_columns)} source columns."
            )

        validated: List[Dict[str, Any]] = []

        for idx, row in enumerate(llm_results):
            column_name = source_columns[idx]
            classification = str(row.get("classification", "")).strip()
            decision = str(row.get("decision", "")).strip()
            reasoning = str(row.get("reasoning", "")).strip()
            confidence = row.get("confidence")

            if not classification:
                raise ValueError(f"Missing `classification` for column: {column_name}")
            if decision not in {"Keep", "Discard"}:
                raise ValueError(f"Invalid `decision` for column {column_name}: {decision}")
            if not reasoning:
                raise ValueError(f"Missing `reasoning` for column: {column_name}")

            try:
                confidence_value = float(confidence)
            except Exception as exc:
                raise ValueError(f"Invalid `confidence` for column {column_name}: {confidence}") from exc
            if confidence_value < 0 or confidence_value > 1:
                raise ValueError(
                    f"`confidence` must be between 0 and 1 for column {column_name}: {confidence_value}"
                )

            validated.append({
                "column_name": column_name,
                "classification": classification,
                "decision": decision,
                "reasoning": reasoning,
                "confidence": confidence_value,
            })

        return validated

    def _build_failed_mapping_rows(
        self,
        df: pd.DataFrame,
        samples_meta: Dict[str, Dict[str, Any]],
        target_columns: Optional[List[str]],
        primary_targets: Optional[Iterable[str]],
        reason: str
    ) -> List[Dict[str, Any]]:
        heuristic_rows = self._heuristic_mapping(
            df,
            samples_meta,
            target_columns=target_columns,
            primary_targets=primary_targets,
        )
        fallback_rows: List[Dict[str, Any]] = []
        for row in heuristic_rows:
            item = dict(row)
            item["semantic_status"] = "fallback"
            item["llm_error"] = reason
            item["reasoning"] = (
                f"{item.get('reasoning', 'Deterministic fallback used.')} "
                f"Semantic LLM classification failed: {reason}"
            ).strip()
            item["confidence"] = min(float(item.get("confidence", 0.5)), 0.35)
            fallback_rows.append(item)
        return fallback_rows

    def _parse_mapping_response(self, response_text: str) -> List[Dict[str, Any]]:
        """Parse LLM mapping output across common JSON shapes."""
        parsed = robust_json_parse(response_text)

        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except Exception:
                parsed = []

        candidates: List[Dict[str, Any]] = []
        if isinstance(parsed, list):
            candidates = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict):
            if isinstance(parsed.get("mappings"), list):
                candidates = [x for x in parsed.get("mappings", []) if isinstance(x, dict)]
            elif isinstance(parsed.get("columns"), list):
                candidates = [x for x in parsed.get("columns", []) if isinstance(x, dict)]
            elif all(isinstance(v, dict) for v in parsed.values()):
                # Shape: {"Spend": {"classification": "...", ...}, ...}
                for key, value in parsed.items():
                    row = dict(value)
                    row.setdefault("column_name", key)
                    candidates.append(row)
            elif "column_name" in parsed or "classification" in parsed:
                candidates = [parsed]

        # Fallback: try to extract JSON array if model wrapped text around it
        if not candidates:
            match = re.search(r"\[[\s\S]*\]", response_text)
            if match:
                data = json.loads(match.group(0))
                if isinstance(data, list):
                    candidates = [x for x in data if isinstance(x, dict)]

        normalized: List[Dict[str, Any]] = []
        for row in candidates:
            normalized.append({
                "classification": row.get("classification"),
                "decision": row.get("decision"),
                "reasoning": row.get("reasoning"),
                "confidence": row.get("confidence", 0.5),
            })

        return normalized

    @staticmethod
    def _annotate_fallback_mappings(mappings: List[Dict[str, Any]], reason: str) -> List[Dict[str, Any]]:
        """
        Preserve mapping UX even when LLM output is unusable.
        This keeps user control (Keep/Exclude) and surfaces explicit fallback reason.
        """
        annotated: List[Dict[str, Any]] = []
        for row in mappings:
            item = dict(row)
            item["llm_error"] = reason
            # Keep confidence non-zero but visibly low to signal fallback quality.
            item["confidence"] = min(float(item.get("confidence", 0.5)), 0.35)
            annotated.append(item)
        return annotated

    @staticmethod
    def _has_unresolved_columns(mappings: List[Dict[str, Any]]) -> bool:
        """True when a non-blank source column still has no target suggestion."""
        for row in mappings or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("column_type") or "").strip().lower() == "blank":
                continue
            target = str(row.get("target_column") or "").strip().lower()
            if not target or target in {"no match", "nomatch", "no-match"}:
                return True
        return False

    @staticmethod
    def _annotate_deterministic_mappings(
        mappings: List[Dict[str, Any]], reason: str
    ) -> List[Dict[str, Any]]:
        """Mark rows that were completed without an LLM call."""
        annotated: List[Dict[str, Any]] = []
        for row in mappings:
            item = dict(row)
            item["semantic_status"] = "deterministic"
            item["llm_skipped"] = True
            item["llm_skip_reason"] = reason
            annotated.append(item)
        return annotated

    async def _generate_mapping_response(self, prompt: str):
        """Support both async and sync LLM client interfaces."""
        if hasattr(self.llm_client, "generate_content_async"):
            response = self.llm_client.generate_content_async(prompt)
            if hasattr(response, "__await__"):
                return await response
            return response
        if hasattr(self.llm_client, "generate_content"):
            return await asyncio.to_thread(self.llm_client.generate_content, prompt)
        raise AttributeError("LLM client does not expose generate_content or generate_content_async")

    def _map_to_simple_type(self, col_name: str, classification: str, dtype: Any, meta: Optional[Dict[str, Any]] = None) -> str:
        """Map complex classifications to simple UI types (Date, Dimension, Metric)."""
        c = classification.lower()
        col_lower = col_name.lower()
        if meta and meta.get("inferred_type"):
            inferred = meta["inferred_type"]
            if inferred in {"Blank", "Metric", "Date"}:
                return inferred
        
        # 1. Check column name for known metric keywords (strongest signal)
        metric_keywords = ['spend', 'cost', 'reach', 'click', 'imps', 'impressions',
                           'revenue', 'views', 'taps', 'likes', 'shares', 'conv',
                           'budget', 'frequency', 'grp', 'trp', 'cpm', 'cpc', 'ctr', 'rate']
        if any(kw in col_lower for kw in metric_keywords):
            return "Metric"
        
        # 2. Check column name for date keywords
        if "date" in col_lower or "time" in col_lower:
            return "Date"
        if "blank" in c or "empty" in c:
            return "Blank"
        
        # 3. Check LLM classification text
        if "date" in c or "time" in c:
            return "Date"
        if "metric" in c or "delivery" in c or "cost" in c or "engagement" in c:
            return "Metric"
        if "structural" in c or "governance" in c or "dimension" in c:
            return "Dimension"
        
        # 4. Fallback to dtype analysis
        if pd.api.types.is_numeric_dtype(dtype):
            return "Metric"
        return "Dimension"

    def _get_samples_meta(self, df: pd.DataFrame) -> Dict[str, Dict]:
        """Get top-10 unique samples and descriptive statistics for each column."""
        meta = {}
        n_full = len(df.index)
        profile_df = _dataframe_for_column_profile(df)
        if n_full > _PROFILE_MAX_ROWS:
            logger.info(
                "Column mapping profile: using first %s + last %s of %s rows",
                f"{_PROFILE_HEAD_ROWS:,}",
                f"{_PROFILE_TAIL_ROWS:,}",
                f"{n_full:,}",
            )
        for col in profile_df.columns:
            series = profile_df[col]
            normalized = series.map(self._normalize_blankish)
            non_blank = normalized.dropna()
            blank_count = int(len(series) - len(non_blank))
            blank_ratio = (blank_count / len(series)) if len(series) else 1.0
            numeric_series = self._coerce_numeric(non_blank)
            numeric_count = int(numeric_series.notna().sum())
            numeric_ratio = (numeric_count / len(non_blank)) if len(non_blank) else 0.0
            inferred_type = self._infer_column_type(col, series, blank_ratio, numeric_ratio)
            
            # Basic info
            col_meta = {
                "dtype": str(series.dtype),
                "unique_values": [str(val) for val in non_blank.unique()[:10]],  # Restrict to top 10
                "non_null_count": int(len(non_blank)),
                "total_count": len(series),
                "sheet_row_count": n_full,
                "blank_count": blank_count,
                "blank_ratio": round(blank_ratio, 4),
                "numeric_ratio": round(numeric_ratio, 4),
                "inferred_type": inferred_type,
                "stats": {}
            }
            
            if inferred_type == "Blank":
                col_meta["stats"] = {
                    "kind": "blank",
                    "blank_count": blank_count,
                    "non_null_count": int(len(non_blank)),
                    "blank_ratio": round(blank_ratio, 4)
                }
            elif numeric_ratio >= 0.8 and numeric_count > 0:
                try:
                    valid_data = numeric_series.dropna()
                    if not valid_data.empty:
                        col_meta["stats"] = {
                            "kind": "numeric",
                            "min": float(valid_data.min()),
                            "max": float(valid_data.max()),
                            "mean": float(valid_data.mean()),
                            "non_null_count": int(len(valid_data))
                        }
                except Exception as e:
                    logger.warning(f"Failed to calculate stats for {col}: {e}")
                    col_meta["stats"] = {"kind": "numeric_error", "min": 0, "max": 0, "mean": 0}
            
            meta[col] = col_meta
        return meta

    def _heuristic_mapping(
        self,
        df: pd.DataFrame,
        samples_meta: Dict = None,
        target_columns: Optional[List[str]] = None,
        primary_targets: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Fallback heuristics for column classification."""
        columns = df.columns
        delivery_keywords = ['imps', 'impressions', 'spend', 'cost', 'clicks', 'taps', 'reach', 'revenue', 'conv', 'paid impressions', 'frequency']
        ratio_keywords = ['cpm', 'cpc', 'ctr', 'rate', 'ratio']
        structural_keywords = ['campaign', 'ad', 'group', 'id', 'name', 'publisher', 'site', 'channel', 'region', 'market', 'creative']
        state_keywords = ['status', 'active', 'paused', 'date', 'start', 'end', 'time']
        
        mappings = []
        for col in columns:
            col_name = str(col)
            col_lower = col_name.lower()
            meta = samples_meta.get(col, {}) if samples_meta else {}
            samples = meta.get("unique_values", []) if samples_meta else []
            column_type = self._map_to_simple_type(col_name, "", df[col].dtype, meta=meta)
            target_suggestion = self._suggest_target_column(col_name, target_columns, meta=meta)
            
            # Default to Derived (Safe to discard)
            classification = "Derived"
            decision = "Discard"
            reasoning = "Heuristic classified as derived noise."
            
            if column_type == "Blank":
                classification = "Blank / Empty"
                decision = "Discard"
                reasoning = f"Column is blank/empty in {meta.get('blank_count', 0)} of {meta.get('total_count', 0)} rows."
            elif any(k in col_lower for k in ratio_keywords):
                classification = "Derived"
                decision = "Discard"
                reasoning = "Detected a derived KPI/ratio metric. It is numeric but excluded by default from the canonical working table."
            elif any(k in col_lower for k in delivery_keywords) or column_type == "Metric":
                classification = "Delivery Metrics"
                decision = "Keep"
                reasoning = "Numeric-like column with metric-style semantics based on name/value profile."
            elif any(k in col_lower for k in structural_keywords):
                classification = "Structural Metadata"
                decision = "Keep"
                reasoning = "Heuristic matched structural descriptors."
            elif any(k in col_lower for k in state_keywords):
                classification = "State / Governance"
                decision = "Keep"
                reasoning = "Heuristic matched state/temporal metadata."

            matched_target = str(target_suggestion.get("target_column") or "").strip()
            primary_set = set(primary_targets or [])
            if matched_target and matched_target.lower() != "no match" and matched_target in primary_set:
                if decision == "Discard":
                    classification = "Structural Metadata"
                    decision = "Keep"
                    reasoning = (
                        f"Source column matched primary template field '{matched_target}'."
                    )
            
            mappings.append(enrich_mapping_value_scale({
                "column_name": col_name,
                "classification": classification,
                "decision": decision,
                "reasoning": reasoning,
                "confidence": 0.5,
                "column_type": column_type,
                "role": self.infer_column_role(
                    classification,
                    decision,
                    column_type,
                    target_column=target_suggestion.get("target_column"),
                    primary_targets=primary_targets,
                ),
                **target_suggestion,
                "unique_values": samples,
                "stats": meta.get("stats", {})
            }))
        return mappings

    def _apply_combined_field_hints(
        self,
        mappings: List[Dict[str, Any]],
        combined_field_hints: List[Dict[str, Any]],
        target_columns: Optional[List[str]] = None,
        primary_targets: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        """Boost mapping proposals using Upload-page accepted packed-column splits."""
        if not combined_field_hints or not mappings:
            return mappings
        by_col = {
            str(h.get("source_column")): h
            for h in combined_field_hints
            if isinstance(h, dict) and h.get("source_column") and h.get("accepted") and not h.get("single_dimension")
        }
        if not by_col:
            return mappings
        target_lookup = {self._normalize_name(t): t for t in (target_columns or [])}
        primary_set = set(primary_targets or [])
        out = []
        for row in mappings:
            col = str(row.get("column_name") or "")
            hint = by_col.get(col)
            if not hint:
                out.append(row)
                continue
            dims = [str(d) for d in (hint.get("target_dimensions") or []) if d]
            # Prefer first dimension that exists on the target template
            chosen = None
            for d in dims:
                dn = self._normalize_name(d)
                if dn in target_lookup:
                    chosen = target_lookup[dn]
                    break
                # common alias spends/spend
                if d in ("spend", "spends") and "spends" in target_lookup:
                    chosen = target_lookup["spends"]
                    break
            updated = dict(row)
            delim = hint.get("delimiter") or "_"
            note = (
                f"Upload hierarchy: packed field splits on {delim!r} into "
                f"{', '.join(dims) or 'dimensions'} (planner may emit transform.split_column)."
            )
            updated["combined_field_hint"] = {
                "delimiter": delim,
                "target_dimensions": dims,
                "samples": list(hint.get("samples") or [])[:6],
            }
            if chosen:
                prev = str(updated.get("target_column") or "").strip().lower()
                if prev in ("", "no match", "nomatch", "no-match"):
                    updated["target_column"] = chosen
                    updated["target_match_confidence"] = max(
                        float(updated.get("target_match_confidence") or 0),
                        0.9,
                    )
                    updated["target_match_method"] = "hierarchy_combined_field"
                    updated["decision"] = "Keep"
                    updated["classification"] = updated.get("classification") or "Structural Metadata"
                    updated["confidence"] = max(float(updated.get("confidence") or 0), 0.85)
                    if chosen in primary_set:
                        updated["role"] = "primary"
                updated["reasoning"] = f"{updated.get('reasoning') or ''} {note}".strip()
            else:
                updated["reasoning"] = f"{updated.get('reasoning') or ''} {note}".strip()
            out.append(updated)
        return out

    @staticmethod
    def infer_column_role(
        classification: Optional[str],
        decision: Optional[str],
        column_type: Optional[str],
        target_column: Optional[str] = None,
        primary_targets: Optional[Iterable[str]] = None,
    ) -> str:
        """
        Suggest a UI role for a source column: 'primary', 'supporting', or 'exclude'.

        Contract:
          * **exclude** - source column is blank, derived noise, or already decided as ``Discard``.
          * **primary** - matched ``target_column`` is a date key, a ``uid_hierarchy`` member,
            or a template metric in ``primary_targets``.
          * **exclude** - metric-like columns that do not map to one of those primary targets.
          * **supporting** - useful non-metric context that is neither excluded nor primary.

        Does not replace ``decision`` / ``target_column``; UI keeps those in sync via
        ``applyRoleRule`` on the frontend.
        """
        cls = str(classification or "").strip().lower()
        dec = str(decision or "").strip().lower()
        ctype = str(column_type or "").strip().lower()
        is_metric_like = (
            ctype == "metric"
            or "delivery" in cls
            or "cost" in cls
            or "engagement" in cls
            or "metric" in cls
        )

        matched_target = str(target_column or "").strip()
        if matched_target and matched_target.lower() != "no match":
            if primary_targets and matched_target in set(primary_targets):
                return "primary"

        if dec == "discard" or ctype == "blank" or "blank" in cls or "derived" in cls:
            return "exclude"

        if is_metric_like:
            return "exclude"

        return "supporting"

    @staticmethod
    def _normalize_name(name: str) -> str:
        normalized = str(name or "").strip().lower()
        if normalized.endswith("_paid_media"):
            normalized = normalized[: -len("_paid_media")]
        normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
        return normalized.strip("_")

    @staticmethod
    def _sample_profile(meta: Optional[Dict[str, Any]]) -> Dict[str, bool]:
        samples = [str(v).strip() for v in (meta or {}).get("unique_values", []) if str(v).strip()]
        inferred_type = str((meta or {}).get("inferred_type") or "").strip().lower()
        lowered = [s.lower() for s in samples]

        def has_token(value: str, aliases: set[str]) -> bool:
            compact = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
            if not compact:
                return False
            tokens = compact.split()
            if compact in aliases:
                return True
            for alias in aliases:
                if alias in tokens:
                    return True
                if compact.startswith(f"{alias} "):
                    return True
            return False

        def parseable_dates(values: List[str]) -> int:
            count = 0
            for value in values[:6]:
                try:
                    if pd.notna(pd.to_datetime(value, errors="coerce")):
                        count += 1
                except Exception:
                    continue
            return count

        geo_tokens = {
            "us", "usa", "uk", "uae", "au", "nz", "id", "in", "sg", "ca", "de", "fr", "it", "es",
            "mexico", "india", "indonesia", "australia", "united states", "united kingdom",
            "apac", "emea", "latam", "north america", "europe",
        }
        channel_tokens = {
            "search", "social", "display", "video", "programmatic", "affiliate", "tv", "radio",
            "meta", "facebook", "instagram", "tiktok", "snapchat", "youtube", "google", "bing",
        }
        publisher_tokens = {
            "google", "meta", "facebook", "instagram", "youtube", "tiktok", "snapchat", "amazon",
            "twitter", "x", "linkedin", "pinterest",
        }
        channel_aliases = channel_tokens | {"ig", "insta", "tik_tok", "tiktok", "yt"}
        publisher_aliases = publisher_tokens | {"ig", "insta", "tik_tok", "tiktok", "yt"}

        geo_hits = sum(1 for value in lowered[:6] if value in geo_tokens)
        channel_hits = sum(1 for value in lowered[:6] if has_token(value, channel_aliases))
        publisher_hits = sum(1 for value in lowered[:6] if has_token(value, publisher_aliases))
        date_hits = parseable_dates(samples)

        return {
            "date_like": inferred_type == "date" or date_hits >= max(1, min(2, len(samples[:6]))),
            "geo_like": geo_hits >= max(1, min(2, len(samples[:6]))),
            "channel_like": channel_hits >= max(1, min(2, len(samples[:6]))),
            "publisher_like": publisher_hits >= max(1, min(2, len(samples[:6]))),
            "metric_like": inferred_type == "metric",
        }

    @staticmethod
    def _target_semantic_bucket(target_name: str) -> Optional[str]:
        norm = SchemaMapper._normalize_name(target_name)
        if any(token in norm for token in ("date", "week", "month", "quarter", "day")):
            return "date_like"
        if any(token in norm for token in ("market", "region", "country", "geo")):
            return "geo_like"
        if any(token in norm for token in ("channel", "media_channel", "placement", "platform")):
            return "channel_like"
        if any(token in norm for token in ("publisher", "site", "source", "partner", "vendor")):
            return "publisher_like"
        if any(token in norm for token in ("spend", "impression", "click", "view", "reach", "cost", "revenue")):
            return "metric_like"
        return None

    def _suggest_target_from_values(
        self,
        source_column: str,
        target_columns: Optional[List[str]],
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not target_columns:
            return None
        profile = self._sample_profile(meta)
        source_norm = self._normalize_name(source_column)
        best_target = None
        best_score = 0.0

        for raw_target in target_columns:
            bucket = self._target_semantic_bucket(str(raw_target))
            # Value-profile matching is only trustworthy for categorical / lexical signals
            # (geo, channel, publisher) and parseable dates. Numeric columns (likes, followers,
            # costs) share similar magnitude patterns; mapping them to spends/impressions from
            # value samples is misleading — use header synonyms, business rules, or string similarity.
            if bucket == "metric_like":
                continue
            if not bucket or not profile.get(bucket):
                continue

            score = 0.78
            target_norm = self._normalize_name(str(raw_target))
            if bucket in {"geo_like", "channel_like", "publisher_like"} and source_norm:
                if any(token in source_norm for token in target_norm.split("_") if len(token) >= 3):
                    score += 0.07
            if score > best_score:
                best_score = score
                best_target = str(raw_target)

        if not best_target:
            return None
        return {
            "target_column": best_target,
            "target_match_confidence": round(best_score, 4),
            "target_match_method": "value_profile",
        }

    def _suggest_target_column(
        self,
        source_column: str,
        target_columns: Optional[List[str]],
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not target_columns:
            return {
                "target_column": "No match",
                "target_match_confidence": 0.0,
                "target_match_method": "none",
            }

        source_norm = self._normalize_name(source_column)
        target_lookup = {self._normalize_name(t): t for t in target_columns}

        if source_norm in target_lookup:
            return {
                "target_column": target_lookup[source_norm],
                "target_match_confidence": 1.0,
                "target_match_method": "exact",
            }

        file_syns = _load_column_synonyms_from_config()
        if file_syns:
            sn_compact = source_norm.replace("_", "")
            # Pass 1: exact matches on template field names and synonym list entries only.
            # Pass 2 (below): substring / partial matches — must run after pass 1 so e.g. "Market"
            # maps to region's explicit alias "market" before channel's partial "marketing_channel".
            for tgt in target_columns:
                tgt_key = tgt if tgt in file_syns else None
                if not tgt_key:
                    continue
                tgt_norm = self._normalize_name(tgt)
                tgt_compact = tgt_norm.replace("_", "")
                if source_norm == tgt_norm or sn_compact == tgt_compact:
                    return {
                        "target_column": target_lookup.get(tgt_norm, tgt),
                        "target_match_confidence": 0.95,
                        "target_match_method": "synonyms_json",
                    }
                for alias in file_syns[tgt_key]:
                    al = self._normalize_name(str(alias))
                    ac = al.replace("_", "")
                    if source_norm == al or sn_compact == ac:
                        return {
                            "target_column": target_lookup.get(tgt_norm, tgt),
                            "target_match_confidence": 0.88,
                            "target_match_method": "synonyms_json",
                        }
            for tgt in target_columns:
                tgt_key = tgt if tgt in file_syns else None
                if not tgt_key:
                    continue
                tgt_norm = self._normalize_name(tgt)
                for alias in file_syns[tgt_key]:
                    al = self._normalize_name(str(alias))
                    if len(al) >= 4 and (al in source_norm or source_norm in al):
                        return {
                            "target_column": target_lookup.get(tgt_norm, tgt),
                            "target_match_confidence": 0.82,
                            "target_match_method": "synonyms_json_partial",
                        }

        # Business-critical override: spend-like columns map to spends when present in template.
        if any(k in source_norm for k in ["spend", "media_cost", "cost", "total_cost"]) and "spends" in target_lookup:
            return {
                "target_column": target_lookup["spends"],
                "target_match_confidence": 0.99,
                "target_match_method": "business_rule",
            }

        synonym_map = {
            "publisher": "publisher",
            "publishername": "publisher",
            "pub_name": "publisher",
            "publisher_name": "publisher",
            "channel_name": "channel",
            "media_channel": "channel",
            "imps": "impressions",
            "impression": "impressions",
            "campaignobjective": "campaign_type",
        }
        source_compact = source_norm.replace("_", "")
        mapped = synonym_map.get(source_compact)
        if mapped and mapped in target_lookup:
            return {
                "target_column": target_lookup[mapped],
                "target_match_confidence": 0.9,
                "target_match_method": "synonym",
            }

        sample_based_match = self._suggest_target_from_values(source_column, target_columns, meta=meta)
        if sample_based_match:
            return sample_based_match

        best_match = "No match"
        best_score = 0.0
        for norm_target, raw_target in target_lookup.items():
            score = SequenceMatcher(None, source_norm, norm_target).ratio()
            if score > best_score:
                best_score = score
                best_match = raw_target
        if best_score >= 0.72:
            return {
                "target_column": best_match,
                "target_match_confidence": round(best_score, 4),
                "target_match_method": "similarity",
            }
        return {
            "target_column": "No match",
            "target_match_confidence": round(best_score, 4),
            "target_match_method": "none",
        }

    def _match_target_column(self, source_column: str, target_columns: Optional[List[str]]) -> str:
        return self._suggest_target_column(source_column, target_columns)["target_column"]

    def create_mcwt(self, df: pd.DataFrame, decisions: Dict[str, str]) -> pd.DataFrame:
        """
        Filter the DataFrame based on user decisions.
        'decisions' map column names to 'Keep' or 'Discard'.
        """
        requested_columns = [
            col for col, decision in decisions.items()
            if decision in {"Keep", "Metadata", "Context", "Use as Context"}
        ]
        cols_to_keep = list(requested_columns)
        # Ensure we only try to keep columns that actually exist
        cols_to_keep = [c for c in cols_to_keep if c in df.columns]
        if not cols_to_keep:
            available = [str(col) for col in df.columns]
            requested = [str(col) for col in requested_columns]
            if available:
                logger.warning(
                    "MCWT: no Keep/Metadata columns selected; falling back to all %s columns",
                    len(available),
                )
                cols_to_keep = list(df.columns)
            else:
                logger.error("MCWT creation failed: dataframe has no columns.")
                raise ValueError(
                    "No approved MCWT columns matched the prepared scoped dataframe. "
                    f"Requested columns: {requested}. Available columns: {available}."
                )

        return df[cols_to_keep].copy()

    @staticmethod
    def _normalize_blankish(value: Any) -> Any:
        if pd.isna(value):
            return None
        text = str(value).strip()
        return None if text == "" else value

    @staticmethod
    def _coerce_numeric(series: pd.Series) -> pd.Series:
        if series is None or len(series) == 0:
            return pd.Series(dtype="float64")
        cleaned = (
            series.astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("$", "", regex=False)
            .str.replace("%", "", regex=False)
            .str.strip()
        )
        return pd.to_numeric(cleaned, errors="coerce")

    def _infer_column_type(self, col_name: str, _series: pd.Series, blank_ratio: float, numeric_ratio: float) -> str:
        col_lower = str(col_name).lower()
        if blank_ratio >= 0.999:
            return "Blank"
        if any(k in col_lower for k in ["date", "time", "period", "month", "year"]):
            return "Date"
        if any(k in col_lower for k in ["spend", "cost", "reach", "click", "imps", "impression", "revenue", "views",
                                        "taps", "likes", "shares", "conv", "budget", "frequency", "grp", "trp",
                                        "cpm", "cpc", "ctr", "rate", "ratio"]):
            return "Metric"
        if numeric_ratio >= 0.8:
            if any(k in col_lower for k in ["id", "code", "key"]):
                return "Dimension"
            return "Metric"
        return "Dimension"


def _resolve_dataframe_column(df: pd.DataFrame, column_name: str) -> Optional[str]:
    """Match mapping ``source_column`` to a prepared dataframe column."""
    name = str(column_name or "").strip()
    if not name or df is None or df.empty:
        return None
    if name in df.columns:
        return name
    by_lower = {str(c).casefold(): c for c in df.columns}
    return by_lower.get(name.casefold())


def enrich_mapping_rows_from_dataframe(
    rows: Optional[List[Dict[str, Any]]],
    df: Optional[pd.DataFrame],
) -> List[Dict[str, Any]]:
    """
    Attach ``unique_values`` / ``stats`` from the scoped sheet for UI mapping cards.

    ``mapping_registry`` rows omit samples when drafts are saved; re-hydrate on read.
    """
    if not rows:
        return []
    if df is None or getattr(df, "empty", True):
        return [dict(r) for r in rows if isinstance(r, dict)]

    profiler = SchemaMapper(llm_client=None)
    samples_meta = profiler._get_samples_meta(df)
    enriched: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out = dict(row)
        col_key = _resolve_dataframe_column(df, str(out.get("source_column") or out.get("column_name") or ""))
        if not col_key:
            enriched.append(out)
            continue
        meta = samples_meta.get(col_key) or {}
        if meta.get("unique_values") is not None:
            out["unique_values"] = list(meta.get("unique_values") or [])
        if meta.get("stats"):
            out["stats"] = dict(meta.get("stats") or {})
        if meta.get("inferred_type") and not str(out.get("column_type") or "").strip():
            out["column_type"] = str(meta.get("inferred_type"))
        enriched.append(out)
    return enriched


def mapping_rows_need_sample_enrichment(rows: Optional[List[Dict[str, Any]]]) -> bool:
    """True when any row is missing unique-value samples (registry reload path)."""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if not row.get("unique_values"):
            return True
    return False
