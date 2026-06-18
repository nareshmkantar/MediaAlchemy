# UX stages 0–4 vs pipeline

The **web stepper** shows three pills: **Upload**, **Layout & mapping**, **Plan & run**. Backend `current_ux_stage` remains 0–4; the UI collapses stages 2–4 into the last pill until the job completes.

The table below describes backend UX stages vs pipeline work (not separate stepper labels after setup).

Product-facing backend stages map to work as follows.

| UX stage | User-facing intent | Backend / notes |
|----------|-------------------|-----------------|
| **0** | Upload data + optional JSON scope; start guided setup | Job created (`stage0_complete` when `data_files` non-empty). |
| **1** | Per source/sheet: layout (demarcation) then semantic column mapping | `ux_source_progress[source_id].layout_complete` after demarcation submit; `.mapping_complete` after mapping submit. Multi-file jobs also require `relationships_gate_complete` (set when file relationships are saved) or a single data file. |
| **2** | Drop blank columns, column typing, date interpretation, **date normalisation**, then **hygiene** | Matches pipeline order in `sia/tools/pipeline_catalog.py`: `column_typing` and `date_normalisation` use lower `sort_key` than `hygiene`, so tools run **after** canonical dates exist, **before** row-level junk removal and **before** value standardisation. |
| **3** | Value standardisation (`map_values`, `format`, `fuzzy_standardize`, …) | `value_standardisation` stage only. |
| **4** | Reshape / aggregate / validate | `reshaping_aggregation` then `validation`. |

## API fields (job document)

- `current_ux_stage` (0–4): client may PATCH; server bumps to at least **2** when full `process_all_sources` processing starts.
- `ux_source_progress`: map `source_id` → `{ layout_complete, mapping_complete }`.
- `relationships_gate_complete`: `false` when a second data file is added; set `true` when relationship decisions are persisted (`save_file_relationships`) or automatically for single-file jobs.

`GET /api/status/<job_id>` includes `ux_stepper_summary` with derived booleans `stage0_complete`, `stage1_complete`, and copies of the above for the stepper.

## PATCH

`PATCH /api/jobs/<job_id>/ux-state` with JSON body, e.g.:

```json
{
  "current_ux_stage": 3,
  "relationships_gate_complete": true,
  "ux_source_progress": {
    "source-id-123": { "layout_complete": true, "mapping_complete": true }
  }
}
```

## Processing guard

`POST /api/process/<job_id>` with `process_all_sources: true` **does not** block on `stage1_complete`: partial-job runs are allowed (the server logs when setup is incomplete). `stage1_complete` remains meaningful for the stepper and setup UX. Quick Process from Upload still uses `process_all_sources: false` for the single-sheet path.

## Handoff after stage 1 (guided setup)

1. User completes demarcation and mapping for every source (and relationships for multi-file jobs).
2. **Run pipeline** on the setup page (or **Skip & process**): submits mapping as needed, sets `current_ux_stage` to **2** via `PATCH /api/jobs/<id>/ux-state`, and navigates to **Processing**.
3. On **Processing**, the client calls `POST /api/process` with `process_all_sources: true` when autostart fires (sessionStorage from setup, or first status poll while not already running), without requiring `stage1_complete`. There is no separate “harmonize / shape” wizard step in the UI; value harmonisation remains a product concern **after** transforms when you design that flow.

Until the backend supports stopping between internal stages, internal stages 2–4 are still one worker run; only the pre-run checklist was removed.
