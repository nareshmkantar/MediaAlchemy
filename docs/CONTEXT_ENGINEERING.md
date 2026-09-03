# Context Engineering Roadmap

This document is the implementation backlog for **source-scoped context** in SchemaAgent: how context is built, propagated, verified, persisted, and debugged. It applies the principle:

> **Context should be pulled by evidence, not pushed by memory.**

Use it as a checklist — implement phases in order; each phase unlocks the next.

---

## 1. Mental model

### 1.1 Context hierarchy (target)

```
Workbook context     job_id, template, user_notes, source_registry
    ↓
Sheet context        source_id, sheet_name, scope, mappings, rules, relationships (peer)
    ↓
Block context        main_blocks vs context_blocks → snippets → interpreted fields
    ↓
Row / execution      current_df, tool literals (must reconcile to sheet/block scope)
```

### 1.2 Propagation boundaries

| Boundary | Policy |
|----------|--------|
| Workbook → Sheet | ✅ Allow (registry, template) |
| Sheet → Block | ✅ Allow (demarcation, layout_registry) |
| Block → Field | ✅ Allow with evidence (snippet line → field) |
| Cross-sheet | ⚠️ Only with explicit relationship + evidence |
| Cross-workbook | ❌ Never by default |
| Plan memory → new sheet | ❌ Block; rebind or replan per source |

### 1.3 Concepts to implement

| Concept | Purpose | Status today |
|---------|---------|--------------|
| **Scope tags** | Every field knows `workbook \| sheet \| block \| cross_sheet` | ✅ `ScopedField` (Phase 1–2) |
| **Provenance** | Every field knows `source_id`, `block_label`, `line`, `hop` | ✅ `evidence[]` + `scoped_fields` (Phase 2) |
| **Confidence** | Propagation gated by confidence threshold | Partial (plan/structure confidence only) |
| **Hop count** | Cross-scope use increments hops; max hops enforced | ✅ `hop` on evidence + `propagate_field` (Phase 3) |
| **Context diff log** | Before/after at build → plan → rebind → stamp → output | ✅ `job.context_diff_log` (Phase 4) |
| **Context verifier** | Deterministic gate before execute / after finalize | ✅ `ContextVerifier` (Phase 5) |
| **Fingerprinted artifacts** | Snippets + interpreted context persisted per source | ✅ `sia/context/artifacts.py` (Phase 2) |
| **Debug surfacing** | UI shows scope, provenance, diffs, eval chain | Partial (Pipeline Evals + context provenance API) |

---

## 2. Current state (baseline)

### What works

- Per-source `build_context_packet()` (`sia/agent/context_packet.py`)
- Demarcation routes metadata → `context_blocks` (`layout_registry`)
- `_interpret_context_block_snippets()` → `interpreted_context.fields` + `evidence`
- `local_context_fields()` + tab inference (`sia/integrity/context_isolation.py`)
- Execute-time `rebind_source_local_plan_literals()`
- Finalize-time `apply_local_context_dimensions()` + `align_output_metrics_to_clean_template()`
- Pipeline evals: `context_grounding_critical`, `dimension_mismatch`, `metric_mismatch`, `union_false_duplicate`, `memory_scope`
- Integration tests: `tests/integration/test_context_propagation.py`, `tests/evals/test_cp07_pipeline.py`

### Known failure modes (documented in `docs/ARCHITECTURE.md`)

1. **`planning_summary` drift** — `resolve_mapping_node` uses legacy `_summarize_mapping_state`, dropping `interpreted_context` / `layout_summary`.
2. **Rebuilt-on-demand** — snippets re-read from workbook every packet build; no fingerprint invalidation wired.
3. **Execution memory bleed** — row bodies and plan literals can cross sheets before evals fire.
4. **Stamp depends on correct local** — if `local_context_fields` is wrong, `apply_local_context_dimensions` reinforces bleed.
5. **Debug UI** — `compact_context_packet()` omits `interpreted_context.fields` and evidence.
6. **No propagation audit trail** — cannot answer “where did `market=UK` come from?” in one click.

---

## 3. Phased implementation backlog

### Phase 0 — Stop active bleed (P0, ~1–2 days)

