/**
 * Schema Agent - Setup Workspace
 * Handles Layout Demarcation (Step 1) and Column Mapping (Step 2)
 */

let currentJobId = null;
let currentSheet = null;
let currentSourceId = null;
let demarcationProposal = null;
let mappingProposal = null;
/** Per-block mapping sections from `/api/mapping/propose` when `multi_block_mapping` is true. */
let mappingBlocks = null;
/** Set from /api/mapping/propose when multi-level headers were merged for column names. */
let mappingHeaderDerivation = null;
let targetColumnOptions = ['No match'];
let mappingSaveTimer = null;

/** Last fetched preview payload for re-render (e.g. header label toggle). */
let previewTableCache = null;
/** Cached grid slices for inline per-block previews on the demarcation step. */
let inlineDemarcationPreviewCache = new Map();
/** Polls ``/api/demarcation/scan-progress`` while batch propose-all is in flight. */
let demarcationProgressTimer = null;
/** Polls ``/api/mapping/propose-progress`` while ``/api/mapping/propose`` is in flight. */
let mappingProgressTimer = null;

function inlinePreviewCacheKey(block) {
    const c = block.coordinates || {};
    return `${currentJobId}|${currentSheet}|${currentSourceId}|${block.id}|${c.start_row},${c.end_row},${c.start_col},${c.end_col}`;
}

function blockAllowsHeaderPick(block) {
    const currentDecision = normalizeBlockDecision(block);
    if (currentDecision !== 'Keep') {
        return false;
    }
    const pythonRole = (block.review_basis || {}).python_role || block.category;
    return block.category === 'Main Data' || pythonRole === 'Main Data';
}

function isMainDataBlockCategory(block) {
    const c = (block.category || '').trim().toLowerCase();
    return c === 'main data';
}

function resolveBlockSheetName(block) {
    return block.sheet_name || demarcationProposal?.sheet_name || currentSheet || '';
}
/** source_id -> proposal from last batch demarcation (instant tab switch; merged from job + propose-all) */
let demarcationBatchCache = null;
/** Last `/api/jobs/.../source-inventory` rows (for per-sheet saved layout UI). */
let lastSourceInventory = [];
/** Per `source_id` from `ux_stepper_summary.ux_source_progress` (layout / mapping flags). */
let lastUxSourceProgress = {};
/** Last full job payload for header (multi-file summary). */
let lastSetupJobForHeader = null;
/** Registry order used to re-render sheet rail after inventory refresh (sort + scroll). */
let lastSetupSources = [];
/** Order of sources as rendered in sheet tabs (for “save & next sheet”). */
let setupSourceRegistryOrder = [];
/** 1 = demarcation workspace visible, 2 = mapping (no tab UI — use this instead of `.tab-btn`). */
let setupUiStep = 1;
/** Template targets that should count as "primary" in semantic column mapping. */
let primaryTargetColumns = new Set();

