"""Filesystem-backed artifact store for SchemaAgent.

Artifacts are stored under:

    <root>/<job_id>/<source_id or '_global'>/<kind>/<fingerprint>.<ext>

Each write returns an :class:`ArtifactRef` which carries enough information
to record a row in the metadata store. Dataframes are stored as Parquet when
pyarrow/fastparquet are available and fall back to CSV otherwise so the
store works in minimal environments (for example CI).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd


class ArtifactKind(str, Enum):
    """Canonical artifact kinds produced by the pipeline."""

    RAW_SHEET_SNAPSHOT = "raw_sheet_snapshot"
    SCOPED_DATAFRAME = "scoped_dataframe"
    PREPARED_DATAFRAME = "prepared_dataframe"
    NORMALIZED_DATAFRAME = "normalized_dataframe"
    CONTEXT_BLOCK_SNIPPETS = "context_block_snippets"
    INTERPRETED_CONTEXT = "interpreted_context"
    SCHEMA_PROFILE = "schema_profile"
    TRACE_SNAPSHOT = "trace_snapshot"


_DATAFRAME_KINDS = {
    ArtifactKind.RAW_SHEET_SNAPSHOT,
    ArtifactKind.SCOPED_DATAFRAME,
    ArtifactKind.PREPARED_DATAFRAME,
    ArtifactKind.NORMALIZED_DATAFRAME,
}


@dataclass
class ArtifactRef:
    """Lightweight reference to a persisted artifact."""

    job_id: str
    source_id: Optional[str]
    kind: ArtifactKind
    version: int
    fingerprint: str
    path: str
    format: str
    size_bytes: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        data = self.__dict__.copy()
        data["kind"] = self.kind.value if isinstance(self.kind, ArtifactKind) else str(self.kind)
        return data


class ArtifactStore:
    """Persist and load pipeline artifacts on the filesystem."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _artifact_dir(self, job_id: str, source_id: Optional[str], kind: ArtifactKind) -> Path:
        source_segment = source_id or "_global"
        safe_source = source_segment.replace("/", "_").replace("\\", "_").replace(":", "_")
        target = self.root / job_id / safe_source / kind.value
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _write_dataframe(self, df: pd.DataFrame, target_dir: Path, fingerprint: str) -> tuple[Path, str]:
        parquet_path = target_dir / f"{fingerprint}.parquet"
        try:
            df.to_parquet(parquet_path, index=False)
            return parquet_path, "parquet"
        except Exception:
            csv_path = target_dir / f"{fingerprint}.csv"
            df.to_csv(csv_path, index=False)
            return csv_path, "csv"

    def _write_json(self, payload: Any, target_dir: Path, fingerprint: str) -> tuple[Path, str]:
        json_path = target_dir / f"{fingerprint}.json"
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return json_path, "json"

    def save(
        self,
        *,
        job_id: str,
        source_id: Optional[str],
        kind: ArtifactKind,
        payload: Any,
        fingerprint: str,
        version: int = 1,
    ) -> ArtifactRef:
        """Persist ``payload`` and return an :class:`ArtifactRef`.

        Dataframe-typed artifacts are written as Parquet when possible, with
        a CSV fallback. All other artifacts are written as pretty JSON.
        """
        target_dir = self._artifact_dir(job_id, source_id, kind)
        if kind in _DATAFRAME_KINDS:
            if not isinstance(payload, pd.DataFrame):
                raise TypeError(f"Artifact kind {kind.value} requires a pandas DataFrame")
            path, fmt = self._write_dataframe(payload, target_dir, fingerprint)
        else:
            path, fmt = self._write_json(payload, target_dir, fingerprint)
        size_bytes = path.stat().st_size if path.exists() else 0
        return ArtifactRef(
            job_id=job_id,
            source_id=source_id,
            kind=kind,
            version=version,
            fingerprint=fingerprint,
            path=str(path),
            format=fmt,
            size_bytes=size_bytes,
        )

    def load(self, ref: ArtifactRef) -> Any:
        """Load an artifact back from disk using its :class:`ArtifactRef`."""
        path = Path(ref.path)
        if not path.exists():
            raise FileNotFoundError(f"Artifact file missing: {path}")
        if ref.kind in _DATAFRAME_KINDS:
            if ref.format == "parquet":
                return pd.read_parquet(path)
            return pd.read_csv(path)
        return json.loads(path.read_text(encoding="utf-8"))

    def clear_job(self, job_id: str) -> None:
        """Remove all artifacts for ``job_id`` from the filesystem."""
        target = self.root / job_id
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