**Goal:** Fix known bugs so multisheet jobs don’t silently cross-contaminate.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 0.1 | **Fix planning_summary drift** — In `resolve_mapping_node`, always merge mapping supplement via `build_canonical_planning_view()`, never replace with `_summarize_mapping_state` alone. | `sia/agent/nodes.py` | Planner prompt includes `interpreted_context` after mapping stage; regression test. |
| 0.2 | **Harden finalize order** — Assert `lineage.sheet_name` present before stamp; log warning + eval violation if missing. | `web_server.py`, `context_isolation.py` | Radio_DE with missing lineage → `context_completeness` critical. |
| 0.3 | **Stamp dimensions after align** — Today: stamp → align metrics. Consider: align metrics → stamp dimensions so metric repair cannot skip dimension overwrite. Evaluate both orders; pick the one that prevents UK surviving on wrong sheet. | `web_server.py` `_finalize_per_source_output_frame` | CP-07 bleed frame ends with `market=DE` when local is correct. |
| 0.4 | **Block relationship review when critical gate fails** — Optionally pause before duplicate UI when `dimension_mismatch` / `metric_mismatch` on any source (today: warn only). | `web_server.py` `_queue_post_execution_relationship_review` | Evals FAIL → analyst sees bleed warning before treating duplicates. |
| 0.5 | **Expose `local_context` in context_isolation eval UI** — Show expected vs actual dimensions in `pipeline-evals-render.js`. | `web/js/pipeline-evals-render.js`, `sia/evals/display.py` | Radio_DE eval row shows `local_context: {market: DE}` + violations. |

---

### Phase 1 — Canonical context contract (P0, ~2–3 days)

**Goal:** One authoritative packet shape; no parallel summaries.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 1.1 | **Enforce `build_canonical_planning_view` everywhere** | ✅ Done |
| 1.2 | **Add `ScopedField` dataclass** | ✅ Done (`sia/context/scoped_field.py`) |
| 1.3 | **Refactor `enrich_interpreted_fields` → `build_scoped_fields()`** | ✅ Done |
| 1.4 | **`local_context_fields` reads scoped fields** | ✅ Done (`local_scoped_fields` + flat compat) |
| 1.5 | **Packet schema version** | ✅ Done (`CONTEXT_PACKET_SCHEMA_VERSION = 2`, `interpreted_context.scoped_fields`) |

---

### Phase 2 — Provenance & scope (P1, ~3–4 days)

**Goal:** Every local dimension is traceable to evidence.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 2.1 | **Extend evidence records** — Add `source_id`, `sheet_name`, `scope`, `hop=0`, `confidence=1.0` to `_maybe_capture_field`. | ✅ Done (`context_packet.py`) |
| 2.1b | **Tab inference evidence** — When tab fills a gap, append evidence `{scope: sheet, line: "tab:Radio_DE"}`. | ✅ Done (`append_tab_inference_evidence`) |
| 2.2 | **`interpreted_context.scoped_fields`** — Parallel to `fields` during migration; planner prefers scoped view. | ✅ Done (`sia/context/prompt.py`, `planner.py`) |
| 2.3 | **Persist snippets per source** — On demarcation approve + packet build, write `CONTEXT_BLOCK_SNIPPETS` and `INTERPRETED_CONTEXT` via `ArtifactStore`. | ✅ Done (`sia/context/artifacts.py`) |
| 2.4 | **Fingerprint inputs** — Include layout_registry hash, sheet name, block bounds in `compute_source_fingerprint()`. | ✅ Done (`sia/context/fingerprint.py`) |
| 2.5 | **API: `GET /api/debug/<job_id>/context/<source_id>`** — Returns scoped fields, evidence, snippets, local flat view, packet lineage. | ✅ Done (`web_server.py`, debug panel button) |

---

### Phase 3 — Context boundaries & propagation rules (P1, ~4–5 days)