function escapeAttr(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function escapeJsString(value) {
    return String(value ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'");
}

/** Strip markdown asterisks from classifier labels for compact header chips. */
function plainSemanticLabel(label) {
    if (label == null || label === '') return '';
    return String(label)
        .replace(/\*+/g, '')
        .replace(/\s+/g, ' ')
        .trim();
}

function fileBasename(name) {
    const s = String(name ?? '');
    const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    return i >= 0 ? s.slice(i + 1) : s;
}

/** One-line audit for compact legend footer (plain text). */
function compactAuditOneLine(audit) {
    if (!audit || (audit.pandas_rows == null && audit.visual_grid_rows == null)) return '';
    const pr = audit.pandas_rows ?? '—';
    const pc = audit.pandas_cols ?? '—';
    const vr = audit.visual_grid_rows ?? '—';
    const vc = audit.visual_grid_cols ?? '—';
    return `Raw ${pr}×${pc} · Grid ${vr}×${vc}`;
}

/**
 * Single-line legend: counts + confidence bands + thresholds + sheet/grid sizes.
 * Replaces the large Detection summary card.
 */
function buildDemarcationLegendFooter(summary, cg, audit, keptMainCount) {
    const c = cg || {};
    const trust = c.trust_threshold != null ? Number(c.trust_threshold).toFixed(2) : '0.80';
    const rev = c.review_threshold != null ? Number(c.review_threshold).toFixed(2) : '0.40';
    const cts = c.counts || {};
    const hi = Number(cts.high) || 0;
    const amb = Number(cts.ambiguous) || 0;
    const low = Number(cts.low) || 0;
    const s = summary || {};
    const segs = [];
    if (s.block_count != null || s.main_candidates != null) {
        const kept = keptMainCount != null ? Number(keptMainCount) : null;
        const mainLabel =
            kept != null
                ? `${kept} main kept · ${s.main_candidates ?? 0} proposed main`
                : `${s.main_candidates ?? 0} main`;
        segs.push(
            `${s.block_count ?? 0} regions · ${mainLabel} · ${s.context_candidates ?? 0} context · ${s.noise_candidates ?? 0} noise`,
        );
    }
    segs.push(`Bands: high ${hi} · ambiguous ${amb} · low ${low}`);
    segs.push(`Gating: ≥${trust} Python-only · ${rev}–${trust} AI may run · <${rev} review`);
    const al = compactAuditOneLine(audit);
    if (al) segs.push(al);
    const line = segs.join('  ·  ');
    if (!line.trim()) return '';
    return `<div class="demarcation-legend-footer" role="note" title="${escapeAttr(line)}">${escapeHtml(line)}</div>`;
}

/** Grouped file_id → { file_id, file_name, sources } after last `renderSources`. */
let lastSetupFileGroups = new Map();
let lastSetupFileOrder = [];

const elements = {
    setupSourcePanels: document.getElementById('setupSourcePanels'),
    setupFileList: document.getElementById('setupFileList'),
    setupSheetList: document.getElementById('setupSheetList'),
    jobSummaryLine: document.getElementById('jobSummaryLine'),
    demarcationWorkspace: document.getElementById('demarcationWorkspace'),
    mappingGrid: document.getElementById('mappingGrid'),
    tabDemarcation: document.getElementById('tabDemarcation'),
    tabMapping: document.getElementById('tabMapping'),
    backToLayoutBtn: document.getElementById('backToLayoutBtn'),
    demarcationSection: document.getElementById('demarcationSection'),
    mappingSection: document.getElementById('mappingSection'),
    confirmLayoutMapSheetBtn: document.getElementById('confirmLayoutMapSheetBtn'),
    saveMappingNextSheetBtn: document.getElementById('saveMappingNextSheetBtn'),
    skipProcessBtn: document.getElementById('skipProcessBtn'),
    setupGlobalActionsHint: document.getElementById('setupGlobalActionsHint'),
    previewModal: document.getElementById('previewModal'),
    previewTable: document.getElementById('previewTable'),
    previewBody: document.getElementById('previewBody'),
    previewHeader: document.getElementById('previewHeader'),
    previewRange: document.getElementById('previewRange'),
    previewSpinner: document.getElementById('previewSpinner'),
    previewHeaderRowHint: document.getElementById('previewHeaderRowHint'),
    previewShowHeaderLabelsChk: document.getElementById('previewShowHeaderLabelsChk'),
    sourceInventoryTableWrap: document.getElementById('sourceInventoryTableWrap'),
};

const SAVE_MAPPING_NEXT_LABEL = 'Save mapping & next sheet';
const CONFIRM_LAYOUT_MAP_LABEL = 'Confirm & map →';
const SAVE_LAYOUT_SKIP_MAP_LABEL = 'Save layout & next sheet →';
const RUN_AGENT_LABEL = 'Run Agent';

/** Strip UI-only keys before mapping submit (server also ignores ephemeral reuse markers). */
function sanitizeMappingForApi(mapping) {
    if (!Array.isArray(mapping)) return [];
    return mapping.map((c) => {
        if (!c || typeof c !== 'object') return c;
        const o = { ...c };
        delete o._mappingReusedFromSourceId;
        delete o.mapping_reused_from_source_id;
        delete o.mapping_reused_from_sheet;
        return o;
    });
}

function refreshPostMappingGlobalActions(job) {
    const hint = elements.setupGlobalActionsHint;
    const j = job || lastSetupJobForHeader;
    const uxs = (j && j.ux_stepper_summary) || {};
    const ready = !!uxs.stage1_complete || computeSheetsGuidedSetupDone(j);
    if (hint) {
        if (ready) {
            hint.classList.add('hidden');
            hint.textContent = '';
        } else {
            hint.classList.remove('hidden');
            hint.textContent =
                'Finish layout and column mapping for every sheet for full coverage. Skip Set up & Process (file · sheet bar) is always available.';
        }
    }
}

/** Mapping toolbar primary: save & advance, or Run Agent when every sheet has layout + mapping (see computeSheetsGuidedSetupDone). */
function refreshMappingPrimaryActionButton(job) {
    const btn = elements.saveMappingNextSheetBtn;
    if (!btn || btn.dataset.finalizing === '1' || btn.querySelector('.spinner')) return;
    const j = job || lastSetupJobForHeader;
    const uxs = (j && j.ux_stepper_summary) || {};
    const runMode = setupUiStep === 2 && computeSheetsGuidedSetupDone(j);
    btn.textContent = runMode ? RUN_AGENT_LABEL : SAVE_MAPPING_NEXT_LABEL;
    btn.title = runMode
        ? 'Open Console and start the run automatically (current sheet’s mapping is saved first if needed).'
        : 'Save mapping for this sheet and go to the next sheet in tab order.';
    // Match Layout “Confirm & map” — same primary pill for both Save and Run Agent (no secondary outline).
    btn.classList.add('btn-primary');
    btn.classList.remove('btn-secondary');
    btn.disabled = false;
}

/** Match server ``layout_registry_row_is_main_data`` for in-memory demarcation blocks. */
function blockCountsAsKeptMainData(block) {
    if (!block || typeof block !== 'object') return false;
    const cat = String(block.category || '')
        .trim()
        .toLowerCase()
        .replace(/\s+/g, '')
        .replace(/_/g, '');
    if (cat !== 'maindata') return false;
    const dec = String(normalizeBlockDecision(block)).trim().toLowerCase();
    if (dec === 'discard' || dec === 'context') return false;
    return dec === 'keep' || dec === 'approved' || dec === '';
}

function countKeptMainDataBlocksFromProposal(blocks) {
    if (!Array.isArray(blocks)) return 0;
    return blocks.filter((b) => blockCountsAsKeptMainData(b)).length;
}

/**
 * Guided setup complete if the current sheet’s pending layout save is applied and mapping is skipped
 * when there are no Treat-as-Data main blocks on that sheet.
 */
function computeSheetsGuidedSetupDoneAssumingCurrentLayoutSaved(job = lastSetupJobForHeader) {
    const reg =
        (job && Array.isArray(job.source_registry) && job.source_registry.length)
            ? job.source_registry
            : lastSetupSources;
    const n = (reg || []).length;
    if (!n) return false;
    const uxs = (job && job.ux_stepper_summary) || {};
    const prog = uxs.ux_source_progress || (job && job.ux_source_progress) || lastUxSourceProgress || {};
    const curSid = String(currentSourceId || '');
    const curSheet = String(currentSheet || '');
    for (const s of reg) {
        const sid = String(s.source_id || '');
        const sn = String(s.sheet_name || '');
        const isCurrent = sid === curSid && sn === curSheet;
        const row = lastSourceInventory.find(
            (x) => String(x.source_id || '') === sid && String(x.sheet_name || '') === sn,
        );
        const p = prog[sid] || {};
        const layoutOk = Boolean(p.layout_complete || (row && row.has_saved_layout) || isCurrent);
        let mappingOk = Boolean(p.mapping_complete || (row && (row.mapping_column_count || 0) > 0));
        if (!mappingOk && row && row.has_saved_layout && Number(row.main_block_count || 0) === 0) {
            mappingOk = true;
        }
        if (!mappingOk && isCurrent && countKeptMainDataBlocksFromProposal(demarcationProposal?.blocks) === 0) {
            mappingOk = true;
        }
        if (!layoutOk || !mappingOk) return false;
    }
    return true;
}

/** Layout step primary: map vs skip-mapping vs Run Agent when this sheet has no main data blocks. */
function refreshLayoutPrimaryActionButton(job) {
    const btn = elements.confirmLayoutMapSheetBtn;
    if (!btn || setupUiStep !== 1) return;
    if (btn.dataset.finalizing === '1' || btn.querySelector('.spinner')) return;
    const j = job || lastSetupJobForHeader;
    const mainKept = countKeptMainDataBlocksFromProposal(demarcationProposal?.blocks);
    if (mainKept > 0) {
        btn.textContent = CONFIRM_LAYOUT_MAP_LABEL;
        btn.title = 'Save this sheet’s layout and open column mapping for the same sheet';
    } else {
        const runMode = computeSheetsGuidedSetupDoneAssumingCurrentLayoutSaved(j);
        btn.textContent = runMode ? RUN_AGENT_LABEL : SAVE_LAYOUT_SKIP_MAP_LABEL;
        btn.title = runMode
            ? 'Save layout and open Console to start the agent (no column mapping on this sheet).'
            : 'Save layout and go to the next sheet (semantic mapping skipped — no main data blocks).';
    }
    btn.classList.add('btn-primary');
    btn.classList.remove('btn-secondary');
    btn.disabled = false;
}

function refreshUxStepperFromJob(job) {
    if (job) {
        lastSetupJobForHeader = job;
        const uxs = (job && job.ux_stepper_summary) || {};
        lastUxSourceProgress = uxs.ux_source_progress || job.ux_source_progress || lastUxSourceProgress;
    }
    applySheetTabLayoutSavedState();
    refreshPostMappingGlobalActions(job);
    refreshMappingPrimaryActionButton(job);
    refreshLayoutPrimaryActionButton(job);
}

function updateCompactProgressLine() {
    const el = document.getElementById('setupProgressSummary');
    if (!el) return;
    const job = lastSetupJobForHeader;
    const reg = (job && Array.isArray(job.source_registry) && job.source_registry.length)
        ? job.source_registry
        : lastSetupSources;
    const n = (reg || []).length;
    if (!n) {
        el.textContent = '';
        el.classList.add('hidden');
        return;
    }
    const uxs = (job && job.ux_stepper_summary) || {};
    const prog = uxs.ux_source_progress || (job && job.ux_source_progress) || lastUxSourceProgress || {};
    let lc = 0;
    let mc = 0;
    for (const s of reg) {
        const sid = String(s.source_id || '');
        const sn = String(s.sheet_name || '');
        const row = lastSourceInventory.find(
            (x) => String(x.source_id || '') === sid && String(x.sheet_name || '') === sn,
        );
        const p = prog[sid] || {};
        if (p.layout_complete || (row && row.has_saved_layout)) lc += 1;
        let mappingCounted = Boolean(p.mapping_complete || (row && (row.mapping_column_count || 0) > 0));
        if (!mappingCounted && row && row.has_saved_layout && Number(row.main_block_count || 0) === 0) {
            mappingCounted = true;
        }
        if (mappingCounted) mc += 1;
    }
    el.textContent = `Sheets: ${lc}/${n} layout · ${mc}/${n} mapping`;
    el.classList.remove('hidden');
}

/**
 * Per-sheet layout + mapping ready, using the same signals as the progress line plus
 * “mapping N/A” when layout exists and there are no main blocks (matches server _derived_mapping_complete).
 * Does not require multi-file relationship decisions — that gate is enforced in the pipeline / Review.
 */
function computeSheetsGuidedSetupDone(job = lastSetupJobForHeader) {
    const reg = (job && Array.isArray(job.source_registry) && job.source_registry.length)
        ? job.source_registry
        : lastSetupSources;
    const n = (reg || []).length;
    if (!n) return false;
    const uxs = (job && job.ux_stepper_summary) || {};
    const prog = uxs.ux_source_progress || (job && job.ux_source_progress) || lastUxSourceProgress || {};
    for (const s of reg) {
        const sid = String(s.source_id || '');
        const sn = String(s.sheet_name || '');
        const row = lastSourceInventory.find(
            (x) => String(x.source_id || '') === sid && String(x.sheet_name || '') === sn,
        );
        const p = prog[sid] || {};
        const layoutOk = Boolean(p.layout_complete || (row && row.has_saved_layout));
        let mappingOk = Boolean(p.mapping_complete || (row && (row.mapping_column_count || 0) > 0));
        if (!mappingOk && row && row.has_saved_layout && Number(row.main_block_count || 0) === 0) {
            mappingOk = true;
        }
        if (!layoutOk || !mappingOk) return false;
    }
    return true;
}

function updateSetupContextHeader() {
    const job = lastSetupJobForHeader;
    const reg = (job && Array.isArray(job.source_registry) && job.source_registry.length)
        ? job.source_registry
        : (lastSetupSources || []);
    const src = (reg || []).find(
        (s) => (s.source_id || '') === (currentSourceId || '')
            && String(s.sheet_name || '') === String(currentSheet || ''),
    );
    const pick = src || (reg && reg.length ? reg[0] : null);

    const stickyF = document.getElementById('setupStickyFile');
    const stickyS = document.getElementById('setupStickySheet');
    if (stickyF && stickyS) {
        stickyF.textContent = pick
            ? fileBasename(pick.file_name || '')
            : (job?.filename ? fileBasename(String(job.filename).split(/\s*\+\s*\d+/)[0].trim()) || '—' : '—');
        stickyS.textContent = (currentSheet != null && currentSheet !== '')
            ? String(currentSheet)
            : (pick && pick.sheet_name != null ? String(pick.sheet_name) : '—');
    }

    if (elements.jobSummaryLine) {
        const dfs = (job && job.data_files) || [];
        if (job && dfs.length > 1 && job.filename) {
            elements.jobSummaryLine.textContent = `Job: ${job.filename}`;
            elements.jobSummaryLine.classList.remove('hidden');
        } else {
            elements.jobSummaryLine.textContent = '';
            elements.jobSummaryLine.classList.add('hidden');
        }
    }
    updateCompactProgressLine();
}

function sheetCompletionSortTier(source) {
    const sid = source.source_id;
    const sn = source.sheet_name || '';
    const row = lastSourceInventory.find(
        (s) => (s.source_id || '') === sid && (s.sheet_name || '') === sn,
    );
    const prog = lastUxSourceProgress[sid] || {};
    const layoutDone = !!(prog.layout_complete || (row && row.has_saved_layout));
    let mappingDone = !!(prog.mapping_complete || (row && (row.mapping_column_count || 0) > 0));
    if (!mappingDone && row && row.has_saved_layout && Number(row.main_block_count || 0) === 0) {
        mappingDone = true;
    }
    if (layoutDone && mappingDone) return 0;
    return 1;
}

function sortSourcesWithinGroup(sources) {
    const orderIdx = new Map(
        (sources || []).map((s, i) => [`${s.source_id}|${s.sheet_name || ''}`, i]),
    );
    return [...(sources || [])].sort((a, b) => {
        const ta = sheetCompletionSortTier(a);
        const tb = sheetCompletionSortTier(b);
        if (ta !== tb) return ta - tb;
        const aAct = a.source_id === currentSourceId && String(a.sheet_name || '') === String(currentSheet || '');
        const bAct = b.source_id === currentSourceId && String(b.sheet_name || '') === String(currentSheet || '');
        if (aAct !== bAct) return aAct ? -1 : 1;
        const ia = orderIdx.get(`${a.source_id}|${a.sheet_name || ''}`) ?? 0;
        const ib = orderIdx.get(`${b.source_id}|${b.sheet_name || ''}`) ?? 0;
        return ia - ib;
    });
}

function getSourceLayoutMappingDone(sourceId, sheetName) {
    const sid = String(sourceId || '');
    const sn = String(sheetName || '');
    const row = lastSourceInventory.find(
        (s) => (s.source_id || '') === sid && (s.sheet_name || '') === sn,
    );
    const prog = lastUxSourceProgress[sid] || {};
    const layoutDone = !!(prog.layout_complete || (row && row.has_saved_layout));
    const mappingDone = !!(prog.mapping_complete || (row && (row.mapping_column_count || 0) > 0));
    return { layoutDone, mappingDone };
}

/** @returns {'complete'|'progress'|'todo'} */
function tierForSheet(layoutDone, mappingDone) {
    if (layoutDone && mappingDone) return 'complete';
    if (layoutDone || mappingDone) return 'progress';
    return 'todo';
}

/** Aggregate tier for all sources in one workbook group. */
function tierForFileGroup(sources) {
    const list = sources || [];
    const n = list.length;
    if (!n) return 'todo';
    let fully = 0;
    let anyTouch = false;
    for (const s of list) {
        const { layoutDone, mappingDone } = getSourceLayoutMappingDone(s.source_id, s.sheet_name);
        if (layoutDone || mappingDone) anyTouch = true;
        if (layoutDone && mappingDone) fully += 1;
    }
    if (fully === n) return 'complete';
    if (!anyTouch) return 'todo';
    return 'progress';
}

function setupStatusPipHtml(tier, title) {
    const t = escapeAttr(title || '');
    if (tier === 'complete') {
        return `<span class="setup-source-pip setup-source-pip--done" title="${t}" aria-hidden="true">✓</span>`;
    }
    if (tier === 'progress') {
        return `<span class="setup-source-pip setup-source-pip--partial" title="${t}" aria-hidden="true"></span>`;
    }
    return `<span class="setup-source-pip setup-source-pip--empty" title="${t}" aria-hidden="true"></span>`;
}

function renderSetupSourcePanels() {
    const fileList = elements.setupFileList;
    const sheetList = elements.setupSheetList;
    if (!fileList || !sheetList) return;

    const fScroll = fileList.scrollTop;
    const sScroll = sheetList.scrollTop;

    const list = lastSetupSources || [];
    const byFile = new Map();
    const fileOrder = [];
    for (const source of list) {
        const fid = source.file_id || '_default';
        if (!byFile.has(fid)) {
            byFile.set(fid, { file_id: fid, file_name: source.file_name || '', sources: [] });
            fileOrder.push(fid);
        }
        const g = byFile.get(fid);
        if (!g.file_name && source.file_name) g.file_name = source.file_name;
        g.sources.push(source);
    }
    lastSetupFileOrder = fileOrder;
    lastSetupFileGroups = byFile;

    if (!fileOrder.length) {
        fileList.innerHTML = '';
        sheetList.innerHTML = '';
        return;
    }

    let currentFid = null;
    for (const src of list) {
        if (src.source_id === currentSourceId && String(src.sheet_name || '') === String(currentSheet || '')) {
            currentFid = src.file_id || '_default';
            break;
        }
    }
    if (!currentFid) currentFid = fileOrder[0];
    if (!fileOrder.includes(currentFid)) currentFid = fileOrder[0];

    const fileParts = [];
    for (const fid of fileOrder) {
        const g = byFile.get(fid);
        const displayFile = fileBasename(g.file_name) || 'Workbook';
        const fullTitle = g.file_name || displayFile;
        const tier = tierForFileGroup(g.sources);
        const n = g.sources.length;
        let doneSheets = 0;
        for (const s of g.sources) {
            const { layoutDone, mappingDone } = getSourceLayoutMappingDone(s.source_id, s.sheet_name);
            if (layoutDone && mappingDone) doneSheets += 1;
        }
        const meta = n ? `${doneSheets}/${n}` : '';
        const pipTitle = tier === 'complete'
            ? 'All sheets in this file have layout and mapping saved'
            : tier === 'progress'
                ? 'Some sheets in progress'
                : 'No sheet layout or mapping saved yet';
        const selected = fid === currentFid;
        fileParts.push(
            `<button type="button" class="setup-source-item" role="option" aria-selected="${selected ? 'true' : 'false'}"`
            + ` data-setup-pick-file="${escapeAttr(fid)}" title="${escapeAttr(fullTitle)}">`
            + setupStatusPipHtml(tier, pipTitle)
            + `<span class="setup-source-item-label">${escapeHtml(displayFile)}</span>`
            + (meta ? `<span class="setup-source-item-meta">${escapeHtml(meta)}</span>` : '')
            + '</button>',
        );
    }
    fileList.innerHTML = fileParts.join('');

    const group = byFile.get(currentFid);
    const sources = group ? sortSourcesWithinGroup(group.sources) : [];
    const titleFileBase = group ? fileBasename(group.file_name || '') : '';

    const matchCur = (s) => s.source_id === currentSourceId && String(s.sheet_name || '') === String(currentSheet || '');
    const encOk = sources.some(matchCur);

    if (!encOk && sources.length) {
        const s0 = sources[0];
        currentSourceId = s0.source_id;
        currentSheet = s0.sheet_name || null;
        setCurrentJob(currentJobId, currentSheet, currentSourceId);
        syncSetupUrlWithSelection();
    }

    const sheetParts = [];
    for (const source of sources) {
        const sheetPart = source.sheet_name ? String(source.sheet_name) : (source.file_name || source.source_id || 'Sheet');
        const { layoutDone, mappingDone } = getSourceLayoutMappingDone(source.source_id, source.sheet_name);
        const tier = tierForSheet(layoutDone, mappingDone);
        const lm = `${layoutDone ? 'L✓' : 'L·'} ${mappingDone ? 'M✓' : 'M·'}`;
        const pipTitle = `${sheetPart} — ${layoutDone ? 'layout saved' : 'layout pending'}; ${mappingDone ? 'mapping saved' : 'mapping pending'}`;
        const selected = matchCur(source);
        const sid = escapeAttr(source.source_id);
        const sn = escapeAttr(source.sheet_name || '');
        sheetParts.push(
            `<button type="button" class="setup-source-item" role="option" aria-selected="${selected ? 'true' : 'false'}"`
            + ` data-setup-pick-source-id="${sid}" data-setup-pick-sheet-name="${sn}"`
            + ` title="${escapeAttr(`${titleFileBase} / ${sheetPart}`)}">`
            + setupStatusPipHtml(tier, pipTitle)
            + `<span class="setup-source-item-label">${escapeHtml(sheetPart)}</span>`
            + `<span class="setup-source-item-meta">${escapeHtml(lm)}</span>`
            + '</button>',
        );
    }
    sheetList.innerHTML = sheetParts.join('') || '<p class="block-inline-note" style="margin:4px 6px;font-size:11px;">No sheets in this file.</p>';

    fileList.scrollTop = fScroll;
    sheetList.scrollTop = sScroll;
}

function onSetupSourcePanelClick(ev) {
    const fileBtn = ev.target.closest('[data-setup-pick-file]');
    if (fileBtn) {
        const fid = fileBtn.getAttribute('data-setup-pick-file');
        const group = fid ? lastSetupFileGroups.get(fid) : null;
        if (!group || !group.sources.length) return;
        const sorted = sortSourcesWithinGroup(group.sources);
        const sameName = sorted.find((s) => String(s.sheet_name || '') === String(currentSheet || ''));
        const pick = sameName || sorted[0];
        changeSource(pick.source_id, pick.sheet_name || '');
        return;
    }
    const sheetBtn = ev.target.closest('[data-setup-pick-source-id]');
    if (sheetBtn) {
        const sid = sheetBtn.getAttribute('data-setup-pick-source-id');
        const sn = sheetBtn.getAttribute('data-setup-pick-sheet-name') || '';
        if (sid) changeSource(sid, sn);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const urlParams = new URLSearchParams(window.location.search);
    currentJobId = urlParams.get('job_id') || getCurrentJob().jobId;
    currentSheet = urlParams.get('sheet_name') || getCurrentJob().sheetName;
    currentSourceId = urlParams.get('source_id') || getCurrentJob().sourceId;
    // Normalize "null"/"undefined" strings from bad links or storage
    if (currentSheet === 'null' || currentSheet === 'undefined') currentSheet = null;
    if (currentSourceId === 'null' || currentSourceId === 'undefined') currentSourceId = null;

    if (!currentJobId) {
        showToast('No active job found. Redirecting to upload...', 'warning');
        setTimeout(() => window.location.href = 'upload.html', 2000);
        return;
    }

    init();
});

/**
 * localStorage can hold sheet_name / source_id from a previous job. If they do not
 * exist in this job's registry, demarcation may run on the wrong workbook or sheet
 * (e.g. an empty tab), yielding zero blocks even when the real data sheet is fine.
 */
function alignSourceSelectionWithJob(job) {
    const registry = job.source_registry || [];
    if (!Array.isArray(registry) || registry.length === 0) return;

    const validIds = new Set(registry.map(s => s.source_id).filter(Boolean));
    const sheetNames = new Set(registry.map(s => s.sheet_name).filter(Boolean));

    let needsReset = false;
    if (currentSourceId && !validIds.has(currentSourceId)) {
        needsReset = true;
    }
    if (currentSheet && sheetNames.size > 0 && !sheetNames.has(currentSheet)) {
        needsReset = true;
    }

    if (needsReset) {
        const first = registry[0];
        currentSourceId = first.source_id || null;
        currentSheet = first.sheet_name || null;
        setCurrentJob(currentJobId, currentSheet, currentSourceId);
    }
}

/** Keep the address bar in sync so a refresh does not resurrect stale sheet/source query params. */
function syncSetupUrlWithSelection() {
    if (!currentJobId) return;
    const params = new URLSearchParams();
    params.set('job_id', currentJobId);
    if (currentSheet) params.set('sheet_name', currentSheet);
    if (currentSourceId) params.set('source_id', currentSourceId);
    const qs = params.toString();
    const newUrl = `${window.location.pathname}?${qs}`;
    window.history.replaceState({}, '', newUrl);
}

async function init() {
    try {
        const job = await getJobStatus(currentJobId);
        lastSetupJobForHeader = job;

        alignSourceSelectionWithJob(job);
        syncSetupUrlWithSelection();

        if (Array.isArray(job.source_registry) && job.source_registry.length > 0) {
            renderSources(job.source_registry);
        } else if (job.sheets && job.sheets.length > 0) {
            renderSources(job.sheets.map(sheetName => ({ sheet_name: sheetName, source_id: sheetName, file_name: job.filename })));
        } else {
            lastSetupSources = [];
            lastSetupFileOrder = [];
            lastSetupFileGroups = new Map();
            if (elements.setupFileList) elements.setupFileList.innerHTML = '';
            if (elements.setupSheetList) elements.setupSheetList.innerHTML = '';
            updateSetupContextHeader();
        }

        refreshUxStepperFromJob(job);

        mergeJobDemarcationProposalsIntoCache(job);

        // Start with demarcation

        try {
            const prevLabels = localStorage.getItem('setupPreviewShowHeaderLabels');
            if (elements.previewShowHeaderLabelsChk && prevLabels === 'true') {
                elements.previewShowHeaderLabelsChk.checked = true;
            }
        } catch (e) { /* ignore */ }

        await loadDemarcation();
        await loadSourceInventory();
        refreshUxStepperFromJob(job);

        if (elements.setupSourcePanels && !elements.setupSourcePanels.dataset.clickBound) {
            elements.setupSourcePanels.dataset.clickBound = '1';
            elements.setupSourcePanels.addEventListener('click', onSetupSourcePanelClick);
        }

        if (elements.backToLayoutBtn) {
            elements.backToLayoutBtn.addEventListener('click', () => switchStep(1));
        }

        if (elements.confirmLayoutMapSheetBtn) {
            elements.confirmLayoutMapSheetBtn.addEventListener('click', () => confirmLayoutAndOpenMappingForCurrentSheet());
        }
        if (elements.saveMappingNextSheetBtn) {
            elements.saveMappingNextSheetBtn.addEventListener('click', () => onSaveMappingOrRunAgentClick());
        }
        initCollapsiblePanels();
        elements.skipProcessBtn?.addEventListener('click', () => skipAndAutoProcess());

        document.getElementById('closePreviewBtn').addEventListener('click', closeModal);
        document.getElementById('reScanBtn').addEventListener('click', () => loadDemarcationBatch(true));
        if (elements.previewShowHeaderLabelsChk) {
            elements.previewShowHeaderLabelsChk.addEventListener('change', () => {
                try {
                    localStorage.setItem('setupPreviewShowHeaderLabels', elements.previewShowHeaderLabelsChk.checked ? 'true' : 'false');
                } catch (e) { /* ignore */ }
                if (previewTableCache) {
                    renderPreviewTable(
                        previewTableCache.data,
                        previewTableCache.columns,
                        previewTableCache.pStartRow,
                        previewTableCache.pStartCol,
                        previewTableCache.coords,
                        { showHeaderLabels: elements.previewShowHeaderLabelsChk.checked }
                    );
                }
            });
        }
        if (elements.demarcationWorkspace && !elements.demarcationWorkspace.dataset.inlinePreviewClickBound) {
            elements.demarcationWorkspace.dataset.inlinePreviewClickBound = '1';
            elements.demarcationWorkspace.addEventListener('click', onDemarcationInlinePreviewRowClick);
        }

    } catch (e) {
        showToast(`Initialization failed: ${e.message}`, 'error');
    }
}

function renderSources(sources) {
    const list = sources || [];
    lastSetupSources = list;
    setupSourceRegistryOrder = list.map((s) => ({
        source_id: s.source_id,
        sheet_name: s.sheet_name || '',
    }));
    renderSetupSourcePanels();
    updateSetupContextHeader();
}

function applySheetTabLayoutSavedState() {
    renderSetupSourcePanels();
    updateSetupContextHeader();
}

/**
 * Switch active source/sheet and load demarcation or mapping for that tab.
 * Uses `job.demarcation_batch_proposals` from the server when present so tab switches
 * do not re-run propose-all after the batch has already completed.
 */
window.changeSource = async function (sourceId, sheetName) {
    if (sourceId === currentSourceId && sheetName === currentSheet) {
        void getJobStatus(currentJobId)
            .then((j) => refreshUxStepperFromJob(j))
            .catch(() => {});
        return;
    }
    if (mappingSaveTimer) {
        clearTimeout(mappingSaveTimer);
        mappingSaveTimer = null;
    }
    if (setupUiStep === 2 && currentJobId && mappingProposal && mappingProposal.length > 0) {
        try {
            await persistMappingDraft();
        } catch {
            /* best-effort flush before switching sheet */
        }
    }
    const sidKey = String(sourceId || '');
    currentSourceId = sourceId;
    currentSheet = sheetName || null;
    setCurrentJob(currentJobId, currentSheet, currentSourceId);
    syncSetupUrlWithSelection();

    renderSetupSourcePanels();
    updateSetupContextHeader();
    if (setupUiStep === 1) {
        if (demarcationBatchCache && sidKey && demarcationBatchCache[sidKey]) {
            demarcationProposal = demarcationBatchCache[sidKey];
            renderDemarcationBlocks(Array.isArray(demarcationProposal.blocks) ? demarcationProposal.blocks : []);
            applyDemarcationStatusFromProposal();
            loadSourceInventory();
        } else {
            await hydrateDemarcationCacheFromJob();
            if (demarcationBatchCache && sidKey && demarcationBatchCache[sidKey]) {
                demarcationProposal = demarcationBatchCache[sidKey];
                renderDemarcationBlocks(Array.isArray(demarcationProposal.blocks) ? demarcationProposal.blocks : []);
                applyDemarcationStatusFromProposal();
                loadSourceInventory();
            } else {
                await loadDemarcationBatch(false);
            }
        }
    } else {
        await loadMapping(false);
    }
    void getJobStatus(currentJobId)
        .then((j) => refreshUxStepperFromJob(j))
        .catch(() => {});
};

// ===== Step 1: Demarcation =====

function applyDemarcationStatusFromProposal() {
    const statusEl = document.getElementById('demarcationStatus');
    if (!statusEl || !demarcationProposal) return;
    if (demarcationProposal.detection_mode === 'python_only' || demarcationProposal.llm_status === 'python_only') {
        statusEl.textContent = 'Python only (AI off)';
        statusEl.className = 'status-badge warning';
    } else if (demarcationProposal.llm_status === 'fallback_heuristics') {
        statusEl.textContent = 'Python Ready (AI Fallback)';
        statusEl.className = 'status-badge warning';
    } else if (demarcationProposal.llm_status === 'confidence_gated_no_llm') {
        statusEl.textContent = 'Confidence gating (no uncertain blocks → no LLM)';
        statusEl.className = 'status-badge success';
    } else if (demarcationProposal.llm_status === 'confidence_gated_classified') {
        statusEl.textContent = 'Python + AI (uncertain band only)';
        statusEl.className = 'status-badge success';
    } else {
        statusEl.textContent = 'Python + AI Ready';
        statusEl.className = 'status-badge success';
    }
}

/** Copy server-persisted batch proposals into the client cache (same shape as propose-all response). */
function mergeJobDemarcationProposalsIntoCache(job) {
    if (!job) return 0;
    const pb = job.demarcation_batch_proposals;
    if (!pb || typeof pb !== 'object') return 0;
    if (!demarcationBatchCache) demarcationBatchCache = {};
    let n = 0;
    for (const [k, v] of Object.entries(pb)) {
        if (v && typeof v === 'object') {
            demarcationBatchCache[String(k)] = v;
            n += 1;
        }
    }
    return n;
}

/** Refresh demarcation cache from GET /api/status (job holds proposals after propose-all or submit). */
async function hydrateDemarcationCacheFromJob() {
    if (!currentJobId) return 0;
    try {
        const job = await getJobStatus(currentJobId);
        lastSetupJobForHeader = job;
        return mergeJobDemarcationProposalsIntoCache(job);
    } catch {
        return 0;
    }
}

/**
 * One blocking request: Python demarcation for every source, then one batched LLM pass for ambiguous blocks.
 * Results are stored on the job (`demarcation_batch_proposals`) and mirrored in `demarcationBatchCache` so
 * switching sheets does not re-run the scan. (Overlapping “scan sheet 2 while you review sheet 1” would need
 * an async / incremental propose-all API.)
 */
async function loadDemarcationBatch(force = false) {
    if (!currentJobId) return;
    if (!force) {
        await hydrateDemarcationCacheFromJob();
        const sidKey = String(currentSourceId || '');
        if (demarcationBatchCache && sidKey && demarcationBatchCache[sidKey]) {
            demarcationProposal = demarcationBatchCache[sidKey];
            renderDemarcationBlocks(Array.isArray(demarcationProposal.blocks) ? demarcationProposal.blocks : []);
            applyDemarcationStatusFromProposal();
            loadSourceInventory();
            return;
        }
    }

    elements.demarcationWorkspace.innerHTML = `
        <div class="skeleton-container">
            <p class="block-inline-note" style="grid-column: 1/-1; margin-bottom: 8px;">Scanning sources: full-sheet Python demarcation (large sheets are slow here). When blocks fall in the uncertain confidence band, a batched LLM pass can refine roles.</p>
            <p id="demarcationScanLive" class="demarcation-scan-live" role="status" aria-live="polite">Starting scan…</p>
            <div class="skeleton-card"><div class="skeleton-text skeleton-title"></div><div class="skeleton-text skeleton-line"></div></div>
            <div class="skeleton-card"><div class="skeleton-text skeleton-title"></div><div class="skeleton-text skeleton-line"></div></div>
        </div>`;

    const stopProgressPoll = () => {
        if (demarcationProgressTimer) {
            clearInterval(demarcationProgressTimer);
            demarcationProgressTimer = null;
        }
    };
    const pollScanProgress = async () => {
        const live = document.getElementById('demarcationScanLive');
        if (!live || !currentJobId) return;
        try {
            const r = await fetch(`/api/demarcation/scan-progress/${currentJobId}?_=${Date.now()}`, { cache: 'no-store' });
            const p = await r.json();
            if (p && !p.error && p.message) live.textContent = p.message;
        } catch {
            /* ignore transient poll errors */
        }
    };
    pollScanProgress();
    demarcationProgressTimer = setInterval(pollScanProgress, 650);

    try {
        if (force) demarcationBatchCache = null;
        const params = new URLSearchParams();
        if (currentSheet) params.set('sheet_name', currentSheet);
        if (currentSourceId) params.set('source_id', currentSourceId);
        params.set('_ts', String(Date.now()));
        const url = `/api/demarcation/propose-all/${currentJobId}?${params.toString()}`;
        const res = await fetch(url, { cache: 'no-store' });
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        demarcationBatchCache = {};
        inlineDemarcationPreviewCache.clear();
        const pb = data.proposals_by_source_id || {};
        for (const [k, v] of Object.entries(pb)) {
            demarcationBatchCache[String(k)] = v;
        }
        const curSid = String(currentSourceId || '');
        if (curSid && demarcationBatchCache[curSid]) {
            demarcationProposal = demarcationBatchCache[curSid];
        } else if (data.proposal && Object.keys(data.proposal).length) {
            demarcationProposal = data.proposal;
            if (demarcationProposal.source_id) {
                demarcationBatchCache[String(demarcationProposal.source_id)] = demarcationProposal;
            }
        } else {
            demarcationProposal = {};
        }
        const blocks = Array.isArray(demarcationProposal.blocks) ? demarcationProposal.blocks : [];
        renderDemarcationBlocks(blocks);
        applyDemarcationStatusFromProposal();
        loadSourceInventory();
    } catch (e) {
        elements.demarcationWorkspace.innerHTML = `
            <div class="empty-state">
                <p class="error-text">❌ Batch analysis failed: ${e.message}</p>
                <button class="btn-secondary btn-sm" onclick="loadDemarcationBatch(true)">Try Again</button>
            </div>`;
    } finally {
        stopProgressPoll();
    }
}

async function loadDemarcation(force = false) {
    await loadDemarcationBatch(force);
}

function normalizeBlockDecision(block) {
    return block.decision || block.ai_suggestion || ((block.review_basis || {}).python_suggestion) || 'Discard';
}

function formatRowLabel(rowIdx) {
    return `Row ${Number(rowIdx) + 1}`;
}

function renderDemarcationBlocks(blocks) {
    if (!blocks || blocks.length === 0) {
        const dbg = demarcationProposal && demarcationProposal.demarcation_debug;
        const selectionHtml = `
            <div class="demarcation-debug-panel">
                <p class="section-label" style="margin-top: 12px;">Current selection</p>
                <ul class="signal-list">
                    <li>Job: ${escapeHtml(String(currentJobId || ''))}</li>
                    <li>Source ID: ${escapeHtml(String(currentSourceId || '(none)'))}</li>
                    <li>Sheet name: ${escapeHtml(String(currentSheet || '(none)'))}</li>
                </ul>
                <p class="block-inline-note">Use the <strong>Source</strong> tabs above to pick the file/sheet that contains the table. If you use multiple uploads, the wrong default source often looks empty here.</p>
            </div>
        `;
        const dbgHtml = dbg ? `
            <div class="demarcation-debug-panel">
                <p class="section-label" style="margin-top: 12px;">Server diagnostics (zero blocks)</p>
                <ul class="signal-list">
                    <li>File: ${escapeHtml(dbg.data_file_name || dbg.resolved_path || '')}</li>
                    <li>On disk: ${dbg.file_exists === false ? 'missing' : 'found'}${dbg.file_size_bytes != null ? ` (${escapeHtml(String(dbg.file_size_bytes))} bytes)` : ''}</li>
                    <li>Sheet used: ${escapeHtml(String(dbg.target_sheet || ''))}</li>
                    <li>Grid shape (visual scan): ${escapeHtml(String(dbg.grid_shape_rows))} × ${escapeHtml(String(dbg.grid_shape_cols))}</li>
                    ${dbg.pandas_shape_rows != null ? `<li>Pandas sheet shape (raw): ${escapeHtml(String(dbg.pandas_shape_rows))} × ${escapeHtml(String(dbg.pandas_shape_cols))}</li>` : ''}
                    <li>Non-empty cells (value-based): ${escapeHtml(String(dbg.non_empty_cells))}</li>
                    <li>Sheets in workbook: ${escapeHtml((dbg.workbook_sheets || []).join(', '))}</li>
                    ${dbg.proposal_error ? `<li>Proposal error: ${escapeHtml(String(dbg.proposal_error))}</li>` : ''}
                    ${dbg.llm_error ? `<li>LLM: ${escapeHtml(String(dbg.llm_error))}</li>` : ''}
                </ul>
                <p class="block-inline-note">If non-empty cells is 0, the workbook may use formulas without cached values (re-save in Excel) or you may be on an empty tab.</p>
            </div>
        ` : `
            <p class="block-inline-note" style="margin-top: 12px;">Tip: Hard-refresh this page (Ctrl+F5) and restart the web server so you load the latest setup script and API. Older builds only showed this line with no diagnostics.</p>
        `;
        elements.demarcationWorkspace.innerHTML = `
            <p class="empty-state">No distinctive regions found.</p>
            ${selectionHtml}
            ${dbgHtml}`;
        refreshLayoutPrimaryActionButton();
        return;
    }

    const summary = demarcationProposal?.python_summary;
    const cg = demarcationProposal?.confidence_gating || {};
    const audit = demarcationProposal?.sheet_shape_audit;
    const keptMain = countKeptMainDataBlocksFromProposal(blocks);
    const legendFooter = buildDemarcationLegendFooter(summary, cg, audit || {}, keptMain);

    const cardsHtml = blocks.map((block, index) => {
        const signals = getBlockSignals(block);
        const currentDecision = normalizeBlockDecision(block);
        const pythonSummary = (block.review_basis || {}).python_summary || block.summary || '';
        const pythonDetection = block.python_detection || {};
        const headerRowZero = Number.isInteger(block.coordinates?.header_row)
            ? block.coordinates.header_row
            : Number.isInteger(pythonDetection.header_row_candidate)
                ? pythonDetection.header_row_candidate
                : block.coordinates?.start_row;
        const headerMode = (block.coordinates?.header_mode || 'single').toLowerCase() === 'multi' ? 'multi' : 'single';
        const isMultiHeader = headerMode === 'multi';
        const sr0 = block.coordinates?.start_row ?? 0;
        const er0 = block.coordinates?.end_row ?? 0;
        let headerEndZero = headerRowZero;
        if (isMultiHeader) {
            headerEndZero = Number.isInteger(block.coordinates?.header_row_end)
                ? block.coordinates.header_row_end
                : Math.min((Number.isInteger(headerRowZero) ? headerRowZero : sr0) + 1, er0);
        }
        const showHeaderInput = blockAllowsHeaderPick(block);
        const headerExcelMin = sr0 + 1;
        const headerExcelMax = er0 + 1;
        const headerExcelValue = Number.isInteger(headerRowZero) ? headerRowZero + 1 : headerExcelMin;
        const headerEndExcelValue = Number.isInteger(headerEndZero) ? headerEndZero + 1 : headerExcelMin + 1;
        const bidJs = escapeJsString(block.id);

        const reviewBadge = block.human_review_required
            ? '<span class="human-review-badge" title="Heuristic confidence below 0.4 — please confirm role manually">Human review</span>'
            : '';
        const sheetLabel = resolveBlockSheetName(block);
        const sheetHtml = sheetLabel
            ? ` <span class="block-sheet-label" title="Worksheet for this block">· Sheet: ${escapeHtml(sheetLabel)}</span>`
            : '';
        return `
        <div class="demarcation-block-card decision-node ${block.category.toLowerCase().replace(' ', '-')} stagger-${(index % 3) + 1}" data-id="${block.id}">
            <div class="card-toolbar">
                <div class="toolbar-info">
                    <span class="block-type-label">Block Type: ${escapeHtml(block.category || 'Unknown')}</span>
                    ${reviewBadge}
                    <div class="block-subtext">${escapeHtml(block.name || 'Candidate block')} (${escapeHtml(block.excel_range || 'Unknown range')})${sheetHtml}</div>
                </div>

                <div class="toolbar-actions">
                    <button class="btn-secondary btn-sm preview-btn-inline" onclick="showPreview('${block.id}')">
                        <span style="margin-right: 4px;">👁️</span> Preview
                    </button>
                    <div class="decision-toggle-inline" data-block-id="${block.id}">
                        <button class="toggle-btn-inline treat-as-data ${currentDecision === 'Keep' ? 'active' : ''}" 
                                onclick="updateBlockDecision('${block.id}', 'Keep')">Treat as Data</button>
                        <button class="toggle-btn-inline use-as-meta ${currentDecision === 'Context' || currentDecision === 'Use as Context' ? 'active' : ''}" 
                                onclick="updateBlockDecision('${block.id}', 'Context')">Use as Metadata</button>
                        <button class="toggle-btn-inline ignore ${currentDecision === 'Discard' ? 'active' : ''}" 
                                onclick="updateBlockDecision('${block.id}', 'Discard')">Ignore</button>
                    </div>
                </div>
            </div>

            <div class="card-body-grid">
                <div class="signals-section">
                    <span class="section-label">Python Detection</span>
                    <ul class="signal-list">
                        ${signals.map(s => `<li>${escapeHtml(s)}</li>`).join('')}
                    </ul>
                    ${pythonSummary ? `<p class="suggestion-reasoning demarcation-heuristic-summary">${escapeHtml(pythonSummary)}</p>` : ''}
                </div>

                <div class="inline-preview-column">
                    <div class="block-inline-preview-root">
                        <div class="inline-preview-toolbar">
                            <span class="inline-preview-title">Preview</span>
                            ${showHeaderInput ? `
                                ${isMultiHeader ? `
                                <label class="header-row-input-group inline-preview-header-field">
                                    <span>Header start (Excel)</span>
                                    <input
                                        class="header-row-excel-input"
                                        type="number"
                                        min="${headerExcelMin}"
                                        max="${headerExcelMax}"
                                        value="${headerExcelValue}"
                                        onchange="updateBlockHeaderRow('${bidJs}', this.value)"
                                    />
                                </label>
                                <label class="header-row-input-group inline-preview-header-field">
                                    <span>Header end (Excel)</span>
                                    <input
                                        class="header-row-end-excel-input"
                                        type="number"
                                        min="${headerExcelMin}"
                                        max="${headerExcelMax}"
                                        value="${headerEndExcelValue}"
                                        onchange="updateBlockHeaderEndRow('${bidJs}', this.value)"
                                    />
                                </label>
                                ` : `
                                <label class="header-row-input-group inline-preview-header-field">
                                    <span>Header (Excel row)</span>
                                    <input
                                        class="header-row-excel-input"
                                        type="number"
                                        min="${headerExcelMin}"
                                        max="${headerExcelMax}"
                                        value="${headerExcelValue}"
                                        onchange="updateBlockHeaderRow('${bidJs}', this.value)"
                                    />
                                </label>
                                `}
                                <div class="header-mode-toggle-inline" role="group" aria-label="Header rows mode">
                                    <button type="button" class="header-mode-chip ${isMultiHeader ? '' : 'active'}" onclick="updateBlockHeaderMode('${bidJs}', 'single')">Single header</button>
                                    <button type="button" class="header-mode-chip ${isMultiHeader ? 'active' : ''}" onclick="updateBlockHeaderMode('${bidJs}', 'multi')">Multi header</button>
                                </div>
                                <p class="inline-preview-hint-inline">${isMultiHeader
        ? 'Header band is merged into one set of column names (parent_sub). Click a row to set the start row.'
        : 'Click any row in the grid to set the header.'}</p>
                            ` : '<p class="inline-preview-hint-inline inline-preview-hint-muted">Header row is only used for blocks set to <strong>Treat as Data</strong>.</p>'}
                        </div>
                        <div class="inline-preview-scroll${showHeaderInput ? ' header-row-pick-mode' : ''}" data-block-id="${escapeAttr(block.id)}">
                            <div class="inline-preview-table-host"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>`;
    }).join('');

    elements.demarcationWorkspace.innerHTML = legendFooter + cardsHtml;
    void hydrateInlineDemarcationPreviews(blocks);
    refreshLayoutPrimaryActionButton();
}

function coordsToExcelRange(coords) {
    if (!coords) return '';
    const sr = Number(coords.start_row ?? 0);
    const er = Number(coords.end_row ?? 0);
    const sc = Number(coords.start_col ?? 0);
    const ec = Number(coords.end_col ?? 0);
    return `${indexToExcelColumn(sc)}${sr + 1}:${indexToExcelColumn(ec)}${er + 1}`;
}

function getBlockSignals(block) {
    const detection = block.python_detection || {};
    const coords = block.coordinates || {};
    const rangeLabel = block.excel_range || coordsToExcelRange(coords);

    const band = block.confidence_band;
    const hc = block.heuristic_confidence;
    const bandLine = band != null && hc != null
        ? `Heuristic confidence: ${hc} (band: ${band}${band === 'high' ? ', AI skipped' : ''}${band === 'ambiguous' ? ', AI eligible' : ''}${band === 'low' ? ', flagged for review' : ''})`
        : null;

    const lines = [
        `Range: ${rangeLabel}`,
        `Shape: ${detection.row_count ?? 0} rows x ${detection.col_count ?? 0} cols, fill ${(detection.fill_ratio ?? 0).toFixed(2)}`,
        `Cells: ${detection.non_empty_cells ?? 0} non-empty, ${detection.numeric_cells ?? 0} numeric, ${detection.text_cells ?? 0} text`,
    ];

    if (isMainDataBlockCategory(block)) {
        const hcand = detection.header_row_candidate;
        const row0 = Number.isInteger(hcand)
            ? hcand
            : Number.isInteger(coords.header_row)
                ? coords.header_row
                : coords.start_row ?? 0;
        lines.push(`Python header candidate: Excel row ${Number(row0) + 1}`);
    } else {
        lines.push('Header row: not used for this block type (only primary tables get a header).');
    }

    lines.push(
        'Preview: grid includes a small pad around the range; purple highlights are the block — extra columns/rows are neighboring sheet cells.',
    );

    if (bandLine) {
        lines.push(bandLine);
    }
    return lines;
}

function getDebugDetails(block) {
    const coords = block.coordinates || { start_row: 0, end_row: 0, start_col: 0, end_col: 0 };
    return `
        <ul>
            <li><strong>Coordinates:</strong> R${coords.start_row}:R${coords.end_row} | C${coords.start_col}:C${coords.end_col}</li>
            <li><strong>Confidence Score:</strong> 0.94 (Layout Match)</li>
            <li><strong>Validation Agents:</strong> DateRangeValidator, CurrencyConsistencyAgent</li>
        </ul>
    `;
}

function indexToExcelColumn(n) {
    let result = '';
    let num = n;
    while (num >= 0) {
        result = String.fromCharCode((num % 26) + 65) + result;
        num = Math.floor(num / 26) - 1;
    }
    return result;
}

/** Table body mount inside `.inline-preview-scroll` (toolbar stays outside innerHTML churn). */
function getInlinePreviewTableHost(scrollEl) {
    return scrollEl?.querySelector?.('.inline-preview-table-host') || scrollEl;
}

async function hydrateInlineDemarcationPreviews(blocks) {
    if (!currentJobId || !Array.isArray(blocks) || blocks.length === 0) return;
    await Promise.all(blocks.map((block) => {
        const scrollEl = document.querySelector(`.inline-preview-scroll[data-block-id="${escapeAttr(block.id)}"]`);
        if (!scrollEl) return Promise.resolve();
        return loadAndRenderInlinePreview(block, scrollEl);
    }));
}

async function loadAndRenderInlinePreview(block, scrollEl) {
    const coords = block.coordinates;
    const tableHost = getInlinePreviewTableHost(scrollEl);
    if (!coords) {
        tableHost.innerHTML = '<p class="block-inline-note">No coordinates</p>';
        return;
    }
    const key = inlinePreviewCacheKey(block);
    try {
        let payload = inlineDemarcationPreviewCache.get(key);
        if (!payload) {
            tableHost.innerHTML = '<p class="block-inline-note">Loading preview…</p>';
            const pad = 2;
            const pStartRow = Math.max(0, coords.start_row - pad);
            const pEndRow = coords.end_row + pad;
            const pStartCol = Math.max(0, coords.start_col - pad);
            const pEndCol = coords.end_col + pad;
            let url = `/api/demarcation/preview/${currentJobId}?start_row=${pStartRow}&end_row=${pEndRow}&start_col=${pStartCol}&end_col=${pEndCol}`;
            if (currentSheet) url += `&sheet_name=${encodeURIComponent(currentSheet)}`;
            if (currentSourceId) url += `&source_id=${encodeURIComponent(currentSourceId)}`;
            const res = await fetch(url);
            const data = await res.json();
            if (data.error) throw new Error(data.error);
            let previewNote = '';
            if (data.preview_truncated) {
                previewNote = `Showing first ${data.preview_row_count_returned} of ${data.range_row_count} rows (UI cap ${data.preview_row_cap}). Block range ${data.excel_range || ''} is still the full selection for layout.`;
            }
            payload = { data: data.preview, columns: data.columns, pStartRow, pStartCol, previewNote };
            inlineDemarcationPreviewCache.set(key, payload);
        }
        if (blockAllowsHeaderPick(block)) {
            scrollEl.classList.add('header-row-pick-mode');
        } else {
            scrollEl.classList.remove('header-row-pick-mode');
        }
        renderInlinePreviewTable(tableHost, block, payload);
    } catch (e) {
        tableHost.innerHTML = `<p class="block-inline-note error-text">${escapeHtml(e.message)}</p>`;
    }
}

function renderInlinePreviewTable(tableHost, block, payload) {
    const { data, columns, pStartRow, pStartCol, previewNote } = payload;
    const coords = block.coordinates;
    const { headerInner, bodyInner } = buildPreviewTableHtml(
        data,
        columns,
        pStartRow,
        pStartCol,
        coords,
        { showHeaderLabels: false },
    );
    const bid = escapeAttr(String(block.id));
    const noteHtml = previewNote
        ? `<p class="block-inline-note" style="margin:0 0 6px;">${escapeHtml(previewNote)}</p>`
        : '';
    tableHost.innerHTML = `${noteHtml}<table class="inline-preview-table" data-block-id="${bid}"><thead><tr>${headerInner}</tr></thead><tbody>${bodyInner}</tbody></table>`;
}

function rerenderInlinePreviewForBlock(blockId) {
    const block = demarcationProposal?.blocks?.find(b => String(b.id) === String(blockId));
    if (!block) return;
    const scrollEl = document.querySelector(`.inline-preview-scroll[data-block-id="${escapeAttr(blockId)}"]`);
    if (!scrollEl) return;
    const tableHost = getInlinePreviewTableHost(scrollEl);
    const key = inlinePreviewCacheKey(block);
    const payload = inlineDemarcationPreviewCache.get(key);
    if (payload) {
        renderInlinePreviewTable(tableHost, block, payload);
    }
}

function onDemarcationInlinePreviewRowClick(e) {
    const tr = e.target.closest('tr[data-abs-row]');
    if (!tr || !elements.demarcationWorkspace.contains(tr)) return;
    const table = tr.closest('table.inline-preview-table');
    const blockId = table?.dataset?.blockId;
    if (!blockId) return;
    const block = demarcationProposal?.blocks?.find(b => String(b.id) === String(blockId));
    if (!block || !blockAllowsHeaderPick(block)) return;
    const abs = Number.parseInt(tr.dataset.absRow, 10);
    if (!Number.isInteger(abs)) return;
    const c = block.coordinates;
    if (!c || abs < c.start_row || abs > c.end_row) return;
    updateBlockHeaderRow(blockId, abs + 1);
    const card = document.querySelector(`.demarcation-block-card[data-id="${escapeAttr(blockId)}"]`);
    const inp = card?.querySelector('.header-row-excel-input');
    if (inp) inp.value = String(abs + 1);
}

window.updateBlockDecision = function (blockId, decision) {
    const block = demarcationProposal?.blocks?.find(b => b.id === blockId);
    if (block) {
        block.decision = decision;
        console.log(`[DECISION NODE] Block ${blockId} set to ${decision}`);
    }
    if (demarcationProposal?.blocks) {
        renderDemarcationBlocks(demarcationProposal.blocks);
    }
};

/** `excelRow` is 1-based (Excel row number). Stored as 0-based index on the block. */
window.updateBlockHeaderRow = function (blockId, excelRow1Based) {
    const block = demarcationProposal?.blocks?.find(b => String(b.id) === String(blockId));
    if (!block) return;
    if (!blockAllowsHeaderPick(block)) {
        showToast('Header row applies only to blocks set to Treat as Data.', 'warning');
        return;
    }
    const parsed = Number.parseInt(String(excelRow1Based).trim(), 10);
    if (!Number.isInteger(parsed)) return;
    const z = parsed - 1;
    const sr = block.coordinates?.start_row ?? 0;
    const er = block.coordinates?.end_row ?? 0;
    if (z < sr || z > er) {
        showToast(`Header must be an Excel row between ${sr + 1} and ${er + 1}.`, 'warning');
        const card = document.querySelector(`.demarcation-block-card[data-id="${escapeAttr(blockId)}"]`);
        const inp = card?.querySelector('.header-row-excel-input');
        const cur = block.coordinates?.header_row;
        if (inp && Number.isInteger(cur)) inp.value = String(cur + 1);
        return;
    }
    block.coordinates = block.coordinates || {};
    block.coordinates.header_row = z;
    const mode = (block.coordinates.header_mode || 'single').toLowerCase();
    if (mode === 'multi' && Number.isInteger(block.coordinates.header_row_end) && block.coordinates.header_row_end < z) {
        block.coordinates.header_row_end = z;
        const inpEnd = document.querySelector(`.demarcation-block-card[data-id="${escapeAttr(blockId)}"] .header-row-end-excel-input`);
        if (inpEnd) inpEnd.value = String(z + 1);
    }
    rerenderInlinePreviewForBlock(blockId);
};

/** End row for multi-header mode; 1-based Excel row. */
window.updateBlockHeaderEndRow = function (blockId, excelRow1Based) {
    const block = demarcationProposal?.blocks?.find(b => String(b.id) === String(blockId));
    if (!block) return;
    if (!blockAllowsHeaderPick(block)) return;
    if ((block.coordinates?.header_mode || 'single').toLowerCase() !== 'multi') return;
    const parsed = Number.parseInt(String(excelRow1Based).trim(), 10);
    if (!Number.isInteger(parsed)) return;
    const z = parsed - 1;
    const sr = block.coordinates?.start_row ?? 0;
    const er = block.coordinates?.end_row ?? 0;
    const hdr = block.coordinates?.header_row ?? sr;
    if (z < sr || z > er || z < hdr) {
        showToast(`Header end must be between ${Math.max(sr, hdr) + 1} and ${er + 1} (not before the start row).`, 'warning');
        const card = document.querySelector(`.demarcation-block-card[data-id="${escapeAttr(blockId)}"]`);
        const inpEnd = card?.querySelector('.header-row-end-excel-input');
        const cur = block.coordinates?.header_row_end;
        if (inpEnd && Number.isInteger(cur)) inpEnd.value = String(cur + 1);
        return;
    }
    block.coordinates = block.coordinates || {};
    block.coordinates.header_row_end = z;
    rerenderInlinePreviewForBlock(blockId);
};

window.updateBlockHeaderMode = function (blockId, mode) {
    const block = demarcationProposal?.blocks?.find(b => String(b.id) === String(blockId));
    if (!block || !blockAllowsHeaderPick(block)) return;
    block.coordinates = block.coordinates || {};
    const m = String(mode).toLowerCase() === 'multi' ? 'multi' : 'single';
    if (m === 'single') {
        block.coordinates.header_mode = 'single';
        delete block.coordinates.header_row_end;
    } else {
        block.coordinates.header_mode = 'multi';
        const hr = Number.isInteger(block.coordinates.header_row) ? block.coordinates.header_row : (block.coordinates.start_row ?? 0);
        const er = block.coordinates.end_row ?? hr;
        if (!Number.isInteger(block.coordinates.header_row_end) || block.coordinates.header_row_end < hr) {
            block.coordinates.header_row_end = Math.min(hr + 1, er);
        }
    }
    if (demarcationProposal?.blocks) {
        renderDemarcationBlocks(demarcationProposal.blocks);
    }
};

async function submitLayoutForSource(blocks, sourceId, sheetName) {
    if (!currentJobId || !Array.isArray(blocks) || blocks.length === 0) {
        throw new Error('Nothing to save (no blocks).');
    }
    if (!sourceId) {
        throw new Error('Missing source_id for layout save.');
    }
    const response = await fetch(`/api/demarcation/submit/${currentJobId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            blocks,
            source_id: sourceId,
            sheet_name: sheetName || null,
        }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) {
        throw new Error(payload.error || `Demarcation save failed (${response.status})`);
    }
    return payload;
}

async function persistDemarcation() {
    if (!demarcationProposal || !Array.isArray(demarcationProposal.blocks) || demarcationProposal.blocks.length === 0) {
        return null;
    }
    const sheetName = demarcationProposal.sheet_name || currentSheet || null;
    const payload = await submitLayoutForSource(demarcationProposal.blocks, currentSourceId, sheetName);
    const sid = String(currentSourceId || '');
    if (sid && demarcationProposal) {
        if (!demarcationBatchCache) demarcationBatchCache = {};
        demarcationBatchCache[sid] = demarcationProposal;
    }
    return payload;
}

/** Active row from last `/api/jobs/.../source-inventory` fetch. */
function inventoryRowForCurrentSource(sources = lastSourceInventory) {
    const list = sources || [];
    return list.find(
        (r) => String(r.source_id) === String(currentSourceId || '')
            && String(r.sheet_name || '') === String(currentSheet || ''),
    );
}

/** Saved layout exists but no Main Data blocks kept as data → semantic mapping N/A. */
function shouldSkipSemanticMappingForInventoryRow(inv) {
    if (!inv || !inv.has_saved_layout) return false;
    return Number(inv.main_block_count || 0) === 0;
}

/** After skipping mapping: go to layout step and open the next source (same pattern as save & next). */
async function advanceToNextSourceAfterMappingSkip(toastMessage) {
    if (toastMessage) showToast(toastMessage, 'info');
    const { next } = getNextSourceInTabOrder();
    if (next && next.source_id) {
        switchStep(1);
        await changeSource(next.source_id, next.sheet_name || '');
        return true;
    }
    switchStep(1);
    showToast('No more sheets in this job.', 'info');
    return false;
}

function readSourceOrderFromDom() {
    if (setupSourceRegistryOrder.length) {
        return setupSourceRegistryOrder.map((s) => ({
            source_id: s.source_id,
            sheet_name: s.sheet_name || '',
        }));
    }
    return [];
}

function getNextSourceInTabOrder() {
    const order = setupSourceRegistryOrder.length > 0 ? setupSourceRegistryOrder : readSourceOrderFromDom();
    if (!order.length) return { next: null, order };
    const idx = order.findIndex(
        (s) => s.source_id === currentSourceId && (s.sheet_name || '') === (currentSheet || ''),
    );
    if (idx < 0) return { next: null, order };
    return { next: order[idx + 1] || null, order };
}

function formatSourceTabLabel(entry) {
    if (!entry) return '';
    return entry.sheet_name ? String(entry.sheet_name) : String(entry.source_id || '');
}

async function confirmLayoutAndOpenMappingForCurrentSheet() {
    try {
        if (!currentJobId) {
            showToast('No active job.', 'warning');
            return;
        }
        if (!demarcationProposal || !Array.isArray(demarcationProposal.blocks) || demarcationProposal.blocks.length === 0) {
            showToast('No blocks to save for this sheet.', 'warning');
            return;
        }
        const submitPayload = await persistDemarcation();
        await loadSourceInventory();
        const job = await getJobStatus(currentJobId);
        lastSetupJobForHeader = job;
        refreshUxStepperFromJob(job);
        const uxs = job.ux_stepper_summary || {};
        const prog = uxs.ux_source_progress || job.ux_source_progress || {};
        const cur = prog[currentSourceId] || {};
        if (!cur.layout_complete) {
            showToast('Layout was not confirmed on the server. Check errors or try Re-Scan.', 'warning');
            return;
        }
        const mainKeptBefore = countKeptMainDataBlocksFromProposal(demarcationProposal?.blocks);
        if (mainKeptBefore === 0 || (submitPayload && submitPayload.skip_semantic_mapping)) {
            const jobAfter = await getJobStatus(currentJobId);
            lastSetupJobForHeader = jobAfter;
            refreshUxStepperFromJob(jobAfter);
            if (computeSheetsGuidedSetupDone(jobAfter)) {
                await goToHarmonizationPrep(elements.confirmLayoutMapSheetBtn);
                return;
            }
            await advanceToNextSourceAfterMappingSkip(
                'No main data blocks on this sheet — semantic mapping skipped.',
            );
            const jobAfterNav = await getJobStatus(currentJobId);
            lastSetupJobForHeader = jobAfterNav;
            refreshUxStepperFromJob(jobAfterNav);
            return;
        }
        switchStep(2);
        showToast('Layout saved — column mapping for this sheet.', 'success');
    } catch (e) {
        showToast(`Save failed: ${e.message}`, 'error');
    }
}

async function onSaveMappingOrRunAgentClick() {
    if (!currentJobId) {
        showToast('No active job.', 'warning');
        return;
    }
    const jobPeek = await getJobStatus(currentJobId);
    lastSetupJobForHeader = jobPeek;
    const uxsPeek = jobPeek.ux_stepper_summary || {};
    if (setupUiStep === 2 && computeSheetsGuidedSetupDone(jobPeek)) {
        await goToHarmonizationPrep(elements.saveMappingNextSheetBtn);
        return;
    }
    await saveMappingAndNextSheet();
}

async function saveMappingAndNextSheet() {
    try {
        if (!currentJobId) {
            showToast('No active job.', 'warning');
            return;
        }
        if (!mappingProposal || !Array.isArray(mappingProposal) || mappingProposal.length === 0) {
            showToast('No mapping rows for this sheet yet.', 'warning');
            return;
        }
        await persistMappingDraft();
        await loadSourceInventory();
        const job = await getJobStatus(currentJobId);
        lastSetupJobForHeader = job;
        refreshUxStepperFromJob(job);
        showToast('Mapping saved for this sheet.', 'success');
        const { next } = getNextSourceInTabOrder();
        if (next && next.source_id) {
            switchStep(1);
            await changeSource(next.source_id, next.sheet_name || '');
        } else {
            const uxs = job.ux_stepper_summary || {};
            const allSheetsReady = computeSheetsGuidedSetupDone(job);
            showToast(
                allSheetsReady
                    ? 'That was the last sheet. Click Run Agent to open Console and start the run.'
                    : 'That was the last sheet. Finish remaining sheets, then Run Agent when the button appears.',
                'info',
            );
        }
    } catch (e) {
        showToast(`Save failed: ${e.message}`, 'error');
    }
}

function initCollapsiblePanels() {
    document.querySelectorAll('.collapsible-panel-toggle').forEach((btn) => {
        if (btn.dataset.collapsibleWired === '1') return;
        btn.dataset.collapsibleWired = '1';
        const bodyId = btn.getAttribute('aria-controls');
        const body = bodyId ? document.getElementById(bodyId) : null;
        const panel = btn.closest('.collapsible-panel');
        if (!body || !panel) return;
        btn.addEventListener('click', () => {
            const expanded = btn.getAttribute('aria-expanded') !== 'false';
            const willExpand = !expanded;
            btn.setAttribute('aria-expanded', willExpand ? 'true' : 'false');
            body.hidden = !willExpand;
            panel.classList.toggle('is-collapsed', !willExpand);
        });
    });
}

/** Open mapping for the current sheet after layout is already saved (e.g. back from layout). */
async function goToMappingStep() {
    try {
        await loadSourceInventory();
        const job = await getJobStatus(currentJobId);
        lastSetupJobForHeader = job;
        const uxs = job.ux_stepper_summary || {};
        const prog = uxs.ux_source_progress || job.ux_source_progress || {};
        const cur = prog[currentSourceId] || {};
        if (!cur.layout_complete) {
            showToast('Save layout for this sheet first (Confirm layout & map this sheet).', 'warning');
            return;
        }
        const inv = inventoryRowForCurrentSource();
        if (shouldSkipSemanticMappingForInventoryRow(inv)) {
            await advanceToNextSourceAfterMappingSkip(
                'This sheet has no main data blocks — semantic mapping does not apply.',
            );
            const jobAfter = await getJobStatus(currentJobId);
            lastSetupJobForHeader = jobAfter;
            refreshUxStepperFromJob(jobAfter);
            return;
        }
        switchStep(2);
        refreshUxStepperFromJob(job);
    } catch (e) {
        showToast(`Open mapping failed: ${e.message}`, 'error');
    }
}

function getCategoryIcon(cat) {
    const map = {
        'Main Data': '📊',
        'Metadata': 'ℹ️',
        'Footnote': '🗒️',
        'Comment': '💬',
        'Noise': '🗑️'
    };
    return map[cat] || '📦';
}

window.showPreview = async function (blockId) {
    const block = demarcationProposal.blocks.find(b => b.id === blockId);
    if (!block) return;

    elements.previewRange.textContent = block.excel_range || '';
    elements.previewModal.classList.remove('hidden');
    elements.previewSpinner.classList.remove('hidden');
    elements.previewTable.classList.add('hidden');

    try {
        const coords = block.coordinates;
        // Padded range for context (+/- 3 cells)
        const pad = 3;
        const pStartRow = Math.max(0, coords.start_row - pad);
        const pEndRow = coords.end_row + pad;
        const pStartCol = Math.max(0, coords.start_col - pad);
        const pEndCol = coords.end_col + pad;

        let url = `/api/demarcation/preview/${currentJobId}?start_row=${pStartRow}&end_row=${pEndRow}&start_col=${pStartCol}&end_col=${pEndCol}`;
        if (currentSheet) url += `&sheet_name=${encodeURIComponent(currentSheet)}`;
        if (currentSourceId) url += `&source_id=${encodeURIComponent(currentSourceId)}`;

        const res = await fetch(url);
        const data = await res.json();
        if (data.error) throw new Error(data.error);

        let rangeLabel = block.excel_range || '';
        if (data.preview_truncated && data.range_row_count != null) {
            rangeLabel += ` — preview: first ${data.preview_row_count_returned} of ${data.range_row_count} rows (cap ${data.preview_row_cap}); layout still uses full range`;
            elements.previewRange.textContent = rangeLabel;
        } else {
            elements.previewRange.textContent = rangeLabel;
        }

        previewTableCache = {
            data: data.preview,
            columns: data.columns,
            pStartRow,
            pStartCol,
            coords,
        };
        const hdr = Number.isInteger(coords?.header_row) ? coords.header_row : null;
        if (elements.previewHeaderRowHint) {
            const truncHint = data.preview_truncated
                ? ` Large range: table shows first ${data.preview_row_count_returned} rows only; coordinates above are the full block.`
                : '';
            if (hdr !== null) {
                elements.previewHeaderRowHint.textContent =
                    `Detected header: ${formatRowLabel(hdr)}. Column row shows letters only unless you enable labels below.${truncHint}`;
                elements.previewHeaderRowHint.classList.remove('hidden');
            } else {
                elements.previewHeaderRowHint.textContent = truncHint.trim();
                elements.previewHeaderRowHint.classList.toggle('hidden', !truncHint);
            }
        }
        const showLabels = !!(elements.previewShowHeaderLabelsChk && elements.previewShowHeaderLabelsChk.checked);
        renderPreviewTable(data.preview, data.columns, pStartRow, pStartCol, coords, { showHeaderLabels: showLabels });
        elements.previewSpinner.classList.add('hidden');
        elements.previewTable.classList.remove('hidden');

    } catch (e) {
        console.error(e);
        showToast(`Preview failed: ${e.message}`, 'error');
        closeModal();
    }
};

function buildPreviewTableHtml(data, columns, startRowIdx, startColIdx, targetBox, opts = {}) {
    const showHeaderLabels = !!opts.showHeaderLabels;
    const box = targetBox || {};
    const detectedHeaderRow = Number.isInteger(box.header_row) ? box.header_row : null;
    const multiBand = String(box.header_mode || '').toLowerCase() === 'multi' && Number.isInteger(box.header_row_end);
    const bandEndRow = multiBand ? box.header_row_end : detectedHeaderRow;
    const headerRowOffset = detectedHeaderRow !== null ? detectedHeaderRow - startRowIdx : null;
    const headerRowData = headerRowOffset !== null && headerRowOffset >= 0 && headerRowOffset < data.length
        ? data[headerRowOffset]
        : null;

    const colLetters = columns.map((_, i) => indexToExcelColumn(startColIdx + i));
    const headerInner = `
        <th class="excel-header row-num-col"></th>
        ${colLetters.map((letter, idx) => {
            const colKey = columns[idx];
            const headerValue = headerRowData ? headerRowData[colKey] : '';
            const safeHeaderValue = escapeHtml(String(headerValue ?? '').trim());
            const showHeaderValue = showHeaderLabels && safeHeaderValue && safeHeaderValue !== letter;
            return `
                <th class="excel-header ${showHeaderValue ? 'detected-header-cell' : ''}">
                    <div class="preview-col-letter">${letter}</div>
                    ${showHeaderValue ? `<div class="preview-header-value" title="${safeHeaderValue}">${safeHeaderValue}</div>` : ''}
                </th>
            `;
        }).join('')}
    `;

    const bodyInner = data.map((row, rIdx) => {
        const absoluteRow = startRowIdx + rIdx;
        const inHeaderBand = detectedHeaderRow !== null && bandEndRow !== null && bandEndRow !== undefined
            && absoluteRow >= detectedHeaderRow && absoluteRow <= bandEndRow;
        const rowNumCell = `<td class="excel-header row-num ${inHeaderBand ? 'preview-detected-header-row' : ''}">${absoluteRow + 1}</td>`;

        const rowContent = columns.map((colKey, cIdx) => {
            const absoluteCol = startColIdx + cIdx;
            const val = row[colKey] !== null && row[colKey] !== undefined ? row[colKey] : '';

            const isTarget = absoluteRow >= (box.start_row ?? 0) &&
                absoluteRow <= (box.end_row ?? 0) &&
                absoluteCol >= (box.start_col ?? 0) &&
                absoluteCol <= (box.end_col ?? 0);
            const cellClasses = [
                isTarget ? 'preview-target-cell' : '',
                inHeaderBand ? 'preview-detected-header-row' : '',
            ].filter(Boolean).join(' ');

            return `<td class="${cellClasses}">${escapeHtml(String(val))}</td>`;
        }).join('');

        return `<tr data-abs-row="${absoluteRow}">${rowNumCell}${rowContent}</tr>`;
    }).join('');

    return { headerInner, bodyInner };
}

function renderPreviewTable(data, columns, startRowIdx, startColIdx, targetBox, opts = {}) {
    const { headerInner, bodyInner } = buildPreviewTableHtml(data, columns, startRowIdx, startColIdx, targetBox, opts);
    elements.previewHeader.innerHTML = headerInner;
    elements.previewBody.innerHTML = bodyInner;
}

function closeModal() {
    elements.previewModal.classList.add('hidden');
}

async function loadSourceInventory() {
    if (!currentJobId) return;
    try {
        const params = new URLSearchParams();
        if (currentSourceId) params.set('active_source_id', currentSourceId);
        if (currentSheet) params.set('active_sheet_name', currentSheet);
        const qs = params.toString();
        const url = `/api/jobs/${currentJobId}/source-inventory${qs ? `?${qs}` : ''}`;
        const res = await fetch(url);
        const data = await res.json();
        if (data.error) throw new Error(data.error);
        lastSourceInventory = Array.isArray(data.sources) ? data.sources : [];
        if (elements.sourceInventoryTableWrap) {
            renderSourceInventoryTable(lastSourceInventory);
        }
        if (lastSetupSources.length) {
            renderSources(lastSetupSources);
        } else {
            applySheetTabLayoutSavedState();
            updateSetupContextHeader();
        }
        updateCompactProgressLine();
        refreshUxStepperFromJob(lastSetupJobForHeader);
    } catch (e) {
        if (elements.sourceInventoryTableWrap) {
            elements.sourceInventoryTableWrap.innerHTML = `<p class="error-text">${escapeHtml(e.message)}</p>`;
        } else {
            showToast(`Source inventory failed: ${e.message}`, 'error');
        }
    }
}

function renderSourceInventoryTable(sources) {
    if (!elements.sourceInventoryTableWrap) return;
    if (!sources.length) {
        elements.sourceInventoryTableWrap.innerHTML = '<p class="block-inline-note">No sources registered for this job.</p>';
        return;
    }
    const rows = sources.map((s) => {
        const active = s.is_active ? 'active-source' : '';
        const base = fileBasename(s.file_name || '');
        const sheet = s.sheet_name || '—';
        const layoutMark = s.has_saved_layout ? '✓' : '—';
        const mapN = Number(s.mapping_column_count ?? 0);
        const mapMark = mapN > 0 ? `✓ (${mapN})` : '—';
        const sid = escapeJsString(s.source_id);
        const sn = escapeJsString(s.sheet_name || '');
        const title = escapeAttr(`${s.file_name || ''} / ${sheet}`);
        return `<tr class="${active}">
            <td class="source-inventory-cell-source" title="${title}"><span class="source-inventory-file">${escapeHtml(base)}</span><span class="source-inventory-sep">/</span><span class="source-inventory-sheet">${escapeHtml(sheet)}</span></td>
            <td class="source-inventory-cell-flag">${layoutMark}</td>
            <td class="source-inventory-cell-flag">${mapMark}</td>
            <td class="source-inventory-cell-mini">${escapeHtml(String(s.block_count ?? 0))}</td>
            <td class="source-inventory-cell-mini">${escapeHtml(String(s.main_block_count ?? 0))}</td>
            <td><button type="button" class="btn-secondary btn-sm" onclick="jumpToSource('${sid}','${sn}')">Open</button></td>
        </tr>`;
    }).join('');
    elements.sourceInventoryTableWrap.innerHTML = `
        <table class="source-inventory-table source-inventory-table--compact">
            <thead>
                <tr>
                    <th>Source</th>
                    <th title="Saved layout">L</th>
                    <th title="Mapping columns">M</th>
                    <th title="Layout blocks">Blk</th>
                    <th title="Main blocks">Main</th>
                    <th></th>
                </tr>
            </thead>
            <tbody>${rows}</tbody>
        </table>`;
}

window.jumpToSource = function (sourceId, sheetName) {
    changeSource(sourceId, sheetName);
};

function refreshMappingCardsScope() {
    if (!mappingProposal || !Array.isArray(mappingProposal)) return;
    renderMappingCards();
}

// ===== Step 2: Mapping =====

window.updateMappingDecision = function (columnName, decision, blockId) {
    const col = findMappingColumn(columnName, blockId);
    if (col) {
        col.decision = decision;
        console.log(`[MAPPING] Column ${columnName} set to ${decision}`);
        scheduleMappingDraftSave();
        refreshMappingCardsScope();
    }
};

window.updateTargetColumn = function (columnName, targetColumn, blockId) {
    const col = findMappingColumn(columnName, blockId);
    if (!col) return;

    col.target_column = targetColumn;
    col.target_match_method = 'manual';
    col.target_match_confidence = targetColumn === 'No match' ? 0.0 : 1.0;
    applyDecisionRule(col);
    scheduleMappingDraftSave();
    refreshMappingCardsScope();
};

function inferDecisionFromTarget(col) {
    const target = col.target_column || 'No match';
    if (target === 'No match') {
        return 'Discard';
    }

    const columnType = String(col.column_type || '').toLowerCase();
    const classification = String(col.classification || '').toLowerCase();

    if (
        columnType === 'metric' ||
        columnType === 'date' ||
        classification.includes('delivery') ||
        classification.includes('cost') ||
        classification.includes('engagement')
    ) {
        return 'Keep';
    }

    return 'Metadata';
}

/**
 * AI-suggested UI role for a source column: 'primary', 'supporting', or 'exclude'.
 * Kept in sync with SchemaMapper.infer_column_role in schema_mapper.py.
 */
function inferRoleFromSemantics(col) {
    if (!col) return 'supporting';
    const cls = String(col.classification || '').toLowerCase();
    const dec = String(col.decision || '').toLowerCase();
    const ctype = String(col.column_type || '').toLowerCase();
    const isMetricLike =
        ctype === 'metric' ||
        cls.includes('delivery') ||
        cls.includes('cost') ||
        cls.includes('engagement') ||
        cls.includes('metric');
    if (dec === 'discard' || ctype === 'blank' || cls.includes('blank') || cls.includes('derived')) {
        return 'exclude';
    }
    const target = String(col.target_column || '').trim();
    if (target && target !== 'No match' && primaryTargetColumns.has(target)) {
        return 'primary';
    }
    if (isMetricLike) {
        return 'exclude';
    }
    return 'supporting';
}

/** Role → decision/target coupling so downstream submit logic still sees Keep/Metadata/Discard. */
function applyRoleRule(col) {
    if (!col) return;
    const role = col.role || 'supporting';
    if (role === 'exclude') {
        col.decision = 'Discard';
        col.target_column = 'No match';
        col.target_match_method = col.target_match_method || 'manual';
        col.target_match_confidence = 0.0;
        return;
    }
    if (role === 'primary') {
        col.decision = 'Keep';
        return;
    }
    col.decision = 'Metadata';
}

function applyDecisionRule(col) {
    if (!col) return;
    if (col.role) {
        applyRoleRule(col);
        return;
    }
    col.decision = inferDecisionFromTarget(col);
}

function inferDateSemanticFromColumn(col) {
    if (!col || typeof col !== 'object') return '';
    const name = String(col.column_name || '').trim().toLowerCase();
    const target = String(col.target_column || '').trim().toLowerCase();
    const ctype = String(col.column_type || '').trim().toLowerCase();
    if (!(ctype.includes('date') || isDateLikeTargetName(target) || isLikelyDateColumnName(name))) {
        return '';
    }
    if (/(^|[\s_])(start|from|begin)([\s_]|$)/.test(name)) return 'range_start';
    if (/(^|[\s_])(end|to|until|finish)([\s_]|$)/.test(name)) return 'range_end';
    if (/(^|[\s_])year([\s_]|$)/.test(name)) return 'part_year';
    if (/(^|[\s_])month([\s_]|$)/.test(name)) return 'part_month';
    if (/(^|[\s_])day([\s_]|$)/.test(name)) return 'part_day';
    if (/(^|[\s_])q[1-4]([\s_]|$)|(^|[\s_])quarter([\s_]|$)/.test(name)) return 'part_quarter';
    if (/(range|period|to|until|through)/.test(name)) return 'period_text';
    return 'point_in_time';
}

function resolveMappingColumnName(col) {
    if (!col || typeof col !== 'object') return '';
    return String(col.column_name || col.source_column || col.name || '').trim();
}

function normalizeMappingProposal(mapping) {
    if (!Array.isArray(mapping)) return;
    mapping.forEach(col => {
        if (!col || typeof col !== 'object') return;
        const name = resolveMappingColumnName(col);
        if (name) {
            col.column_name = name;
            if (!col.source_column) col.source_column = name;
        }
        if (!col.role) col.role = inferRoleFromSemantics(col);
        if (!isDateMappingCardCandidate(col)) {
            col.date_semantic = '';
        } else if (!col.date_semantic) {
            col.date_semantic = inferDateSemanticFromColumn(col);
        }
        applyRoleRule(col);
    });
}

function syncFlatMappingProposal() {
    if (mappingBlocks && Array.isArray(mappingBlocks) && mappingBlocks.length > 0) {
        mappingProposal = mappingBlocks.flatMap((section) =>
            Array.isArray(section.mapping) ? section.mapping : [],
        );
    }
}

function getAllMappingRows() {
    syncFlatMappingProposal();
    return Array.isArray(mappingProposal) ? mappingProposal : [];
}

function findMappingColumn(columnName, blockId) {
    const want = String(columnName || '').trim();
    const matches = (c) => c && resolveMappingColumnName(c) === want;
    const bid = String(blockId || '').trim();
    if (mappingBlocks && mappingBlocks.length && bid) {
        const section = mappingBlocks.find((s) => s && String(s.block_id || '') === bid);
        if (section && Array.isArray(section.mapping)) {
            return section.mapping.find(matches);
        }
    }
    return (mappingProposal || []).find(matches);
}

function applyMappingFromProposeResponse(data) {
    mappingBlocks = null;
    if (
        data
        && data.multi_block_mapping
        && Array.isArray(data.mapping_blocks)
        && data.mapping_blocks.length >= 2
    ) {
        mappingBlocks = data.mapping_blocks.map((section) => ({
            block_id: section.block_id,
            block_label: section.block_label,
            excel_range: section.excel_range,
            mapping: Array.isArray(section.mapping) ? section.mapping.slice() : [],
        }));
        mappingBlocks.forEach((section) => normalizeMappingProposal(section.mapping));
        syncFlatMappingProposal();
        return;
    }
    mappingProposal = Array.isArray(data.mapping) ? data.mapping : [];
    normalizeMappingProposal(mappingProposal);
}

function getMappingSectionsForRender() {
    if (mappingBlocks && Array.isArray(mappingBlocks) && mappingBlocks.length >= 2) {
        return mappingBlocks.map((section) => ({
            block_id: section.block_id || '',
            block_label: section.block_label || section.block_id || 'Main data block',
            excel_range: section.excel_range || '',
            mapping: Array.isArray(section.mapping) ? section.mapping : [],
        }));
    }
    return [
        {
            block_id: '',
            block_label: '',
            excel_range: '',
            mapping: mappingProposal || [],
        },
    ];
}

function isMultiBlockMappingUi() {
    return Boolean(mappingBlocks && mappingBlocks.length >= 2);
}

function scheduleMappingDraftSave() {
    if (getAllMappingRows().length === 0) return;
    const scheduledJob = currentJobId;
    const scheduledSheet = currentSheet;
    const scheduledSource = currentSourceId;
    if (mappingSaveTimer) {
        clearTimeout(mappingSaveTimer);
    }
    mappingSaveTimer = setTimeout(() => {
        mappingSaveTimer = null;
        if (
            scheduledJob !== currentJobId
            || String(scheduledSheet ?? '') !== String(currentSheet ?? '')
            || String(scheduledSource ?? '') !== String(currentSourceId ?? '')
        ) {
            return;
        }
        void persistMappingDraft();
    }, 250);
}

async function persistMappingDraft() {
    const rows = getAllMappingRows();
    if (!currentJobId || rows.length === 0) return;
    try {
        const r = await fetch(`/api/mapping/submit/${currentJobId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                mapping: sanitizeMappingForApi(rows),
                sheet_name: currentSheet,
                source_id: currentSourceId
            })
        });
        if (r.ok) {
            const j = await getJobStatus(currentJobId);
            refreshUxStepperFromJob(j);
        }
    } catch (e) {
        console.warn('Failed to save mapping draft', e);
    }
}

