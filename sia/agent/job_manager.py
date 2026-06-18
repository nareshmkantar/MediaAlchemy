"""
Job Manager - Service for managing processing jobs and HITL workflows.
Provides a clean interface for the web server to interact with the agent's state.
"""
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional

from .context_packet import create_source_registry, make_source_id, normalize_business_rules, normalize_mapping_records

logger = logging.getLogger(__name__)


def _layout_row_is_kept_main_data(row: Dict[str, Any]) -> bool:
    """Match web_server.layout_registry_row_is_main_data (avoid importing web_server)."""
    cat_compact = str(row.get("block_category") or "").strip().lower().replace(" ", "").replace("_", "")
    if cat_compact != "maindata":
        return False
    dec = str(row.get("decision") or "").strip().lower()
    if dec in ("discard", "context"):
        return False
    return dec in ("keep", "approved") or dec == ""


def _derived_layout_complete(job: Dict[str, Any], source_id: str, prog_entry: Dict[str, Any]) -> bool:
    """True if UX says layout done or persisted layout rows exist for this source."""
    if prog_entry.get("layout_complete"):
        return True
    sid = str(source_id)
    return any(str(r.get("source_id")) == sid for r in (job.get("layout_registry") or []))


def _derived_mapping_complete(job: Dict[str, Any], source_id: str, prog_entry: Dict[str, Any]) -> bool:
    """True if saved mappings exist, UX says done, or sheet has no kept main-data (mapping N/A)."""
    sid = str(source_id)
    for row in job.get("mapping_registry") or []:
        if str(row.get("source_id")) == sid:
            return True
    if prog_entry.get("mapping_complete"):
        return True
    layouts = [r for r in (job.get("layout_registry") or []) if str(r.get("source_id")) == sid]
    if not layouts:
        return False
    main_n = sum(1 for x in layouts if _layout_row_is_kept_main_data(x))
    return main_n == 0