**Goal:** Enforce the Allow / ⚠ / ❌ table in code.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 3.1 | **`ContextBoundaryPolicy` class** — Methods: `may_propagate(from_scope, to_scope, *, relationship?, evidence?)`. | ✅ Done (`sia/context/boundary_policy.py`) |
| 3.2 | **`propagate_field(target_packet, field, policy)`** — Increments `hop`; rejects if `hop > max_hops` (default 1 for cross-sheet). | ✅ Done (`sia/context/propagation.py`) |
| 3.3 | **Cross-sheet context only via relationships** — Strip peer `market`/`channel` from planner prompt unless `relationship_context.allows_enrichment`. | ✅ Done (`sia/context/planner_context.py`, `planner.py`) |
| 3.4 | **Resume / HITL scope guard** — On resume, rebuild packet from registry; never merge prior sheet’s `interpreted_context` into new `source_id`. | ✅ Done (`merge_plan_review_context_packet`, `_RESUME_SCOPE_FRESH_ONLY_KEYS`) |
| 3.5 | **Plan source binding** — Persist `plan.source_id` on every `ExtractionPlan`; reject execute if `state.source_id != plan.source_id`. | ✅ Done (`planner.py`, `execute_tools_node`) |

---

### Phase 4 — Context diff logging (P1, ~2–3 days)

**Goal:** Answer “where did this value come from?” at any pipeline point.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 4.1 | **`ContextDiffLogger` on job** — `job["context_diff_log"] = [{ stage, source_id, field, before, after, reason, hop }]`. | ✅ Done (`sia/context/diff_log.py`) |
| 4.2 | **Hook points** — Log at: packet build, plan generate, rebind, apply_local_context_dimensions, align_metrics, finalize assess. | ✅ Done |
| 4.3 | **Dimension column snapshot** — Before/after stamp, record `distinct(market)` per source. | ✅ Done (`log_dimension_column_snapshots`) |
| 4.4 | **Debug UI: Context trail panel** — Timeline filterable by source_id + field name. | ✅ Done (`debug.js`, Pipeline Evals tab) |
| 4.5 | **Export context trail** — Include in debug JSON download. | ✅ Done (`GET /api/download/<job_id>/debug-json`) |

---

### Phase 5 — Context verifier agent (P1, ~3–4 days)

**Goal:** Deterministic gate separate from LLM `OutputVerifier`.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 5.1 | **`ContextVerifier` class** — Pure functions, no LLM. Checks: plan literals vs scoped fields, output dimensions vs local, metric fingerprint vs clean template, packet lineage vs active source. | ✅ Done (`sia/context/verifier.py`) |
| 5.2 | **Pre-execute gate** — `verify_plan_context(plan, packet) → VerificationResult` in `execute_tools_node` before first tool. | ✅ Done (`nodes.py`) |
| 5.3 | **Post-finalize gate** — Already partially done; unify under `ContextVerifier.verify_output_frame()`. | ✅ Done (`web_server.py`, `assess_source_context_isolation`) |
| 5.4 | **Optional LLM context judge (Tier 3)** — Extend `PipelineEvalRunner.summarize_for_judge()` with scoped fields + diff tail. | ✅ Done (`sia/evals/runner.py`) |
| 5.5 | **Do not confuse with `OutputVerifier`** — Document: OutputVerifier = schema/rules; ContextVerifier = scope/provenance. | ✅ Done (`docs/ARCHITECTURE.md`) |

---

### Phase 6 — Debug & observability UI (P1, ~3–5 days) ✅

**Goal:** Operators can debug context without reading Python traces.

**Approach:** Extend existing Debug tab (Pipeline Evals + context trail + state snapshots) — no separate `context-inspector.js` page.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 6.1 | **Context inspector panel** — Per source: lineage, scoped fields, evidence, block snippets (replaces raw JSON dump). | ✅ `pipeline-evals-render.js`, `debug.html` tab 5 | Radio_DE shows Germany from block, not UK. |
| 6.2 | **Enrich state snapshots** — `compact_context_packet` includes `interpreted_context.fields`, `evidence`, `local_context_fields()`, `lineage`. | ✅ `sia/debug/state_snapshot.py` | Snapshot card shows market provenance. |
| 6.3 | **Pipeline Evals: context drill-down** — On `dimension_mismatch`, link to context trail + snapshot. | ✅ `pipeline-evals-render.js`, `debug.js` | One click from eval to evidence. |
| 6.4 | **Relationship review: dimension mismatch banner** — When duplicate pairs cross sheets with same market/channel, show “likely context bleed” not just “duplicate”. | ✅ `web/js/review.js` | CP-07 duplicate shows bleed hint. |
| 6.5 | **Processing view: active context chip** — Show `sheet · market · channel` from scoped fields during per-source run. | ✅ `processing.js`, `processing.html`, `web_server.py` | Analyst sees active scope while waiting. |
| 6.6 | **Evals dashboard: context health score** — Per-job rollup: % sources passing context_isolation, bleed count, max hop. | ✅ `web/js/evals.js`, `display.py` | Job list shows context column. |