async function loadMapping(force = false) {
    const live = document.getElementById('mappingProposeLive');
    const statusEl = document.getElementById('mappingStatus');

    if (currentJobId) {
        await loadSourceInventory();
        const inv = inventoryRowForCurrentSource();
        if (shouldSkipSemanticMappingForInventoryRow(inv)) {
            if (statusEl) {
                statusEl.textContent = 'No main data — mapping skipped';
                statusEl.className = 'status-badge success';
            }
            if (live) live.textContent = '';
            elements.mappingGrid.innerHTML = `
                <p class="block-inline-note" style="max-width: 52rem;">
                    This sheet has no <strong>Main Data</strong> block kept as <strong>Treat as Data</strong>,
                    so semantic column mapping does not apply.
                </p>`;
            showToast('Semantic mapping skipped (no main data blocks).', 'info');
            await advanceToNextSourceAfterMappingSkip(null);
            const jobAfter = await getJobStatus(currentJobId);
            lastSetupJobForHeader = jobAfter;
            refreshUxStepperFromJob(jobAfter);
            return;
        }
    }

    try {
        await requireValidLlmConfig({ redirectOnFail: 'immediate' });
    } catch (e) {
        if (statusEl) {
            statusEl.textContent = 'Mapping Failed';
            statusEl.className = 'status-badge danger';
        }
        elements.mappingGrid.innerHTML = `<p class="error-text">${escapeHtml(e.message || 'LLM not configured')}</p>
            <p class="block-inline-note"><a href="/pages/settings.html">Open Settings</a> to configure your API key.</p>`;
        return;
    }

    const stopMappingProgressPoll = () => {
        if (mappingProgressTimer) {
            clearInterval(mappingProgressTimer);
            mappingProgressTimer = null;
        }
    };
    const pollMappingProposeProgress = async () => {
        if (!live || !currentJobId) return;
        try {
            const qs = new URLSearchParams();
            if (currentSheet) qs.set('sheet_name', currentSheet);
            if (currentSourceId) qs.set('source_id', currentSourceId);
            const r = await fetch(`/api/mapping/propose-progress/${currentJobId}?${qs}&_=${Date.now()}`, { cache: 'no-store' });
            const p = await r.json();
            if (!p || p.error) return;
            const wantSheet = String(currentSheet || '');
            const wantSrc = String(currentSourceId || '');
            if (String(p.sheet_name ?? '') !== wantSheet) return;
            if (wantSrc && String(p.source_id ?? '') !== wantSrc) return;
            if (p.message) live.textContent = p.message;
        } catch {
            /* ignore transient poll errors */
        }
    };

    elements.mappingGrid.innerHTML = `
        <div class="skeleton-container" style="display: flex; flex-direction: column; gap: 16px;">
            <div class="skeleton-card"></div>
            <div class="skeleton-card"></div>
            <div class="skeleton-card"></div>
            <div class="skeleton-card"></div>
            <div class="skeleton-card"></div>
            <div class="skeleton-card"></div>
        </div>`;

    if (statusEl) {
        statusEl.textContent = 'Loading mapping…';
        statusEl.className = 'status-badge warning';
    }
    if (live) {
        live.textContent = 'Starting semantic mapping request…';
    }
    pollMappingProposeProgress();
    mappingProgressTimer = setInterval(pollMappingProposeProgress, 650);

    try {
        mappingBlocks = null;
        let url = `/api/mapping/propose/${currentJobId}`;
        const params = new URLSearchParams();
        if (currentSheet) params.set('sheet_name', currentSheet);
        if (currentSourceId) params.set('source_id', currentSourceId);
        if (force) params.set('refresh', '1');
        if (params.toString()) url += `?${params.toString()}`;

        const res = await fetch(url);
        const data = await parseJsonResponse(res);
        if (!res.ok || data.error) {
            if (data.redirect_settings) {
                const returnUrl = encodeURIComponent(window.location.pathname + window.location.search);
                window.location.href = `/pages/settings.html?return=${returnUrl}&reason=llm`;
                return;
            }
            throw new Error(data.error || `Mapping request failed (${res.status})`);
        }

        if (!Array.isArray(data.mapping)) {
            throw new Error('Invalid mapping response format from server.');
        }

        targetColumnOptions = Array.isArray(data.target_columns) ? ['No match', ...data.target_columns] : ['No match'];
        primaryTargetColumns = new Set(Array.isArray(data.primary_target_columns) ? data.primary_target_columns : []);
        mappingHeaderDerivation = data.header_derivation || null;
        applyMappingFromProposeResponse(data);
        const reuse = data.mapping_reuse;
        if (reuse && Number(reuse.columns_reused) > 0) {
            showToast(
                `Reused saved mapping from another file for ${reuse.columns_reused} column(s) with the same sheet name. Review cards with the gold highlight.`,
                'info',
            );
        }
        scheduleMappingDraftSave();
        renderMappingCards();

        const hasSemanticFallback = mappingProposal.some(col => col.semantic_status === 'fallback');
        if (statusEl) {
            if (mappingProposal.length > 0 && hasSemanticFallback) {
                statusEl.textContent = 'Mapping Ready (Fallback)';
                statusEl.className = 'status-badge warning';
            } else if (mappingProposal.length > 0) {
                statusEl.textContent = 'Mapping Ready';
                statusEl.className = 'status-badge success';
            } else {
                statusEl.textContent = 'No Columns Found';
                statusEl.className = 'status-badge warning';
            }
        }

    } catch (e) {
        elements.mappingGrid.innerHTML = `<p class="error-text">Failed to load mapping: ${e.message}</p>`;
        if (statusEl) {
            statusEl.textContent = 'Mapping Failed';
            statusEl.className = 'status-badge danger';
        }
    } finally {
        stopMappingProgressPoll();
        await pollMappingProposeProgress();
        if (live) {
            setTimeout(() => {
                const t = String(live.textContent || '').toLowerCase();
                if (t && !t.includes('fail')) {
                    live.textContent = '';
                }
            }, 2500);
        }
    }
}

