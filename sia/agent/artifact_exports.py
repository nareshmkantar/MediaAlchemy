from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from sia.utils.export_dates import normalize_dataframe_dates_for_export

from .context_packet import apply_context_to_dataframe
from .target_template_utils import prune_dataframe_to_template


def build_pre_transform_dataframe(
    prepared_df: Optional[pd.DataFrame],
    target_template: Optional[Dict[str, Any]],
    approved_mappings: Optional[List[Dict[str, Any]]] = None,
) -> Optional[pd.DataFrame]:
    """Create the analyst-facing pre-transform frame.

    This represents the data immediately after layout/header preparation and
    semantic column mapping decisions, before planner-driven transformation
    tools run. We keep only date / hierarchy / metrics / approved supporting
    columns and exclude explicitly discarded columns.
    """
    if prepared_df is None:
        return None
    if prepared_df.empty:
        return prepared_df.copy()

    pre_df, _ = apply_context_to_dataframe(
        prepared_df,
        approved_mappings=approved_mappings,
        business_rules=None,
    )
    pre_df, _ = prune_dataframe_to_template(
        pre_df,
        target_template or {},
        approved_mappings=approved_mappings,
        business_rules=None,
    )
    return pre_df


def safe_artifact_slug(*parts: Any) -> str:
    tokens: List[str] = []
    for part in parts:
        if part is None:
            continue
        token = re.sub(r"[^A-Za-z0-9._-]+", "_", str(part).strip())
        token = token.strip("._-")
        if token:
            tokens.append(token)
    return "_".join(tokens) or "artifact"


def persist_processing_artifacts(
    output_dir: str | Path,
    job_id: str,
    pre_frames: Dict[str, pd.DataFrame],
    post_df: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """Persist pre/post transformation artifacts and zip bundles."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pre_files: List[str] = []
    for label, frame in pre_frames.items():
        if frame is None:
            continue
        normed = normalize_dataframe_dates_for_export(frame.copy())
        frame_to_write = frame.copy() if normed is None else normed
        path = out_dir / f"{job_id}_pre_transform_{safe_artifact_slug(label)}.xlsx"
        frame_to_write.to_excel(path, index=False, engine="openpyxl")
        pre_files.append(str(path))

    post_path: Optional[str] = None
    if post_df is not None and not post_df.empty:
        post_file = out_dir / f"{job_id}_post_transform.xlsx"
        normed_post = normalize_dataframe_dates_for_export(post_df.copy())
        post_write = post_df.copy() if normed_post is None else normed_post
        post_write.to_excel(post_file, index=False, engine="openpyxl")
        post_path = str(post_file)

    pre_bundle_path: Optional[str] = None
    if pre_files:
        pre_bundle = out_dir / f"{job_id}_pre_transform_bundle.zip"
        with zipfile.ZipFile(pre_bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in pre_files:
                path = Path(file_path)
                zf.write(path, arcname=f"pre_transform/{path.name}")
        pre_bundle_path = str(pre_bundle)

    artifact_bundle_path: Optional[str] = None
    if pre_files or post_path:
        artifact_bundle = out_dir / f"{job_id}_processing_artifacts.zip"
        with zipfile.ZipFile(artifact_bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path in pre_files:
                path = Path(file_path)
                zf.write(path, arcname=f"pre_transform/{path.name}")
            if post_path:
                path = Path(post_path)
                zf.write(path, arcname=f"post_transform/{path.name}")
        artifact_bundle_path = str(artifact_bundle)

    return {
        "pre_transform_files": pre_files,
        "post_transform_file": post_path,
        "pre_transform_bundle": pre_bundle_path,
        "artifacts_bundle": artifact_bundle_path,
    }


def iter_artifact_paths(artifact_files: Optional[Dict[str, Any]]) -> Iterable[Path]:
    payload = artifact_files or {}
    for key in ("pre_transform_files",):
        for item in payload.get(key) or []:
            if item:
                yield Path(item)
    for key in ("post_transform_file", "pre_transform_bundle", "artifacts_bundle"):
        path = payload.get(key)
        if path:
            yield Path(path)