---

### Phase 7 — Confidence-based propagation (P2, ~2–3 days) ✅

**Goal:** Low-confidence context does not overwrite high-confidence evidence.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 7.1 | **Confidence on scoped fields** — Block parse = 1.0; tab inference = 0.7; planner assumption = 0.5; cross-sheet = 0.3 unless relationship approved. | ✅ `confidence.py`, `context_isolation.py`, `propagation.py` | Tab does not override block market. |
| 7.2 | **`merge_scoped_fields(base, overlay)`** — Higher confidence wins; equal → narrower scope wins (block > sheet > workbook). | ✅ `sia/context/merge.py` | Germany (block, 1.0) beats UK (planner, 0.5). |
| 7.3 | **Propagation threshold** — `MIN_CONFIDENCE_TO_STAMP = 0.6` for finalize; below → HITL checkpoint `CONTEXT_AMBIGUITY_REVIEW`. | ✅ `stamp_policy.py`, `hitl.py`, `web_server.py` | Ambiguous market triggers analyst confirm. |
| 7.4 | **Eval: `low_confidence_context`** — Advisory when stamp uses tab-only fields on multisheet job. | ✅ `stamp_policy.py`, `verifier.py` | Surfaced in Evals UI. |

---

### Phase 8 — Hop tracking & cross-source controls (P2, ~3 days) ✅

**Goal:** Track and limit how far context travels.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 8.1 | **`hop` on every propagation event** — 0 = same block/sheet; 1 = sheet-level inference; 2+ = cross-sheet (should be rare). | ✅ `propagation.py`, `diff_log.py` | Log shows hop count. |
| 8.2 | **`MAX_CONTEXT_HOPS = 1`** — Cross-sheet literal copy blocked at hop 2. | ✅ `boundary_policy.py`, `evals/context_propagation.py` | Eval `context_hop_exceeded`. |
| 8.3 | **Relationship-gated enrichment** — Lookup/union approval unlocks specific fields only (e.g. `campaign_group` from dimension file, not `market`). | ✅ `boundary_policy.py`, `propagation.py` | Union does not import peer dimensions. |
| 8.4 | **Derived field context** — When `DerivedFieldPlan` lands (ARCHITECTURE §9), propagation requires `join_context` + `approved_by`. | ✅ Spec (`sia/context/derived.py`) | Spec only until derived fields ship. |

---

### Phase 9 — Persistence & multi-process safety (P2, ~5+ days) ✅

**Goal:** Context survives restarts; reproducible packet builds.

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 9.1 | **Wire `ArtifactStore` into packet build** — Read-through cache for snippets/interpreted context. | ✅ `context_packet.py`, `artifacts.py` | Second packet build hits disk cache. |
| 9.2 | **SQLite `artifacts` table** — Store refs + fingerprints per source (`context_block_snippets`, `interpreted_context`). | ✅ `storage/schema.py`, `artifacts.py` | Job reload restores context. |
| 9.3 | **JobManager facade** — Optional persistence via `SCHEMA_AGENT_METADATA_DB`. | ✅ `job_manager.py` | Render deploy uses durable store. |
| 9.4 | **Normalized dataframe artifact** — Post-finalize frame saved with context fingerprint in metadata. | ✅ `web_server.py`, `artifacts.py` | Debug loads exact frame shown in relationship review. |

---

### Phase 10 — Tests & regression harness (ongoing) ✅