/** Multiple source columns mapped to the same template field in the current proposal (LLM/heuristic). */
function duplicateTargetColumnsInProposal(rows) {
    const c = {};
    for (const col of rows || getAllMappingRows()) {
        if (!col || typeof col !== 'object') continue;
        const t = col.target_column;
        if (!t || t === 'No match') continue;
        c[t] = (c[t] || 0) + 1;
    }
    return new Set(Object.keys(c).filter((t) => c[t] > 1));
}

/** target field -> source columns when multiple source columns intentionally point at the same target */
function duplicateTargetDetailsInProposal(rows) {
    const byTarget = {};
    for (const col of rows || getAllMappingRows()) {
        if (!col || typeof col !== 'object') continue;
        const target = String(col.target_column || 'No match').trim();
        const source = resolveMappingColumnName(col);
        if (!target || target === 'No match' || !source) continue;
        if (!byTarget[target]) byTarget[target] = [];
        byTarget[target].push(source);
    }
    const details = {};
    for (const [target, sources] of Object.entries(byTarget)) {
        if (sources.length > 1) details[target] = sources;
    }
    return details;
}

/** Target-column <select> scoped to the template columns for this job. */
function buildTargetColumnOptionsHtml(selectedTarget) {
    const current = selectedTarget || 'No match';
    const options = Array.isArray(targetColumnOptions) && targetColumnOptions.length > 0
        ? targetColumnOptions
        : ['No match'];
    return options
        .map((opt) => {
            const sel = opt === current ? ' selected' : '';
            return `<option value="${escapeAttr(opt)}"${sel}>${escapeHtml(opt)}</option>`;
        })
        .join('');
}