class JobManager:
    """
    Manages the lifecycle of processing jobs and their associated HITL states.
    In-memory storage (can be extended to persistent storage later).
    """
    
    def __init__(self):
        # In-memory storage
        self.jobs = {}             # job_id -> job details
        self.review_queue = {}     # job_id -> low-confidence review details
        self.pending_deletions = {}  # job_id -> destructive operation details
        self.pending_checkpoints = {}  # checkpoint_id -> HITL checkpoint review details
        self.pending_schema_mappings = {} # job_id -> MCWT schema mapping state
        self.pending_demarcations = {}    # job_id -> proposed logical blocks
        
        # Thresholds
        self.STOP_AND_ASK_THRESHOLD = 0.6
        self.FLAG_FOR_REVIEW_THRESHOLD = 0.9

    # --- Job Lifecycle ---

    def create_job(self, job_id: str, filename: str, file_path: str, template_path: Optional[str] = None, sheets: Optional[List[str]] = None) -> Dict:
        """Initialize a new processing job."""
        initial_file_id = "file_001"
        initial_source_registry = create_source_registry(
            file_path,
            filename,
            sheets=sheets,
            job_id=job_id,
            file_id=initial_file_id,
        )
        job = {
            "id": job_id,
            "filename": filename,
            "file_path": file_path,
            "sheets": sheets or [], # List of available sheets if Excel
            "data_files": [{
                "file_id": initial_file_id,
                "file_name": filename,
                "file_path": file_path,
                "sheets": sheets or [],
                "upload_order": 0,
            }],
            "template_path": template_path, # Optional target template
            "status": "queued",
            "created_at": datetime.now().isoformat(),
            "steps": [],
            "data_preview": [],
            "schema": None,
            "total_rows": 0,
            "trace": None,
            "requires_review": False,
            "review_reason": "",
            "output_files": {},
            "source_registry": initial_source_registry,
            "source_scope_registry": {},
            "layout_registry": [],
            "mapping_registry": [],
            "business_rules_registry": [],
            "approved_file_relationships": [],
            "relationship_proposals": [],
            "user_notes": [],
            # UX journey (0–4): see docs/UX_STAGES.md
            "current_ux_stage": 0,
            "ux_source_progress": {},  # source_id -> { layout_complete, mapping_complete }
            "relationships_gate_complete": True,
            "processing_heartbeat_at": None,
        }
        self.jobs[job_id] = job
        return job

    def add_data_file(self, job_id: str, filename: str, file_path: str, sheets: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Append a new uploaded data file to an existing job."""
        job = self.jobs.get(job_id)
        if not job:
            return None

        existing_files = list(job.get("data_files") or [])
        file_id = f"file_{len(existing_files) + 1:03d}"
        file_record = {
            "file_id": file_id,
            "file_name": filename,
            "file_path": file_path,
            "sheets": sheets or [],
            "upload_order": len(existing_files),
        }
        existing_files.append(file_record)
        job["data_files"] = existing_files
        if len(existing_files) > 1:
            job["relationships_gate_complete"] = False
        if len(existing_files) == 1:
            job["filename"] = filename
            job["file_path"] = file_path
            job["sheets"] = sheets or []
        else:
            job["filename"] = f"{existing_files[0]['file_name']} +{len(existing_files) - 1} more"
            job["sheets"] = sorted({
                str(sheet)
                for data_file in existing_files
                for sheet in (data_file.get("sheets") or [])
                if sheet is not None
            })

        source_registry = list(job.get("source_registry") or [])
        source_registry.extend(
            create_source_registry(
                file_path,
                filename,
                sheets=sheets,
                job_id=job_id,
                file_id=file_id,
            )
        )
        job["source_registry"] = source_registry
        return file_record

    @staticmethod
    def _unlink_path_safe(file_path: Optional[str]) -> None:
        if not file_path:
            return
        try:
            p = Path(file_path)
            if p.is_file():
                p.unlink()
        except OSError as e:
            logger.warning("Failed to unlink %s: %s", file_path, e)

    def _recompute_job_file_aggregates(self, job: Dict[str, Any]) -> None:
        """Refresh filename, file_path, sheets summary from data_files."""
        data_files = list(job.get("data_files") or [])
        if not data_files:
            job["filename"] = ""
            job["file_path"] = None
            job["sheets"] = []
            return
        if len(data_files) == 1:
            df0 = data_files[0]
            job["filename"] = str(df0.get("file_name") or "")
            job["file_path"] = df0.get("file_path")
            job["sheets"] = list(df0.get("sheets") or [])
        else:
            job["filename"] = f"{data_files[0].get('file_name', '')} +{len(data_files) - 1} more"
            job["sheets"] = sorted(
                {
                    str(s)
                    for df in data_files
                    for s in (df.get("sheets") or [])
                    if s is not None
                }
            )
            job["file_path"] = data_files[0].get("file_path")

    def _prune_relationships_for_source(self, job: Dict[str, Any], source_id: str) -> None:
        sid = str(source_id)

        def _filter_rel(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for r in items or []:
                if not isinstance(r, dict):
                    continue
                sids = [str(x) for x in (r.get("source_ids") or [])]
                if sid not in sids:
                    out.append(r)
                    continue
                sids2 = [x for x in sids if x != sid]
                if len(sids2) >= 2:
                    nr = dict(r)
                    nr["source_ids"] = sids2
                    out.append(nr)
            return out

        job["approved_file_relationships"] = _filter_rel(list(job.get("approved_file_relationships") or []))
        job["relationship_proposals"] = _filter_rel(list(job.get("relationship_proposals") or []))

    def _sync_data_files_with_registry(self, job: Dict[str, Any]) -> None:
        """Drop data_files with no sources; sync sheet lists from source_registry."""
        sources = list(job.get("source_registry") or [])
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for s in sources:
            fid = str(s.get("file_id") or "")
            by_file.setdefault(fid, []).append(s)

        new_files: List[Dict[str, Any]] = []
        for df in sorted(job.get("data_files") or [], key=lambda x: int(x.get("upload_order") or 0)):
            fid = str(df.get("file_id") or "")
            regs = by_file.get(fid) or []
            if not regs:
                self._unlink_path_safe(df.get("file_path"))
                continue
            names: List[Any] = []
            seen = set()
            for s in regs:
                sn = s.get("sheet_name")
                if sn is None:
                    continue
                key = str(sn)
                if key not in seen:
                    seen.add(key)
                    names.append(sn)
            ndf = dict(df)
            ndf["sheets"] = names
            new_files.append(ndf)
        job["data_files"] = new_files
        dfs = job["data_files"]
        if len(dfs) <= 1:
            job["relationships_gate_complete"] = True
        self._recompute_job_file_aggregates(job)

    def remove_source(self, job_id: str, source_id: str) -> bool:
        """Remove one sheet/source from the job (registry, layouts, mappings, disk if file empty)."""
        job = self.jobs.get(job_id)
        if not job:
            return False
        sr = list(job.get("source_registry") or [])
        match = next((s for s in sr if str(s.get("source_id")) == str(source_id)), None)
        if not match:
            return False

        layout_ids = set()
        for row in list(job.get("layout_registry") or []):
            if str(row.get("source_id")) == str(source_id):
                lid = row.get("layout_id")
                if lid is not None:
                    layout_ids.add(str(lid))

        job["layout_registry"] = [
            x for x in (job.get("layout_registry") or []) if str(x.get("source_id")) != str(source_id)
        ]
        job["mapping_registry"] = [
            x for x in (job.get("mapping_registry") or []) if str(x.get("source_id")) != str(source_id)
        ]
        job["business_rules_registry"] = [
            x
            for x in (job.get("business_rules_registry") or [])
            if str(x.get("applies_to_source_id")) != str(source_id)
        ]

        uxp = dict(job.get("ux_source_progress") or {})
        uxp.pop(str(source_id), None)
        job["ux_source_progress"] = uxp

        batch = dict(job.get("demarcation_batch_proposals") or {})
        batch.pop(str(source_id), None)
        job["demarcation_batch_proposals"] = batch

        mm = dict(job.get("mapping_matrix_draft") or {})
        for lid in layout_ids:
            mm.pop(lid, None)
            try:
                mm.pop(int(lid), None)
            except (ValueError, TypeError):
                pass
        job["mapping_matrix_draft"] = mm

        job["source_registry"] = [s for s in sr if str(s.get("source_id")) != str(source_id)]
        self._prune_relationships_for_source(job, str(source_id))
        self._sync_data_files_with_registry(job)
        self.pending_demarcations.pop(job_id, None)
        return True

    def remove_data_file(self, job_id: str, file_id: str) -> bool:
        """Remove an uploaded workbook (all its sheets/sources) from the job."""
        job = self.jobs.get(job_id)
        if not job:
            return False
        sids = [
            str(s.get("source_id"))
            for s in (job.get("source_registry") or [])
            if str(s.get("file_id")) == str(file_id)
        ]
        if sids:
            for sid in sids:
                self.remove_source(job_id, sid)
            return True
        old_dfs = list(job.get("data_files") or [])
        new_dfs = [df for df in old_dfs if str(df.get("file_id")) != str(file_id)]
        if len(new_dfs) == len(old_dfs):
            return False
        gone = next((df for df in old_dfs if str(df.get("file_id")) == str(file_id)), None)
        if gone:
            self._unlink_path_safe(gone.get("file_path"))
        job["data_files"] = new_dfs
        self._recompute_job_file_aggregates(job)
        if len(new_dfs) <= 1:
            job["relationships_gate_complete"] = True
        return True

    def get_job(self, job_id: str) -> Optional[Dict]:
        """Retrieve job details."""
        job = self.jobs.get(job_id)
        if job:
            self._ensure_ux_fields(job)
        return job

    def remove_job(self, job_id: str) -> Optional[Dict]:
        """Remove a job and all associated in-memory state."""
        job = self.jobs.pop(job_id, None)
        if not job:
            return None

        self.review_queue.pop(job_id, None)
        self.pending_deletions.pop(job_id, None)
        self.pending_schema_mappings.pop(job_id, None)
        self.pending_demarcations.pop(job_id, None)

        checkpoint_ids = [
            checkpoint_id
            for checkpoint_id, checkpoint in self.pending_checkpoints.items()
            if checkpoint.get("job_id") == job_id
        ]
        for checkpoint_id in checkpoint_ids:
            self.pending_checkpoints.pop(checkpoint_id, None)

        return job

    def update_job_status(self, job_id: str, status: str, message: Optional[str] = None, step_name: Optional[str] = None):
        """Update job status and optionally add a step."""
        if job_id not in self.jobs:
            return

        hb = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.jobs[job_id]["processing_heartbeat_at"] = hb

        self.jobs[job_id]["status"] = status
        if step_name and message:
            self.jobs[job_id]["steps"].append({
                "step": step_name,
                "message": message,
                "timestamp": datetime.now().isoformat()
            })
            
    def set_job_results(self, job_id: str, schema: Dict, df_preview: List[Dict], total_rows: int, trace: Dict):
        """Store final processing results in the job."""
        if job_id not in self.jobs:
            return
            
        job = self.jobs[job_id]
        job["schema"] = schema
        job["data_preview"] = df_preview
        job["total_rows"] = total_rows
        job["trace"] = trace
        job["overall_confidence"] = trace.get("overall_confidence", 0.0)

    def update_source_metadata(
        self,
        job_id: str,
        sheet_name: Optional[str],
        metadata: Dict[str, Any],
        source_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Upsert source metadata for a given sheet."""
        job = self.jobs.get(job_id)
        if not job:
            return None

        source_registry = list(job.get("source_registry", []))
        target_index = None
        for idx, source in enumerate(source_registry):
            if source_id and source.get("source_id") == source_id:
                target_index = idx
                break
            if not source_id and source.get("sheet_name") == sheet_name:
                target_index = idx
                break

        if target_index is None:
            normalized_source_id = source_id or make_source_id(job_id, None, sheet_name)
            target_index = len(source_registry)
            source_registry.append({
                "source_id": normalized_source_id,
                "file_id": "",
                "file_name": job.get("filename"),
                "file_path": job.get("file_path"),
                "sheet_name": sheet_name,
                "sheet_order": target_index,
                "contains_main_data": True,
                "contains_reference_data": False,
            })

        source_registry[target_index].update(metadata or {})
        source_registry[target_index].setdefault("source_id", source_id or make_source_id(job_id, source_registry[target_index].get("file_id"), sheet_name, job.get("file_path")))
        job["source_registry"] = source_registry
        return source_registry[target_index]

    def save_layout_registry(self, job_id: str, layout_blocks: List[Dict[str, Any]], source_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Replace layout registry entries for the active source."""
        job = self.jobs.get(job_id)
        if not job:
            return []

        scoped_source = job.get("scoped_source", {}) or {}
        selected_sheet = scoped_source.get("sheet_name")
        source_id = self.get_source_id(job_id, selected_sheet, source_id=source_id)
        retained = [
            block for block in job.get("layout_registry", [])
            if block.get("source_id") != source_id
        ]
        retained.extend(layout_blocks or [])
        job["layout_registry"] = retained
        if scoped_source:
            job.setdefault("source_scope_registry", {})[source_id] = dict(scoped_source)
        return retained

    def save_mapping_registry(
        self,
        job_id: str,
        mappings: List[Dict[str, Any]],
        sheet_name: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Replace mapping registry entries for the active source."""
        job = self.jobs.get(job_id)
        if not job:
            return []

        source_id = self.get_source_id(job_id, sheet_name or job.get("mapping_sheet"), source_id=source_id)
        block_ids_in_submit = {
            str(m.get("block_id") or "").strip()
            for m in (mappings or [])
            if isinstance(m, dict) and str(m.get("block_id") or "").strip()
        }
        normalized = normalize_mapping_records(mappings, source_id=source_id)
        if block_ids_in_submit:
            retained = [
                item
                for item in job.get("mapping_registry", [])
                if item.get("source_id") != source_id
                or str(item.get("block_id") or "").strip() not in block_ids_in_submit
            ]
        else:
            retained = [
                item for item in job.get("mapping_registry", [])
                if item.get("source_id") != source_id
            ]
        retained.extend(normalized)
        job["mapping_registry"] = retained
        return normalized

    def save_business_rules(
        self,
        job_id: str,
        rules: List[Dict[str, Any]],
        sheet_name: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Replace business rules for the active source."""
        job = self.jobs.get(job_id)
        if not job:
            return []

        source_id = self.get_source_id(job_id, sheet_name or (job.get("scoped_source") or {}).get("sheet_name"), source_id=source_id)
        normalized = normalize_business_rules(rules, source_id=source_id)
        retained = [
            item for item in job.get("business_rules_registry", [])
            if item.get("applies_to_source_id") != source_id
        ]
        retained.extend(normalized)
        job["business_rules_registry"] = retained
        return normalized

    def get_source(self, job_id: str, source_id: Optional[str] = None, sheet_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Resolve a source entry for a job."""
        job = self.jobs.get(job_id, {})
        for source in job.get("source_registry", []):
            if source_id and source.get("source_id") == source_id:
                return source
            if not source_id and sheet_name is not None and source.get("sheet_name") == sheet_name:
                return source
        return None

    def get_source_id(self, job_id: str, sheet_name: Optional[str], source_id: Optional[str] = None) -> str:
        """Resolve the source_id for a job and sheet."""
        source = self.get_source(job_id, source_id=source_id, sheet_name=sheet_name)
        if source:
            return str(source.get("source_id"))
        return make_source_id(job_id, None, sheet_name)

    def save_file_relationships(self, job_id: str, relationships: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Persist approved relationship decisions for a job."""
        job = self.jobs.get(job_id)
        if not job:
            return []
        normalized: List[Dict[str, Any]] = []
        for idx, relationship in enumerate(relationships or []):
            if not isinstance(relationship, dict):
                continue
            entry = {
                "relationship_id": relationship.get("relationship_id") or f"{job_id}:relationship:{idx}",
                "relationship_kind": relationship.get("relationship_kind") or relationship.get("kind") or "independent",
                "source_ids": [
                    str(x).strip()
                    for x in (relationship.get("source_ids") or [])
                    if x is not None and str(x).strip() != ""
                ],
                "join_keys": list(relationship.get("join_keys") or []),
                "confidence": float(relationship.get("confidence") or 0.0),
                "status": relationship.get("status") or "approved",
                "rationale": str(relationship.get("rationale") or relationship.get("reasoning") or ""),
                "recommended_action": relationship.get("recommended_action") or "",
            }
            dm = relationship.get("duplicate_merge_mode")
            if dm is not None and str(dm).strip():
                entry["duplicate_merge_mode"] = str(dm).strip()
            ddg = relationship.get("duplicate_key_group_decisions")
            if isinstance(ddg, dict) and ddg:
                entry["duplicate_key_group_decisions"] = dict(ddg)
            normalized.append(entry)
        job["approved_file_relationships"] = normalized
        if normalized:
            job["relationships_gate_complete"] = True
        return normalized

    # --- UX stages (0–4) ---

    @staticmethod
    def _ensure_ux_fields(job: Dict[str, Any]) -> None:
        job.setdefault("current_ux_stage", 0)
        job.setdefault("ux_source_progress", {})
        job.setdefault("relationships_gate_complete", True)
        data_files = job.get("data_files") or []
        if len(data_files) <= 1:
            job["relationships_gate_complete"] = True

    def mark_ux_source_layout(self, job_id: str, source_id: str, complete: bool = True) -> None:
        job = self.jobs.get(job_id)
        if not job or not source_id:
            return
        self._ensure_ux_fields(job)
        entry = job["ux_source_progress"].setdefault(
            str(source_id), {"layout_complete": False, "mapping_complete": False}
        )
        entry["layout_complete"] = bool(complete)

    def mark_ux_source_mapping(self, job_id: str, source_id: str, complete: bool = True) -> None:
        job = self.jobs.get(job_id)
        if not job or not source_id:
            return
        self._ensure_ux_fields(job)
        entry = job["ux_source_progress"].setdefault(
            str(source_id), {"layout_complete": False, "mapping_complete": False}
        )
        entry["mapping_complete"] = bool(complete)

    def patch_ux_state(self, job_id: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Merge client updates for UX stage tracking. Returns updated UX slice or None."""
        job = self.jobs.get(job_id)
        if not job:
            return None
        self._ensure_ux_fields(job)
        if "current_ux_stage" in payload:
            try:
                st = int(payload["current_ux_stage"])
                job["current_ux_stage"] = max(0, min(4, st))
            except (TypeError, ValueError):
                pass
        if "relationships_gate_complete" in payload:
            job["relationships_gate_complete"] = bool(payload["relationships_gate_complete"])
        uxp = payload.get("ux_source_progress")
        if isinstance(uxp, dict):
            for sid, rec in uxp.items():
                if not sid or not isinstance(rec, dict):
                    continue
                base = job["ux_source_progress"].setdefault(
                    str(sid), {"layout_complete": False, "mapping_complete": False}
                )
                if "layout_complete" in rec:
                    base["layout_complete"] = bool(rec["layout_complete"])
                if "mapping_complete" in rec:
                    base["mapping_complete"] = bool(rec["mapping_complete"])
        return self.build_ux_stepper_summary(job)

    def build_ux_stepper_summary(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Derived flags for the 0–4 horizontal stepper (see docs/UX_STAGES.md)."""
        self._ensure_ux_fields(job)
        data_files = job.get("data_files") or []
        stage0_complete = len(data_files) > 0
        sr = job.get("source_registry") or []
        sids = [str(s.get("source_id")) for s in sr if s.get("source_id")]
        prog = job.get("ux_source_progress") or {}
        effective_prog: Dict[str, Dict[str, bool]] = {}
        for sid in sids:
            raw = prog.get(sid) or {}
            base = dict(raw) if isinstance(raw, dict) else {}
            effective_prog[sid] = {
                "layout_complete": _derived_layout_complete(job, sid, base),
                "mapping_complete": _derived_mapping_complete(job, sid, base),
            }
        all_layout = bool(sids) and all(effective_prog[sid]["layout_complete"] for sid in sids)
        all_mapping = bool(sids) and all(effective_prog[sid]["mapping_complete"] for sid in sids)
        multi_file = len(data_files) > 1
        rel_ok = bool(job.get("relationships_gate_complete")) or (not multi_file)
        stage1_complete = stage0_complete and bool(sids) and all_layout and all_mapping and rel_ok
        cur = int(job.get("current_ux_stage") or 0)
        return {
            "current_ux_stage": cur,
            "stage0_complete": stage0_complete,
            "stage1_complete": stage1_complete,
            "ux_source_progress": effective_prog,
            "relationships_gate_complete": bool(job.get("relationships_gate_complete")),
            "multi_file": multi_file,
        }

    # --- HITL: Low-Confidence Reviews (Post-Execution) ---

    def get_hitl_decision(self, job_id: str, confidence: float) -> str:
        """Determine HITL routing based on confidence score."""
        if confidence <= self.STOP_AND_ASK_THRESHOLD:
            return "stop_and_ask"
        elif confidence <= self.FLAG_FOR_REVIEW_THRESHOLD:
            return "flag_for_review"
        return "auto_approve"

    def add_to_review_queue(self, job_id: str, filename: str, confidence: float, reason: str, schema_preview: Dict, data_preview: List[Dict], trace_summary: Dict):
        """Add a job to the low-confidence review queue."""
        decision = self.get_hitl_decision(job_id, confidence)
        
        self.review_queue[job_id] = {
            "job_id": job_id,
            "filename": filename,
            "confidence": confidence,
            "decision": decision,
            "reason": reason,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
            "schema_preview": schema_preview,
            "data_preview": data_preview,
            "trace_summary": trace_summary
        }
        
        if job_id in self.jobs:
            self.jobs[job_id]["requires_review"] = True
            self.jobs[job_id]["review_reason"] = reason
            
        logger.info(f"[JobManager] Job {job_id} added to review queue (decision: {decision})")

    @staticmethod
    def _queue_filename_for_checkpoint(job: Dict[str, Any], checkpoint: Dict[str, Any]) -> str:
        """Label for the review queue and checkpoint detail header.

        ``job["filename"]`` aggregates multi-upload jobs as ``File1.xlsx +1 more``. That is
        correct for the job card but misleading for **plan_review**: each plan is scoped to
        one ``process_file`` run (one workbook / sheet). Use ``pending_state`` + registry.
        """
        cp_type = str(checkpoint.get("checkpoint_type") or checkpoint.get("type") or "")
        if cp_type != "plan_review":
            return str(job.get("filename") or "Unknown")
        ps = checkpoint.get("pending_state") or {}
        if not isinstance(ps, dict):
            return str(job.get("filename") or "Unknown")
        sm = ps.get("source_metadata") or {}
        if not isinstance(sm, dict):
            sm = {}
        sid = ps.get("source_id") or sm.get("source_id")
        sheet = (
            ps.get("sheet_name")
            or sm.get("sheet_name")
            or (ps.get("scoped_source") or {}).get("sheet_name")
        )
        fp = ps.get("file_path") or sm.get("file_path")
        if sid:
            for row in job.get("source_registry") or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get("source_id") or "").strip() != str(sid).strip():
                    continue
                fn = str(row.get("file_name") or "").strip()
                sht = str(row.get("sheet_name") or sheet or "").strip()
                if fn and sht:
                    return f"{fn} · {sht}"
                if fn:
                    return f"{fn} · {sht}" if sht else fn
                break
        if fp:
            try:
                stem = Path(str(fp)).name
            except Exception:
                stem = str(fp).replace("\\", "/").split("/")[-1]
            if sheet:
                return f"{stem} · {sheet}"
            return stem
        if sheet:
            return str(sheet)
        return str(job.get("filename") or "Unknown")

    def add_checkpoint_to_review(self, job_id: str, checkpoint: Dict):
        """Bridge an HITL checkpoint into the review queue for analyst visibility."""
        cp_id = checkpoint.get("checkpoint_id", f"cp_{len(self.pending_checkpoints)}")
        cp_type = checkpoint.get("checkpoint_type", "unknown")
        job = self.jobs.get(job_id, {})

        display_fn = JobManager._queue_filename_for_checkpoint(job, checkpoint)

        self.pending_checkpoints[cp_id] = {
            "checkpoint_id": cp_id,
            "job_id": job_id,
            "filename": display_fn,
            "type": cp_type,
            "title": checkpoint.get("title", ""),
            "description": checkpoint.get("description", ""),
            "severity": checkpoint.get("severity", "medium"),
            "reason": checkpoint.get("trigger_reason", ""),
            "trigger_data": checkpoint.get("trigger_data", {}),
            "available_actions": checkpoint.get("available_actions", ["approve", "reject"]),
            "recommended_action": checkpoint.get("recommended_action", "approve"),
            "confidence": checkpoint.get("confidence", 0.5),
            "current_step": checkpoint.get("current_step", "Processing"),
            "created_at": checkpoint.get("created_at", datetime.now().isoformat()),
            "status": "pending",
            "resolved": False,
            "pending_state": checkpoint.get("pending_state"),
        }
        
        if job_id in self.jobs:
            self.jobs[job_id]["requires_review"] = True
        
        logger.info(f"[JobManager] Checkpoint {cp_id} ({cp_type}) added to review for job {job_id}")
        return cp_id

    def resolve_checkpoint(self, checkpoint_id: str, action: str, resolution_data: Optional[Dict[str, Any]] = None) -> bool:
        """Resolve a pending checkpoint review item."""
        checkpoint = self.pending_checkpoints.get(checkpoint_id)
        if not checkpoint:
            return False

        checkpoint["status"] = "resolved"
        checkpoint["resolved"] = True
        checkpoint["resolution_action"] = action
        checkpoint["resolution_data"] = resolution_data or {}
        checkpoint["resolved_at"] = datetime.now().isoformat()
        return True

    def apply_plan_feedback(
        self,
        job_id: str,
        checkpoint_id: str,
        action: str,
        resolution_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Persist analyst feedback from a plan review into reusable planner context."""
        job = self.jobs.get(job_id)
        checkpoint = self.pending_checkpoints.get(checkpoint_id, {})
        if not job:
            return {"notes_added": [], "rules_saved": []}

        resolution_data = resolution_data or {}
        notes_added: List[str] = []
        analyst_notes = str(resolution_data.get("analyst_notes") or "").strip()
        if analyst_notes:
            notes_added.append(f"[Planner {action}] {analyst_notes}")

        original_approval = checkpoint.get("trigger_data", {}).get("approval_items", [])
        resolution_items = resolution_data.get("approval_items") or []
        if original_approval or resolution_items:
            from .planner_decisions import merge_resolved_approval_items

            merged = merge_resolved_approval_items(
                list(original_approval or []),
                list(resolution_items or []),
            )
            if merged and action in {"approve", "modify", "regenerate"}:
                job.setdefault("resolved_planner_decisions", merged)
                summaries = []
                for item in merged[:5]:
                    if isinstance(item, dict):
                        summaries.append(
                            str(
                                item.get("resolution_note")
                                or item.get("summary")
                                or item.get("description")
                                or item.get("title")
                                or "reviewed item"
                            )
                        )
                if summaries:
                    notes_added.append(
                        f"[Planner {action}] Reviewed approval items: {'; '.join(summaries)}"
                    )

        for note in notes_added:
            if note not in job["user_notes"]:
                job["user_notes"].append(note)

        rules_saved: List[Dict[str, Any]] = []
        updated_rule_actions = resolution_data.get("updated_rule_actions") or []
        if updated_rule_actions:
            sheet_name = (job.get("scoped_source") or {}).get("sheet_name")
            existing_rules = list(job.get("business_rules_registry") or [])
            filtered_existing = []
            for rule in existing_rules:
                same_sheet = rule.get("applies_to_sheet") in {None, sheet_name}
                same_target_action = any(
                    isinstance(item, dict)
                    and rule.get("target_column") == item.get("target_column")
                    and rule.get("rule_type") == item.get("action")
                    for item in updated_rule_actions
                )
                if same_sheet and same_target_action:
                    continue
                filtered_existing.append(rule)

            translated_rules = []
            for item in updated_rule_actions:
                if not isinstance(item, dict):
                    continue
                target_column = str(item.get("target_column") or "").strip()
                action_name = str(item.get("action") or "").strip()
                if not target_column or action_name not in {"format", "fill_blank", "default_value", "cross_field_constraint"}:
                    continue
                translated_rules.append({
                    "target_column": target_column,
                    "rule_type": action_name,
                    "rule_expression": str(item.get("value") or ""),
                    "rule_description": str(item.get("description") or f"Analyst-updated via {action}"),
                    "applies_to_sheet": sheet_name,
                    "approved_by": "user",
                })

            if translated_rules:
                combined_rules = filtered_existing + translated_rules
                rules_saved = self.save_business_rules(job_id, combined_rules, sheet_name=sheet_name)

        return {"notes_added": notes_added, "rules_saved": rules_saved}

    def approve_review(self, job_id: str):
        """Approve a low-confidence review."""
        if job_id in self.review_queue:
            self.review_queue[job_id]["status"] = "approved"
            self.review_queue[job_id]["reviewed_at"] = datetime.now().isoformat()
        
        if job_id in self.jobs:
            self.jobs[job_id]["review_status"] = "approved"

    def reject_review(self, job_id: str, reason: str):
        """Reject a low-confidence review."""
        if job_id in self.review_queue:
            review = self.review_queue[job_id]
            review["status"] = "rejected"
            review["reviewed_at"] = datetime.now().isoformat()
            review["rejection_reason"] = reason
        
        if job_id in self.jobs:
            self.jobs[job_id]["review_status"] = "rejected"
            self.jobs[job_id]["rejection_reason"] = reason

    def cancel_job(self, job_id: str, reason: str = "Cancelled by user") -> Optional[Dict[str, Any]]:
        """Mark a job as cancelled and clear any pending review state."""
        job = self.jobs.get(job_id)
        if not job:
            return None

        job["status"] = "cancelled"
        job["requires_review"] = False
        job["review_reason"] = ""
        job["review_status"] = "cancelled"
        job["cancellation_reason"] = reason
        job["steps"].append({
            "step": "Cancelled",
            "message": reason,
            "timestamp": datetime.now().isoformat()
        })

        self.review_queue.pop(job_id, None)
        self.pending_deletions.pop(job_id, None)
        # Drop HITL checkpoints for this job so the Review queue does not keep resolved ghosts
        # (and GET /api/review/checkpoint/<id> does not serve stale forms after cancel).
        stale_cp_ids = [
            cid
            for cid, checkpoint in self.pending_checkpoints.items()
            if checkpoint.get("job_id") == job_id
        ]
        for cid in stale_cp_ids:
            self.pending_checkpoints.pop(cid, None)
        return job

    def apply_review_correction(self, job_id: str, corrections: Dict):
        """Apply human corrections to a review item."""
        if job_id in self.review_queue:
            review = self.review_queue[job_id]
            review["status"] = "corrected"
            review["reviewed_at"] = datetime.now().isoformat()
            review["corrections"] = corrections
            
        if job_id in self.jobs:
            self.jobs[job_id]["review_status"] = "corrected"
            self.jobs[job_id]["corrections"] = corrections

    # --- HITL: Destructive Approvals (Pre-Execution Pause) ---

    def pause_for_destructive_approval(self, job_id: str, previews: List[Dict], pending_state: Dict, low_confidence_items: List[Dict] = None):
        """Pause processing for destructive operation approval or low confidence items."""
        reason = f"Awaiting approval for {len(previews)} destructive operations"
        if low_confidence_items:
            reason += f" and {len(low_confidence_items)} low-confidence items"
            
        self.update_job_status(job_id, "awaiting_approval", reason, "HITL Pause")
        
        self.pending_deletions[job_id] = {
            "job_id": job_id,
            "previews": previews,
            "pending_tools": [p.get("tool_name") for p in previews],
            "low_confidence_items": low_confidence_items or [], # NEW: Store for UI
            "pending_state": pending_state,
            "created_at": datetime.now().isoformat(),
            "approved": False
        }
        
    def approve_deletions(self, job_id: str, approved_indices: Optional[List[int]] = None):
        """Approve destructive operations."""
        if job_id not in self.pending_deletions:
            return
            
        pending = self.pending_deletions[job_id]
        pending["approved"] = True
        pending["approved_at"] = datetime.now().isoformat()
        pending["approved_tool_indices"] = approved_indices # None means all
        
        if job_id in self.jobs:
            self.jobs[job_id]["destructive_approved"] = True
            self.jobs[job_id]["approved_tool_indices"] = approved_indices

    def reject_deletions(self, job_id: str):
        """Reject destructive operations and mark to skip them."""
        if job_id not in self.pending_deletions:
            return
            
        pending = self.pending_deletions[job_id]
        pending["approved"] = False
        pending["rejected_at"] = datetime.now().isoformat()
        
        if job_id in self.jobs:
            self.jobs[job_id]["destructive_approved"] = True
            self.jobs[job_id]["approved_tool_indices"] = [] # Empty means skip all

    # --- Unified Review Status ---

    def get_all_pending_reviews(self) -> List[Dict]:
        """List all items requiring human attention (both types)."""
        pending = []
        
        # 1. Low-confidence reviews
        for job_id, item in self.review_queue.items():
            if item.get("status") == "pending":
                job = self.jobs.get(job_id, {})
                pending.append({
                    "job_id": job_id,
                    "filename": item.get("filename"),
                    "confidence": item.get("confidence"),
                    "decision": item.get("decision"),
                    "reason": item.get("reason"),
                    "created_at": item.get("created_at"),
                    "type": "confidence_review",
                    "current_step": job.get("current_step", "Plan Execution"),
                    "pending_tools": []
                })
                
        # 2. Destructive approvals
        for job_id, item in self.pending_deletions.items():
            if not item.get("approved") and item.get("rejected_at") is None:
                job = self.jobs.get(job_id, {})
                previews = item.get('previews', [])
                pending_tools = item.get('pending_tools', [])
                
                # Build descriptive reasoning from previews
                if previews:
                    reasons = []
                    for p in previews:
                        tool_name = p.get('tool_name', 'Unknown')
                        row_count = p.get('total_rows_to_delete', 0)
                        col_count = p.get('total_columns_to_delete', 0)
                        
                        if row_count > 0 and col_count > 0:
                            reasons.append(f"{tool_name}: deleting {row_count} rows and {col_count} columns")
                        elif row_count > 0:
                            reasons.append(f"{tool_name}: deleting {row_count} rows")
                        elif col_count > 0:
                            reasons.append(f"{tool_name}: deleting {col_count} columns")
                        else:
                            reasons.append(f"{tool_name}: requested execution")
                    reason_str = " | ".join(reasons)
                else:
                    reason_str = item.get("reason", "Approval required for destructive operations")
                
                # Extra context if low confidence items exist
                low_conf = item.get("low_confidence_items", [])
                if low_conf:
                    reason_str += f" (+ {len(low_conf)} low-confidence items)"

                primary_tool = pending_tools[0] if pending_tools else "Destructive Ops"
                
                pending.append({
                    "job_id": job_id,
                    "filename": job.get("filename", "Unknown"),
                    "confidence": job.get("trace", {}).get("overall_confidence", 0.5),
                    "decision": "stop_and_ask",
                    "reason": reason_str,
                    "created_at": item.get("created_at"),
                    "type": "destructive_approval",
                    "current_step": f"Tool: {primary_tool}",
                    "pending_tools": pending_tools
                })
        
        # 3. HITL Checkpoint reviews (structural, checksum, etc.)
        for cp_id, item in self.pending_checkpoints.items():
            if item.get("status") == "pending":
                jid = item.get("job_id")
                if not jid or jid not in self.jobs:
                    continue
                job = self.jobs[jid]
                if job.get("status") == "cancelled":
                    continue
                cp_type = item.get("type", "unknown")
                if cp_type == "structural_review" and self._is_structural_checkpoint_redundant(job, item):
                    continue
                
                # Map checkpoint type to display icon
                type_labels = {
                    "structural_review": "📐 STRUCTURAL",
                    "checksum_failure": "📊 CHECKSUM",
                    "plan_review": "📋 PLAN",
                    "file_relationship_review": "🧩 RELATIONSHIP",
                    "low_confidence": "🔍 CONF",
                    "verification_stall": "🔄 STALL",
                    "column_decision": "🎯 COLUMNS"
                }
                
                pending.append({
                    "job_id": item.get("job_id"),
                    "checkpoint_id": cp_id,
                    "filename": item.get("filename", job.get("filename", "Unknown")),
                    "confidence": item.get("confidence", 0.5),
                    "decision": "stop_and_ask" if item.get("severity") in ["high", "critical"] else "flag_for_review",
                    "reason": item.get("reason", ""),
                    "created_at": item.get("created_at"),
                    "type": cp_type,
                    "type_label": type_labels.get(cp_type, f"🔔 {cp_type.upper()}"),
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "severity": item.get("severity", "medium"),
                    "trigger_data": item.get("trigger_data", {}),
                    "available_actions": item.get("available_actions", []),
                    "recommended_action": item.get("recommended_action", "approve"),
                    "current_step": item.get("current_step", "Processing"),
                    "pending_tools": []
                })
        
        return pending

    @staticmethod
    def _is_structural_checkpoint_redundant(job: Dict[str, Any], checkpoint: Dict[str, Any]) -> bool:
        if not isinstance(job, dict):
            return False
        scoped_source = job.get("scoped_source") or {}
        trigger_data = checkpoint.get("trigger_data") or {}
        source_id = trigger_data.get("source_id")
        if isinstance(scoped_source, dict) and scoped_source.get("header_row") is not None:
            return True
        if source_id:
            source_scope = ((job.get("source_scope_registry") or {}).get(source_id)) or {}
            if isinstance(source_scope, dict) and source_scope.get("header_row") is not None:
                return True
        return False

    def get_pending_review_count(self) -> int:
        """Simple count for UI badge."""
        q_count = sum(1 for item in self.review_queue.values() if item.get("status") == "pending")
        d_count = sum(1 for item in self.pending_deletions.values() if not item.get("approved") and item.get("rejected_at") is None)
        def _checkpoint_counts_pending(cp_id: str, item: Dict) -> bool:
            if item.get("status") != "pending":
                return False
            jid = item.get("job_id")
            if not jid or jid not in self.jobs:
                return False
            job = self.jobs[jid]
            if job.get("status") == "cancelled":
                return False
            if item.get("type") == "structural_review" and self._is_structural_checkpoint_redundant(job, item):
                return False
            return True

        c_count = sum(1 for cp_id, item in self.pending_checkpoints.items() if _checkpoint_counts_pending(cp_id, item))
        return q_count + d_count + c_count

    def get_job_count(self) -> int:
        """Get total number of jobs."""
        return len(self.jobs)

# Global instance
job_manager = JobManager()