| # | Change | Files | Acceptance |
|---|--------|-------|------------|
| 10.1 | **CP-07 E2E integration** — Full multisheet job: expect eval failures on bleed, pass after fix. | ✅ `tests/integration/test_cp07_multisheet.py` | CI gate (`pytest -m regression`). |
| 10.2 | **Context diff golden tests** — Snapshot diff log for rebinding scenario. | ✅ `tests/context/test_diff_log.py`, `fixtures/context_flow/rebind_diff_golden.json` | Regression on rebind messages. |
| 10.3 | **Boundary policy matrix test** — Parametrize all scope pairs. | ✅ `tests/context/test_boundary_policy_matrix.py` | 25/25 scope pairs covered. |
| 10.4 | **UI smoke** — Context inspector renders fixture job. | ✅ `tests/web/test_context_inspector_smoke.py` | Fields visible (Node smoke when available). |
| 10.5 | **Property: stamp idempotence** — `apply_local_context_dimensions` twice → same frame. | ✅ `tests/integrity/test_context_isolation.py` | No drift on double finalize. |

---

## 4. Eval matrix (what catches what)

| Symptom | Eval stage | Violation type | When to add |
|---------|------------|----------------|-------------|
| Plan has `market=UK` on Radio_DE | `plan` | `context_grounding_critical` | Exists |
| Wrong source plan resumed | `plan` | `memory_scope` | Exists |
| Output `market` ≠ local | `context_isolation` | `dimension_mismatch` | Exists |
| Output metrics ≠ clean template | `context_isolation` | `metric_mismatch` | Exists |
| Union false duplicate | `collation` | `union_false_duplicate` | Exists |
| Missing lineage at finalize | `context_isolation` | `context_lineage_missing` | **Phase 0.2** |
| Context hop > max | `execution` | `context_hop_exceeded` | **Phase 8** |
| Low-confidence stamp | `structure` or `context` | `low_confidence_context` | **Phase 7** |
| Cross-sheet literal without relationship | `plan` | `cross_sheet_context_denied` | **Phase 3** |
| Diff: rebind did not run | `execution` | `context_rebind_skipped` | **Phase 4** |

---

## 5. Debug playbook (operator)

After Phase 4–6 ship, use this sequence:

1. **Evals → Pipeline Evals** — Check critical gate + `context_isolation` per source.
2. **Debug → Context inspector** — Open affected `source_id`; read scoped fields + evidence.
3. **Debug → Context trail** — Filter `field=market`; see build → plan → rebind → stamp.
4. **Debug → State snapshot** — `job.multi_source.source_done` / `after_process_file` for packet at finalize.
5. **Debug → Source trace** — `execute_tools` rebind notes in log metadata.
6. **Review → Relationship** — Confirm `union_false_duplicate` vs true duplicate.

---

## 6. Suggested implementation order (summary)

```
Week 1:  Phase 0 (bleed fixes) + Phase 1.1 (planning_summary contract)
Week 2:  Phase 1 (ScopedField) + Phase 2.1–2.2 (provenance)
Week 3:  Phase 4 (diff log) + Phase 5.1–5.3 (ContextVerifier)
Week 4:  Phase 6 (debug UI) + Phase 3 (boundaries)
Week 5+: Phase 7–9 (confidence, hops, persistence)
```

**Highest ROI first:** 0.1, 0.2, 1.1, 4.1–4.2, 6.1–6.2, 5.1–5.2, 3.3, 3.4.

---

## 7. Files to create (new modules)

```
sia/context/
  __init__.py
  scoped_field.py      # ScopedField + serialization
  boundary_policy.py   # Allow / warn / deny rules
  propagation.py       # propagate_field, hop tracking
  merge.py             # confidence-aware field merge
  diff_log.py          # ContextDiffLogger
  verifier.py          # ContextVerifier (deterministic)
```

---

## 8. Non-goals (for now)

- LLM-based context inference across sheets (keep deterministic parse + HITL).
- Automatic cross-sheet dimension fill without analyst-approved relationships.
- Replacing `OutputVerifier` with context checks (keep separate concerns).

---

## 9. Success criteria

The context engineering work is **done enough for production multisheet** when:

1. CP-07 multisheet job passes all context evals without override.
2. Any `market` / `channel` on output traces to block, tab, or analyst decision in UI.
3. Cross-sheet bleed is blocked at plan or pre-execute, not only at union.
4. Context diff log explains every literal change across the pipeline.
5. Job restart reproduces the same `interpreted_context` for unchanged demarcation (fingerprinted artifacts).

---

*Last updated: 2026-06-25. Align with `docs/ARCHITECTURE.md` §5–6 as canonical context evolves.*