/** Role change: coupling with target/decision and autosave. */
window.updateCardRole = function (columnName, role, blockId) {
    if (!columnName || !role) return;
    const col = findMappingColumn(columnName, blockId);
    if (!col) return;
    col.role = role;
    applyRoleRule(col);
    scheduleMappingDraftSave();
    renderMappingCards();
};

/** Target-column change from the source card. */
window.updateCardTarget = function (columnName, newTarget, blockId) {
    if (!columnName) return;
    const col = findMappingColumn(columnName, blockId);
    if (!col) return;
    const target = newTarget || 'No match';
    col.target_column = target;
    col.target_match_method = 'manual';
    col.target_match_confidence = target === 'No match' ? 0.0 : 1.0;
    if (!isDateLikeTargetName(target)) {
        col.date_semantic = '';
    } else if (!col.date_semantic) {
        col.date_semantic = inferDateSemanticFromColumn(col);
    }
    if (col.role !== 'exclude') {
        col.role = inferRoleFromSemantics(col);
    }
    applyRoleRule(col);
    scheduleMappingDraftSave();
    renderMappingCards();
};

function isDateLikeTargetName(name) {
    const t = String(name || '').trim().toLowerCase();
    if (!t || t === 'no match') return false;
    return /(^|_)(date|calendar_date|week_start|week_end|month|quarter|year)(_|$)/.test(t);
}

function isLikelyDateColumnName(name) {
    const n = String(name || '').trim().toLowerCase();
    if (!n) return false;
    // Use word boundaries so "to" does not match inside "total_*" column names.
    return (
        /\b(date|time|datetime|timestamp)\b/.test(n)
        || /\b(week|month|quarter|year)\b/.test(n)
        || /(^|[_\s])(start|end|from|to)([_\s]|$)/i.test(n)
    );
}

/** Date-role UI only when mapped to a template date-like target (not spends/metrics). */
function isDateMappingCardCandidate(col) {
    if (!col || typeof col !== 'object') return false;
    const target = String(col.target_column || '').trim();
    if (!target || target.toLowerCase() === 'no match') return false;
    return isDateLikeTargetName(target);
}

function normalizeDateSemantic(value) {
    const v = String(value || '').trim().toLowerCase();
    const allowed = new Set([
        '',
        'point_in_time',
        'range_start',
        'range_end',
        'period_text',
        'part_year',
        'part_month',
        'part_day',
        'part_quarter',
    ]);
    return allowed.has(v) ? v : '';
}

window.updateCardDateSemantic = function (columnName, value, blockId) {
    if (!columnName) return;
    const col = findMappingColumn(columnName, blockId);
    if (!col) return;
    col.date_semantic = normalizeDateSemantic(value);
    scheduleMappingDraftSave();
    renderMappingCards();
};

function buildDateSemanticSelectHtml(columnName, selectedSemantic, blockId) {
    const selected = normalizeDateSemantic(selectedSemantic || '');
    const safeColumnName = JSON.stringify(columnName || '');
    const safeBlockId = JSON.stringify(blockId || '');
    return `
        <div class="mapping-header-target mapping-header-target--bare">
            <select class="mapping-target-select mapping-target-select--date-role" aria-label="Date role for ${escapeAttr(columnName)}"
                onchange='updateCardDateSemantic(${safeColumnName}, this.value, ${safeBlockId})'>
                <option value="" ${selected === '' ? 'selected' : ''}>Not specified</option>
                <optgroup label="Point in time">
                    <option value="point_in_time" ${selected === 'point_in_time' ? 'selected' : ''}>Single calendar date</option>
                </optgroup>
                <optgroup label="Date range (two columns)">
                    <option value="range_start" ${selected === 'range_start' ? 'selected' : ''}>Range start</option>
                    <option value="range_end" ${selected === 'range_end' ? 'selected' : ''}>Range end</option>
                </optgroup>
                <optgroup label="Range in one cell">
                    <option value="period_text" ${selected === 'period_text' ? 'selected' : ''}>Range text (one column)</option>
                </optgroup>
                <optgroup label="Date parts">
                    <option value="part_year" ${selected === 'part_year' ? 'selected' : ''}>Year</option>
                    <option value="part_month" ${selected === 'part_month' ? 'selected' : ''}>Month</option>
                    <option value="part_day" ${selected === 'part_day' ? 'selected' : ''}>Day</option>
                    <option value="part_quarter" ${selected === 'part_quarter' ? 'selected' : ''}>Quarter</option>
                </optgroup>
            </select>
        </div>
    `;
}

function mappingRowCountsForDateRangeSemantics() {
    const full = getAllMappingRows();
    const starts = [];
    const ends = [];
    for (const col of full) {
        if (!col || typeof col !== 'object') continue;
        if (String(col.role || '').trim().toLowerCase() === 'exclude') continue;
        const t = String(col.target_column || '').trim();
        if (!t || t.toLowerCase() === 'no match') continue;
        const name = String(col.column_name || '').trim();
        if (!name) continue;
        const ds = String(col.date_semantic || '').trim().toLowerCase();
        if (ds === 'range_start') starts.push(name);
        if (ds === 'range_end') ends.push(name);
    }
    return { starts, ends };
}

/** Non-blocking hint when range start/end roles do not form a single valid pair. */
function buildDateRangeSemanticHintBanner() {
    const { starts, ends } = mappingRowCountsForDateRangeSemantics();
    if (!starts.length && !ends.length) return '';

    const msgs = [];
    if (starts.length > 1) {
        msgs.push(
            `Multiple columns marked as range start (${starts.map(escapeHtml).join(', ')}). Use exactly one start column for a date span.`,
        );
    }
    if (ends.length > 1) {
        msgs.push(
            `Multiple columns marked as range end (${ends.map(escapeHtml).join(', ')}). Use exactly one end column for a date span.`,
        );
    }
    if (starts.length >= 1 && ends.length === 0) {
        msgs.push('You marked a range start but no range end. Pair it with an end column or pick another date role.');
    }
    if (ends.length >= 1 && starts.length === 0) {
        msgs.push('You marked a range end but no range start. Pair it with a start column or pick another date role.');
    }
    const su = [...new Set(starts.map((s) => s.toLowerCase()))];
    const eu = [...new Set(ends.map((s) => s.toLowerCase()))];
    if (su.length === 1 && eu.length === 1 && su[0] === eu[0]) {
        msgs.push('Range start and end must be two different source columns.');
    }
    if (!msgs.length) return '';

    return `<div class="mapping-date-range-hint" role="note">${msgs
        .map((m) => `<p class="block-inline-note">${m}</p>`)
        .join('')}</div>`;
}

function buildDuplicateTargetsBannerForRows(rows) {
    const duplicateTargetEntries = Object.entries(duplicateTargetDetailsInProposal(rows));
    if (!duplicateTargetEntries.length) return '';
    return `<div class="matrix-conflict-banner" role="alert"><strong>Multiple source columns mapped to the same target</strong>: ${
        duplicateTargetEntries
            .map(([target, sources]) => `${escapeHtml(target)} ← ${sources.map((item) => escapeHtml(item)).join(', ')}`)
            .join(' · ')
    }. These mappings are preserved; changing one card will not automatically remap the others.</div>`;
}

function renderOneMappingCard(col, blockId) {
    if (!col || typeof col !== 'object') return '';
    const columnName = resolveMappingColumnName(col);
    const role = col.role || inferRoleFromSemantics(col);
    const uniqueValues = Array.isArray(col.unique_values) ? col.unique_values : [];
    const colType = col.column_type || 'Other';
    const stats = col.stats || {};
    const statsKind = stats.kind || null;
    const hasNumericStats = statsKind === 'numeric' && stats.max !== undefined;
    const isBlankColumn = colType === 'Blank' || statsKind === 'blank';
    const badgeClass = String(colType).toLowerCase().replace(/[^a-z0-9]+/g, '-');
    const targetMatchConfidence = Math.round((col.target_match_confidence || 0) * 100);
    const targetMatchMethod = col.target_match_method || 'none';
    const reasoningRaw = String(col.reasoning || '').trim();
    const reasoningShort = reasoningRaw.length > 240 ? `${reasoningRaw.slice(0, 237)}…` : reasoningRaw;
    const reasoningBlock = reasoningRaw
        ? `<p class="mapping-rationale-excerpt" title="${escapeAttr(reasoningRaw)}">${escapeHtml(reasoningShort)}</p>`
        : '';
    const semanticStatus = col.semantic_status || 'ready';
    const semanticLabel = col.classification || 'Unclassified';
    const semanticMeta =
        semanticStatus === 'fallback'
            ? 'Semantic classification used deterministic fallback because the LLM response was invalid.'
            : '';
    const semanticPlain = plainSemanticLabel(semanticLabel);
    const showSemanticChip =
        semanticPlain && semanticPlain.toLowerCase() !== 'unclassified';
    const selectedTarget = col.target_column || 'No match';
    const confidence = Math.max(0, Math.min(1, Number(col.confidence || 0)));
    const mappingSignal = Math.max(
        confidence,
        Math.max(0, Math.min(1, Number(col.target_match_confidence || 0))),
    );
    const mappingSignalPct = Math.round(mappingSignal * 100);
    const isExcluded = role === 'exclude';
    const inlineDateControls = isDateMappingCardCandidate(col)
        ? buildDateSemanticSelectHtml(columnName, col.date_semantic, blockId)
        : '';

    let previewContent = '';
    if (hasNumericStats) {
                previewContent = `
                <div class="stats-grid metrics-only">
                    <div class="stat-item">
                        <span class="stat-label">Min</span>
                        <span class="stat-value">${typeof stats.min === 'number' ? stats.min.toLocaleString() : stats.min}</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-label">Max</span>
                        <span class="stat-value">${typeof stats.max === 'number' ? stats.max.toLocaleString() : stats.max}</span>
                    </div>
                    <div class="stat-item" style="grid-column: span 2;">
                        <span class="stat-label">Mean</span>
                        <span class="stat-value">${typeof stats.mean === 'number' ? Math.round(stats.mean).toLocaleString() : stats.mean}</span>
                    </div>
                </div>`;
            } else if (isBlankColumn) {
                previewContent = `
                <div class="stats-grid metrics-only">
                    <div class="stat-item">
                        <span class="stat-label">Blank Rows</span>
                        <span class="stat-value">${(stats.blank_count ?? 0).toLocaleString()}</span>
                    </div>
                    <div class="stat-item">
                        <span class="stat-label">Non-Blank Rows</span>
                        <span class="stat-value">${(stats.non_null_count ?? 0).toLocaleString()}</span>
                    </div>
                    <div class="stat-item" style="grid-column: span 2;">
                        <span class="stat-label">Blank Ratio</span>
                        <span class="stat-value">${Math.round((stats.blank_ratio || 0) * 100)}%</span>
                    </div>
                </div>`;
            } else {
                previewContent = `
                <div class="samples-container">
                    ${uniqueValues.slice(0, 10).map((v) => `<span class="sample-chip" title="${escapeAttr(v)}">${escapeHtml(String(v))}</span>`).join('')}
                    ${uniqueValues.length === 0 ? '<span class="text-muted" style="font-size: 11px;">No samples available</span>' : ''}
                    ${uniqueValues.length > 10 ? '<span class="sample-more">...</span>' : ''}
                </div>`;
            }

    const reusedPeer = Boolean(
        col.mapping_reused_from_source_id || col._mappingReusedFromSourceId,
    );
    const safeColumnName = JSON.stringify(columnName);
    const safeBlockId = JSON.stringify(blockId || '');
    return `
        <div class="mapping-card mapping-card--by-source${isExcluded ? ' mapping-card--excluded' : ''}${reusedPeer ? ' mapping-card--reused-peer' : ''}" data-source-col="${escapeAttr(columnName)}" data-block-id="${escapeAttr(blockId || '')}" data-role="${escapeAttr(role)}">
            <div class="mapping-card-header mapping-card-header--by-source">
                <div class="mapping-header-left">
                    <span class="mapping-source-col-label" title="Source column from this block">${columnName ? escapeHtml(columnName) : '<em class="text-muted">Unnamed column</em>'}</span>
                    <span class="col-type-badge ${badgeClass}">${escapeHtml(colType)}</span>
                </div>
                <div class="mapping-header-dropdown-row">
                    ${inlineDateControls}
                    <div class="mapping-header-target mapping-header-target--bare">
                        <select class="mapping-target-select mapping-target-select--target-field" aria-label="Target column for ${escapeAttr(columnName)}"
                            onchange='updateCardTarget(${safeColumnName}, this.value, ${safeBlockId})'
                            ${isExcluded ? 'disabled' : ''}>
                            ${buildTargetColumnOptionsHtml(selectedTarget)}
                        </select>
                    </div>
                </div>
                <div class="column-toggle-group" role="group" aria-label="Role for ${escapeAttr(columnName)}">
                    <button type="button"
                        class="column-toggle-btn primary-col${role === 'primary' ? ' active' : ''}"
                        onclick='updateCardRole(${safeColumnName}, "primary", ${safeBlockId})'>Primary Column</button>
                    <button type="button"
                        class="column-toggle-btn support-meta${role === 'supporting' ? ' active' : ''}"
                        onclick='updateCardRole(${safeColumnName}, "supporting", ${safeBlockId})'>Supporting Meta</button>
                    <button type="button"
                        class="column-toggle-btn exclude-col${role === 'exclude' ? ' active' : ''}"
                        onclick='updateCardRole(${safeColumnName}, "exclude", ${safeBlockId})'>Exclude</button>
                </div>
            </div>

            <div class="mapping-card-body">
                <div class="mapping-preview-section">
                    <span class="section-label">${hasNumericStats ? 'Descriptive Statistics' : isBlankColumn ? 'Blank Column Summary' : 'Unique Values (Top 10)'}</span>
                    ${previewContent}
                </div>

                <div class="mapping-reasoning-section">
                    <span class="section-label">Semantic type</span>
                    ${showSemanticChip ? `<p class="ai-reason-text" style="margin: 0;"><strong>${escapeHtml(semanticPlain)}</strong></p>` : ''}
                    ${semanticMeta ? `<p class="ai-reason-text" style="margin-top: 6px; color: #f59e0b;">${escapeHtml(semanticMeta)}</p>` : ''}
                    <p class="ai-reason-text target-match-meta">
                        ${targetMatchMethod === 'none'
                            ? `<strong>Template field match:</strong> none selected (best name similarity ${targetMatchConfidence}%).`
                            : `<strong>Template field match:</strong> ${escapeHtml(targetMatchMethod)} (${targetMatchConfidence}% — how well the <em>column name</em> lines up with the chosen template field).`}
                    </p>
                    ${reasoningBlock}
                    <div class="confidence-row" title="Mapping signal uses the stronger of semantic classification confidence and template field match confidence.">
                        <span><strong>Mapping signal</strong> ${mappingSignalPct}%</span>
                        <div class="conf-bar-bg">
                            <div class="conf-bar-fill" style="width: ${mappingSignalPct}%; background: ${getConfColor(mappingSignal)}"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function renderMappingCards() {
    syncFlatMappingProposal();
    const sections = getMappingSectionsForRender();
    const full = sections.flatMap((section) => section.mapping || []);
    if (!Array.isArray(full)) {
        elements.mappingGrid.innerHTML = '<p class="empty-state">No columns identified for mapping.</p>';
        return;
    }
    if (full.length === 0) {
        elements.mappingGrid.innerHTML = `
            <div class="empty-state">
                <p><strong>No columns identified for mapping.</strong></p>
                <p class="mapping-cards-help">Confirm layout and open mapping again, or refresh the proposal.</p>
            </div>`;
        return;
    }

    const multiBlock = isMultiBlockMappingUi();
    const scopeIntro = multiBlock
        ? `
        <p class="mapping-cards-help" role="note">
            This sheet has <strong>${sections.length} main data blocks</strong> side by side. Map each block separately below — columns with the same name in different blocks are independent.
            Suggestions come from <strong>heuristics</strong> and the <strong>LLM</strong> when this sheet is first proposed on the server.
        </p>`
        : `
        <p class="mapping-cards-help" role="note">
            Each card is one <strong>source column</strong> for this sheet. Pick the <strong>template field</strong>, then <strong>Primary</strong> / <strong>Supporting</strong> / <strong>Exclude</strong>.
            Suggestions come from <strong>heuristics</strong> (names + <code>synonyms.json</code>) and, when this sheet is first proposed on the server, the <strong>LLM</strong> if your config has a model key — saved or cached sheets load without a new model call.
        </p>`;
    const duplicateTargetsBanner = multiBlock
        ? ''
        : buildDuplicateTargetsBannerForRows(full);
    const reuseCount = full.filter(
        (c) => c && (c.mapping_reused_from_source_id || c._mappingReusedFromSourceId),
    ).length;
    const reuseBanner =
        reuseCount > 0
            ? `<div class="mapping-reuse-banner" role="status"><strong>Reused mapping</strong> — ${reuseCount} column(s) match a <strong>same-named sheet</strong> already mapped in this job. Gold border = copy; change targets or roles anytime.</div>`
            : '';
    const dateRangeSemanticHintBanner = buildDateRangeSemanticHintBanner();

    const restoreWindowScrollY = setupUiStep === 2 ? window.scrollY : null;

    const cardsHtml = sections
        .map((section) => {
            const blockId = section.block_id || '';
            const blockRows = Array.isArray(section.mapping) ? section.mapping : [];
            if (!blockRows.length) return '';
            const sectionCards = blockRows.map((col) => renderOneMappingCard(col, blockId)).join('');
            if (!multiBlock) {
                return sectionCards;
            }
            const rangeHint = section.excel_range
                ? `<span class="mapping-block-section__range">${escapeHtml(section.excel_range)}</span>`
                : '';
            const sectionDupBanner = buildDuplicateTargetsBannerForRows(blockRows);
            return `
        <section class="mapping-block-section" data-block-id="${escapeAttr(blockId)}">
            <header class="mapping-block-section__header">
                <h3 class="mapping-block-section__title">${escapeHtml(section.block_label || 'Main data block')}</h3>
                ${rangeHint}
            </header>
            ${sectionDupBanner}
            <div class="mapping-block-section__cards">${sectionCards}</div>
        </section>`;
        })
        .join('');

    elements.mappingGrid.innerHTML =
        scopeIntro + reuseBanner + duplicateTargetsBanner + dateRangeSemanticHintBanner + cardsHtml;

    if (restoreWindowScrollY != null) {
        requestAnimationFrame(() => {
            window.scrollTo(window.scrollX, restoreWindowScrollY);
        });
    }
}

function getConfColor(conf) {
    if (conf > 0.9) return '#10b981';
    if (conf > 0.7) return '#f59e0b';
    return '#ef4444';
}

// ===== Transitions =====

function switchStep(step) {
    const demSticky = document.getElementById('demarcationToolbarStickyRow');
    const mapSticky = document.getElementById('mappingToolbarStickyRow');
    if (step === 1) {
        setupUiStep = 1;
        if (elements.tabDemarcation) elements.tabDemarcation.classList.add('active');
        if (elements.tabMapping) elements.tabMapping.classList.remove('active');
        elements.demarcationSection.classList.remove('hidden');
        elements.mappingSection.classList.add('hidden');
        if (demSticky) demSticky.classList.remove('hidden');
        if (mapSticky) mapSticky.classList.add('hidden');
        loadDemarcation(false);
    } else {
        setupUiStep = 2;
        if (elements.tabDemarcation) elements.tabDemarcation.classList.remove('active');
        if (elements.tabMapping) elements.tabMapping.classList.add('active');
        elements.demarcationSection.classList.add('hidden');
        elements.mappingSection.classList.remove('hidden');
        if (demSticky) demSticky.classList.add('hidden');
        if (mapSticky) mapSticky.classList.remove('hidden');
        const cachedRows = getAllMappingRows();
        const hasSamples = cachedRows.some(
            (col) => Array.isArray(col?.unique_values) && col.unique_values.length > 0,
        );
        if (cachedRows.length > 0 && hasSamples) {
            renderMappingCards();
            const statusEl = document.getElementById('mappingStatus');
            if (statusEl) {
                statusEl.textContent = 'Mapping Ready';
                statusEl.className = 'status-badge success';
            }
        } else {
            loadMapping(false);
        }
    }
    refreshUxStepperFromJob(lastSetupJobForHeader);
}

async function persistCurrentMappingSilently() {
    const rows = getAllMappingRows();
    if (!currentJobId || rows.length === 0) {
        return;
    }
    if (setupUiStep !== 2) return;
    const r = await fetch(`/api/mapping/submit/${currentJobId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            mapping: sanitizeMappingForApi(rows),
            sheet_name: currentSheet,
            source_id: currentSourceId,
        }),
    });
    const mapData = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(mapData.error || `Mapping submit failed (${r.status})`);
    if (mapData.error) throw new Error(mapData.error);
}

function refreshPrimaryActionButtonForEl(btn, job) {
    if (!btn) return;
    if (btn === elements.confirmLayoutMapSheetBtn) {
        refreshLayoutPrimaryActionButton(job);
    } else {
        refreshMappingPrimaryActionButton(job);
    }
}

async function goToHarmonizationPrep(triggerEl) {
    const btn = triggerEl || elements.saveMappingNextSheetBtn;
    const fromLayoutBtn = btn === elements.confirmLayoutMapSheetBtn;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Saving...';
    }
    let navigatedAway = false;

    try {
        const preJob = await getJobStatus(currentJobId);
        lastSetupJobForHeader = preJob;
        const guidedReady = fromLayoutBtn
            ? computeSheetsGuidedSetupDoneAssumingCurrentLayoutSaved(preJob)
            : computeSheetsGuidedSetupDone(preJob);
        if (!guidedReady) {
            showToast(
                'Complete layout and column mapping for every sheet first (see the Sheets line in the header).',
                'warning',
            );
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '';
                refreshPrimaryActionButtonForEl(btn, preJob);
            }
            return;
        }

        if (btn) btn.dataset.finalizing = '1';
        await persistCurrentMappingSilently();
        await patchJobUxState(currentJobId, { current_ux_stage: 2 });

        try {
            sessionStorage.setItem('schemaAgentAutostartProcess', '1');
        } catch {
            /* ignore */
        }
        showToast('Opening Console — run will start automatically.', 'success');
        navigateTo('processing');
        navigatedAway = true;
    } catch (e) {
        showToast(`Could not open console: ${e.message}`, 'error');
    } finally {
        if (btn) delete btn.dataset.finalizing;
        if (!navigatedAway && btn) {
            btn.innerHTML = '';
            btn.disabled = false;
            refreshPrimaryActionButtonForEl(btn, lastSetupJobForHeader);
        }
        if (!navigatedAway) {
            refreshPostMappingGlobalActions(lastSetupJobForHeader);
            void getJobStatus(currentJobId)
                .then((j) => {
                    lastSetupJobForHeader = j;
                    refreshUxStepperFromJob(j);
                })
                .catch(() => refreshPostMappingGlobalActions(lastSetupJobForHeader));
        }
    }
}

async function skipAndAutoProcess() {
    if (!currentJobId) {
        showToast('No active job.', 'warning');
        return;
    }
    try {
        if (setupUiStep === 2 && mappingProposal && mappingProposal.length > 0) {
            try {
                await persistCurrentMappingSilently();
            } catch (e) {
                showToast(`Could not save this sheet’s mapping: ${e.message}`, 'warning');
            }
        }
        try {
            await patchJobUxState(currentJobId, { current_ux_stage: 2 });
        } catch {
            /* ignore */
        }
        try {
            sessionStorage.setItem('schemaAgentAutostartProcess', '1');
        } catch {
            /* ignore */
        }
        navigateTo('processing');
    } catch (e) {
        showToast(`Skip failed: ${e.message}`, 'error');
    }
}
