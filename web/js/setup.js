/**
 * Schema Agent - Setup Workspace
 * Handles Column shaping (Step 1) and Column Mapping (Step 2)
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
/** @type {Map<string, {id: string, name: string, kind: string, supports_currency: boolean}>} */
let targetColumnMeta = new Map();
const ADD_CUSTOM_METRIC = '__add_custom_metric__';
const VALUE_SCALE_CHOICES = [
    { value: '1', label: 'As reported (×1)' },
    { value: '1000', label: "Thousands ('000)" },
    { value: '1000000', label: 'Millions' },
];
const CURRENCY_CHOICES = [
    { value: '', label: 'Currency not set' },
    { value: 'EUR', label: 'EUR' },
    { value: 'USD', label: 'USD' },
    { value: 'GBP', label: 'GBP' },
    { value: 'CHF', label: 'CHF' },
    { value: 'PLN', label: 'PLN' },
    { value: 'SEK', label: 'SEK' },
    { value: 'NOK', label: 'NOK' },
    { value: 'DKK', label: 'DKK' },
    { value: 'Other', label: 'Other…' },
];
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
/** 1 = layout, 1.5 = messy standardize, 1.75 = column std, 2 = mapping. */
let setupUiStep = 1;
/** Last standardize API payload (assignment + preview) for the current sheet. */
let standardizeState = null;
/** Column shaping (multipart split/combine) working state for current sheet. */
let columnStdState = null;
let columnStdDirty = false;
let columnStdSaveTimer = null;
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
    standardizeSection: document.getElementById('standardizeSection'),
    standardizeWorkspace: document.getElementById('standardizeWorkspace'),
    columnStdSection: document.getElementById('columnStdSection'),
    columnStdWorkspace: document.getElementById('columnStdWorkspace'),
    confirmLayoutMapSheetBtn: document.getElementById('confirmLayoutMapSheetBtn'),
    confirmStandardizeMapBtn: document.getElementById('confirmStandardizeMapBtn'),
    confirmColumnStdBtn: document.getElementById('confirmColumnStdBtn'),
    regenerateStandardTableBtn: document.getElementById('regenerateStandardTableBtn'),
    backToLayoutFromStdBtn: document.getElementById('backToLayoutFromStdBtn'),
    backFromColumnStdBtn: document.getElementById('backFromColumnStdBtn'),
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
const CONFIRM_LAYOUT_STANDARDIZE_LABEL = 'Confirm & standardize →';
/** Legacy — Layout Demarcation removed from Guided Setup happy path */
const SETUP_STEP_LAYOUT = 1.25;
const SETUP_STEP_STANDARDIZE = 1.5;
/** Column shaping: multipart detect → split/keep → combine parts → media dimensions */
const SETUP_STEP_COLUMN_STD = 1;
const SETUP_STEP_MAPPING = 2;
const COLSTD_CUSTOM = '__custom__';
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
    const runMode = setupUiStep === SETUP_STEP_MAPPING && computeSheetsGuidedSetupDone(j);
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
    const dec = String(normalizeBlockDecision(block)).trim().toLowerCase();
    if (dec === 'discard' || dec === 'context' || dec === 'metadata' || dec === 'ignore' || dec === 'noise' || dec === 'use as context') {
        return false;
    }
    if (dec === 'keep' || dec === 'approved') return true;
    const cat = String(block.category || '')
        .trim()
        .toLowerCase()
        .replace(/\s+/g, '')
        .replace(/_/g, '');
    return cat === 'maindata';
}

/** Human label for the block card header — reflects the user's toggle, not the stale AI category. */
function effectiveBlockTypeLabel(block) {
    const dec = String(normalizeBlockDecision(block)).trim().toLowerCase();
    if (dec === 'keep' || dec === 'approved') return 'Main Data';
    if (dec === 'context' || dec === 'metadata' || dec === 'use as context') return 'Metadata';
    if (dec === 'discard' || dec === 'ignore' || dec === 'noise') return 'Ignored';
    return block.category || 'Unknown';
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
function sheetNeedsStandardize(report) {
    if (!report || typeof report !== 'object') return false;
    const cls = String(report.classification || '').toLowerCase();
    const score = Number(report.complexity_score);
    if (cls === 'messy') return true;
    if (!Number.isNaN(score) && score >= 50) return true;
    if (report.crosstab) return true;
    if (report.adapter_used || report.use_adapter) return true;
    return false;
}

function currentSheetNeedsStandardize() {
    return sheetNeedsStandardize(demarcationProposal?.layout_complexity);
}

function refreshLayoutPrimaryActionButton(job) {
    const btn = elements.confirmLayoutMapSheetBtn;
    if (!btn || setupUiStep !== SETUP_STEP_LAYOUT) return;
    if (btn.dataset.finalizing === '1' || btn.querySelector('.spinner')) return;
    const j = job || lastSetupJobForHeader;
    const mainKept = countKeptMainDataBlocksFromProposal(demarcationProposal?.blocks);
    if (mainKept > 0) {
        const tidy = currentSheetNeedsStandardize();
        btn.textContent = tidy ? CONFIRM_LAYOUT_STANDARDIZE_LABEL : CONFIRM_LAYOUT_MAP_LABEL;
        btn.title = tidy
            ? 'Save this sheet’s layout and convert the messy grid into a standard Date / Dimensions / Metrics table'
            : 'Save this sheet’s layout and open column mapping for the same sheet';
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
        const lm = `${layoutDone ? 'C✓' : 'C·'} ${mappingDone ? 'M✓' : 'M·'}`;
        const pipTitle = `${sheetPart} — ${layoutDone ? 'column shaping saved' : 'column shaping pending'}; ${mappingDone ? 'mapping saved' : 'mapping pending'}`;
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

    setCurrentJob(currentJobId, currentSheet, currentSourceId);
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

        const dataFiles = job.data_files || [];
        const uxs = job.ux_stepper_summary || {};
        const hierarchyOk = Boolean(
            job.hierarchy_register_complete
            || uxs.hierarchy_register_complete
        );
        if (dataFiles.length > 0 && !hierarchyOk) {
            showToast('Register media hierarchy on Upload before Guided Setup.', 'warning');
            const q = `upload.html?job_id=${encodeURIComponent(currentJobId)}`;
            setTimeout(() => { window.location.href = q; }, 1200);
            return;
        }

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

        // Start with Column shaping (Layout Demarcation removed from happy path)

        try {
            const prevLabels = localStorage.getItem('setupPreviewShowHeaderLabels');
            if (elements.previewShowHeaderLabelsChk && prevLabels === 'true') {
                elements.previewShowHeaderLabelsChk.checked = true;
            }
        } catch (e) { /* ignore */ }

        await loadSourceInventory();
        refreshUxStepperFromJob(job);
        switchStep(SETUP_STEP_COLUMN_STD);

        if (elements.setupSourcePanels && !elements.setupSourcePanels.dataset.clickBound) {
            elements.setupSourcePanels.dataset.clickBound = '1';
            elements.setupSourcePanels.addEventListener('click', onSetupSourcePanelClick);
        }

        if (elements.backToLayoutBtn) {
            elements.backToLayoutBtn.addEventListener('click', () => {
                switchStep(SETUP_STEP_COLUMN_STD);
            });
        }
        if (elements.confirmColumnStdBtn) {
            elements.confirmColumnStdBtn.addEventListener('click', () => confirmColumnStdAndOpenMapping());
        }
        if (elements.saveMappingNextSheetBtn) {
            elements.saveMappingNextSheetBtn.addEventListener('click', () => onSaveMappingOrRunAgentClick());
        }
        initCollapsiblePanels();
        elements.skipProcessBtn?.addEventListener('click', () => skipAndAutoProcess());

        document.getElementById('closePreviewBtn').addEventListener('click', closeModal);
        const reScanBtn = document.getElementById('reScanBtn');
        if (reScanBtn) reScanBtn.addEventListener('click', () => loadDemarcationBatch(true));
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

        window.addEventListener('pagehide', flushSetupDraftsBeacon);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') flushSetupDraftsBeacon();
        });

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
    if (columnStdSaveTimer) {
        clearTimeout(columnStdSaveTimer);
        columnStdSaveTimer = null;
    }
    // Flush drafts for the *current* sheet before switching ids
    if (setupUiStep === SETUP_STEP_MAPPING && currentJobId && mappingProposal && mappingProposal.length > 0) {
        try {
            await persistMappingDraft();
        } catch {
            /* best-effort flush before switching sheet */
        }
    }
    if (columnStdState && currentJobId && currentSourceId && columnStdDirty) {
        try {
            await persistColumnStdDraft(false);
        } catch {
            /* best-effort */
        }
    }
    currentSourceId = sourceId;
    currentSheet = sheetName || null;
    setCurrentJob(currentJobId, currentSheet, currentSourceId);
    syncSetupUrlWithSelection();

    renderSetupSourcePanels();
    updateSetupContextHeader();
    columnStdState = null;
    columnStdDirty = false;
    if (setupUiStep === SETUP_STEP_STANDARDIZE || setupUiStep === SETUP_STEP_LAYOUT) {
        switchStep(SETUP_STEP_COLUMN_STD);
        return;
    }
    if (setupUiStep === SETUP_STEP_COLUMN_STD) {
        await loadColumnStd(true);
        loadSourceInventory();
    } else if (setupUiStep === SETUP_STEP_MAPPING) {
        await loadMapping(false);
    } else {
        switchStep(SETUP_STEP_COLUMN_STD);
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
    } else if (demarcationProposal.layout_complexity && demarcationProposal.layout_complexity.adapter_used) {
        const st = demarcationProposal.layout_complexity.sheet_type || 'matrix';
        statusEl.textContent = `Layout diagnostics (${st})`;
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

function formatLayoutSampleRecord(rec) {
    if (!rec || typeof rec !== 'object') return '';
    const bits = [];
    if (rec.dimensions) bits.push(Array.isArray(rec.dimensions) ? rec.dimensions.join(' / ') : String(rec.dimensions));
    if (rec.metric) bits.push(`Metric: ${rec.metric}`);
    if (rec.period) bits.push(`Period: ${rec.period}`);
    if (rec.value != null) bits.push(`Value: ${rec.value}`);
    return bits.join(' · ');
}

const LAYOUT_ROLE_META = {
    metadata: { label: 'Metadata', color: '#94a3b8' },
    time: { label: 'Time', color: '#f59e0b' },
    dimensions: { label: 'Dimensions', color: '#14b8a6' },
    metrics: { label: 'Metrics', color: '#a78bfa' },
    values: { label: 'Values', color: '#60a5fa' },
    totals: { label: 'Totals', color: '#fb923c' },
    noise: { label: 'Comments / noise', color: '#f87171' },
};

let layoutMinimapState = null;
let layoutMinimapResizeObserver = null;
let layoutMinimapWantExpanded = false;

function layoutRoleColor(role) {
    return (LAYOUT_ROLE_META[role] || {}).color || '#64748b';
}

function hexToRgba(hex, alpha) {
    const raw = String(hex || '').replace('#', '');
    const n = parseInt(raw.length === 3 ? raw.split('').map((c) => c + c).join('') : raw, 16);
    if (Number.isNaN(n)) return `rgba(100, 116, 139, ${alpha})`;
    const r = (n >> 16) & 255;
    const g = (n >> 8) & 255;
    const b = n & 255;
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function layoutOverlayArea(overlay) {
    const rows = Number(overlay.end_row) - Number(overlay.start_row) + 1;
    const cols = Number(overlay.end_col) - Number(overlay.start_col) + 1;
    return Math.max(1, rows) * Math.max(1, cols);
}

function buildLayoutComplexityPanel(report) {
    if (!report || typeof report !== 'object') return '';
    const sheetType = String(report.sheet_type || report.classification || 'unknown').replace(/_/g, ' ');
    const score = report.complexity_score != null ? Number(report.complexity_score) : null;
    const cls = String(report.classification || '');
    const reasons = Array.isArray(report.reasons) ? report.reasons : [];
    const roles = report.roles_for_review || {};
    const roleOrder = ['metadata', 'time', 'dimensions', 'metrics', 'values', 'totals', 'noise'];
    const overlayRoles = new Set((report.minimap?.overlays || []).map((o) => o.role));
    const roleRows = roleOrder.map((key) => {
        const role = roles[key];
        if (!role) return '';
        const color = layoutRoleColor(key);
        return `<tr class="layout-role-row" data-role="${escapeAttr(key)}" style="--role-color:${color}">
            <td><span class="layout-role-swatch" aria-hidden="true"></span>${escapeHtml(role.label || key)}</td>
            <td>${escapeHtml(String(role.kind || role.axis || role.cell || '—'))}</td>
            <td>${escapeHtml(String(role.detail || ''))}</td>
        </tr>`;
    }).join('');
    const samples = Array.isArray(report.sample_records) ? report.sample_records.slice(0, 8) : [];
    const sampleHtml = samples.length
        ? `<p class="section-label">Sample records (click to locate on the map)</p>
           <ol class="layout-sample-records">${samples.map((s, i) => {
               const row = s.row != null ? Number(s.row) : '';
               const col = s.col != null ? Number(s.col) : '';
               return `<li class="layout-sample-item" data-sample-index="${i}" data-row="${escapeAttr(String(row))}" data-col="${escapeAttr(String(col))}" tabindex="0">${escapeHtml(formatLayoutSampleRecord(s))}</li>`;
           }).join('')}</ol>`
        : '';
    const legendKeys = roleOrder.filter((key) => overlayRoles.has(key));
    const legendHtml = legendKeys.map((key) => {
        const meta = LAYOUT_ROLE_META[key] || { label: key, color: '#64748b' };
        return `<button type="button" class="layout-minimap-legend-item" data-role="${escapeAttr(key)}" style="--role-color:${meta.color}">
            <span class="layout-role-swatch" aria-hidden="true"></span>${escapeHtml(meta.label)}
        </button>`;
    }).join('');
    const hasMinimap = report.minimap && report.minimap.occupancy;
    const messy = sheetNeedsStandardize(report);
    const adapterNote = messy
        ? 'This sheet is not a flat table. Confirm the map, then Confirm & standardize to assign Date / Dimensions / Metrics / Values / noise and generate a standard table for column mapping.'
        : (report.adapter_used
            ? 'Confirm Time / Dimensions / Metrics / Values on the map. The two regions below are only what Confirm & map saves — not the old island-by-island walkthrough.'
            : 'Standard connected-component blocks (clean table path).');
    return `
        <div class="layout-complexity-panel" data-layout-complexity="1">
            <div class="layout-complexity-header">
                <div>
                    <h3>Sheet understanding — ${escapeHtml(sheetType)}</h3>
                    <p class="layout-complexity-meta">
                        ${score != null ? `Complexity score ${escapeHtml(String(score))} · ` : ''}
                        Band: ${escapeHtml(cls || 'n/a')}
                        ${report.shape ? ` · Shape: ${escapeHtml(String(report.shape))}` : ''}
                        ${report.crosstab ? ' · Crosstab / period headers' : ''}
                    </p>
                </div>
                ${hasMinimap ? `<button type="button" class="layout-minimap-zoom-btn layout-minimap-expand-btn" data-minimap-expand aria-expanded="false" title="Open map at full width">Expand</button>` : ''}
            </div>
            ${reasons.length ? `<ul class="layout-complexity-reasons">${reasons.map((r) => `<li>${escapeHtml(String(r))}</li>`).join('')}</ul>` : ''}
            <div class="layout-complexity-body">
                ${hasMinimap ? `
                <div class="layout-minimap-wrap">
                    <div class="layout-minimap-toolbar">
                        <p class="section-label">Sheet map</p>
                        <div class="layout-minimap-zoom-btns">
                            <button type="button" class="layout-minimap-zoom-btn" data-minimap-zoom="out" title="Zoom out" aria-label="Zoom out">−</button>
                            <span class="layout-minimap-zoom-label" data-minimap-zoom-label>100%</span>
                            <button type="button" class="layout-minimap-zoom-btn" data-minimap-zoom="in" title="Zoom in" aria-label="Zoom in">+</button>
                            <button type="button" class="layout-minimap-zoom-btn layout-minimap-fit-btn" data-minimap-zoom="home" title="Readable top-left">Home</button>
                            <button type="button" class="layout-minimap-zoom-btn layout-minimap-fit-btn" data-minimap-zoom="fit" title="Fit whole sheet">Fit</button>
                            <button type="button" class="layout-minimap-zoom-btn layout-minimap-load-btn" data-minimap-load-cells title="Fetch every non-empty cell once and keep it while you pan">Load all cells</button>
                            <button type="button" class="layout-minimap-zoom-btn layout-minimap-expand-btn" data-minimap-expand aria-expanded="false" title="Open map at full width">Expand</button>
                        </div>
                    </div>
                    <canvas class="layout-minimap" tabindex="0" role="img" aria-label="Sheet occupancy map. Scroll to zoom, drag to pan."></canvas>
                    <p class="layout-minimap-nav-hint">Scroll to move · Ctrl+scroll to zoom · drag to pan · Load all cells keeps values on screen</p>
                    <div class="layout-minimap-legend">${legendHtml}</div>
                    <p class="layout-minimap-hint" data-layout-minimap-hint></p>
                </div>` : ''}
                <div class="layout-complexity-confirm">
                    <div class="layout-complexity-roles">
                        <p class="section-label">Confirm what will be used</p>
                        <table class="layout-role-table">
                            <thead><tr><th>Role</th><th>Detected as</th><th>Where</th></tr></thead>
                            <tbody>${roleRows}</tbody>
                        </table>
                    </div>
                    ${sampleHtml ? `<div class="layout-complexity-samples">${sampleHtml}</div>` : ''}
                </div>
            </div>
            <p class="block-inline-note">${escapeHtml(adapterNote)}${messy ? '' : (report.adapter_used ? ' Approve the map, then Confirm &amp; map.' : ' Approve with Treat as Data / Metadata / Ignore on the regions below, then Confirm &amp; map.')}</p>
        </div>`;
}

function layoutMinimapBboxRect(bbox, minimap, width, height) {
    const cols = Math.max(1, Number(minimap.cols) || 1);
    const rows = Math.max(1, Number(minimap.rows) || 1);
    const originC = Number(minimap.start_col) || 0;
    const originR = Number(minimap.start_row) || 0;
    const x = ((Number(bbox.start_col) - originC) / cols) * width;
    const y = ((Number(bbox.start_row) - originR) / rows) * height;
    const w = ((Number(bbox.end_col) - Number(bbox.start_col) + 1) / cols) * width;
    const h = ((Number(bbox.end_row) - Number(bbox.start_row) + 1) / rows) * height;
    return { x, y, w: Math.max(w, 1), h: Math.max(h, 1) };
}

const LAYOUT_MINIMAP_MIN_SCALE = 1;
const LAYOUT_MINIMAP_MAX_SCALE = 24;
const LAYOUT_MINIMAP_GUTTER_LEFT = 44;
const LAYOUT_MINIMAP_GUTTER_TOP = 24;
let layoutMinimapPreviewTimer = null;
let layoutMinimapPreviewReq = 0;

function layoutMinimapViewSize() {
    const canvas = layoutMinimapState?.canvas;
    if (!canvas) return { cssW: 1, cssH: 1 };
    const expanded = Boolean(layoutMinimapWantExpanded || layoutMinimapState?.expanded);
    return {
        cssW: Math.max(1, canvas.clientWidth || canvas.getBoundingClientRect().width),
        cssH: Math.max(1, canvas.clientHeight || (expanded ? 560 : 400)),
    };
}

function layoutMinimapMetrics() {
    const minimap = layoutMinimapState?.minimap || {};
    const { cssW, cssH } = layoutMinimapViewSize();
    const rows = Math.max(1, Number(minimap.rows) || 1);
    const cols = Math.max(1, Number(minimap.cols) || 1);
    const scale = layoutMinimapState?.scale || 1;
    const gutterL = LAYOUT_MINIMAP_GUTTER_LEFT;
    const gutterT = LAYOUT_MINIMAP_GUTTER_TOP;
    const innerW = Math.max(1, cssW - gutterL);
    const innerH = Math.max(1, cssH - gutterT);
    const colPx = (innerW / cols) * scale;
    const rowPx = (innerH / rows) * scale;
    return {
        cssW,
        cssH,
        innerW,
        innerH,
        gutterL,
        gutterT,
        rows,
        cols,
        scale,
        colPx,
        rowPx,
        originR: Number(minimap.start_row) || 0,
        originC: Number(minimap.start_col) || 0,
        showText: colPx >= 28 && rowPx >= 10,
    };
}

function clampLayoutMinimapCamera() {
    if (!layoutMinimapState) return;
    const m = layoutMinimapMetrics();
    const scale = m.scale;
    const contentW = m.innerW * scale;
    const contentH = m.innerH * scale;
    if (contentW <= m.innerW) {
        layoutMinimapState.panX = (m.innerW - contentW) / 2;
    } else {
        layoutMinimapState.panX = Math.min(0, Math.max(m.innerW - contentW, layoutMinimapState.panX));
    }
    if (contentH <= m.innerH) {
        layoutMinimapState.panY = (m.innerH - contentH) / 2;
    } else {
        layoutMinimapState.panY = Math.min(0, Math.max(m.innerH - contentH, layoutMinimapState.panY));
    }
}

function updateLayoutMinimapZoomLabel() {
    const el = document.querySelector('[data-minimap-zoom-label]');
    if (!el || !layoutMinimapState) return;
    const home = layoutMinimapHomeScale();
    const current = layoutMinimapState.scale || home;
    el.textContent = `${Math.max(1, Math.round((current / home) * 100))}%`;
}

function layoutMinimapHomeScale() {
    if (!layoutMinimapState) return 1;
    if (!layoutMinimapState.homeScale) {
        layoutMinimapState.homeScale = readableLayoutMinimapScale();
    }
    return layoutMinimapState.homeScale;
}

function layoutMinimapMaxScale() {
    return Math.max(LAYOUT_MINIMAP_MAX_SCALE, layoutMinimapHomeScale() * 4);
}

function layoutMinimapScreenToWorld(sx, sy) {
    const m = layoutMinimapMetrics();
    return {
        x: (sx - m.gutterL - (layoutMinimapState.panX || 0)) / m.scale,
        y: (sy - m.gutterT - (layoutMinimapState.panY || 0)) / m.scale,
    };
}

function layoutMinimapCellFromEvent(event, canvas, minimap) {
    const rect = canvas.getBoundingClientRect();
    const world = layoutMinimapScreenToWorld(event.clientX - rect.left, event.clientY - rect.top);
    const m = layoutMinimapMetrics();
    const cols = Math.max(1, Number(minimap.cols) || 1);
    const rows = Math.max(1, Number(minimap.rows) || 1);
    const col = Math.floor((world.x / Math.max(m.innerW, 1)) * cols) + (Number(minimap.start_col) || 0);
    const row = Math.floor((world.y / Math.max(m.innerH, 1)) * rows) + (Number(minimap.start_row) || 0);
    return { row, col };
}

function zoomLayoutMinimapAt(sx, sy, factor) {
    if (!layoutMinimapState) return;
    const prev = layoutMinimapState.scale || 1;
    const next = Math.min(layoutMinimapMaxScale(), Math.max(LAYOUT_MINIMAP_MIN_SCALE, prev * factor));
    if (next === prev) return;
    const m = layoutMinimapMetrics();
    const world = layoutMinimapScreenToWorld(sx, sy);
    layoutMinimapState.scale = next;
    layoutMinimapState.panX = (sx - m.gutterL) - world.x * next;
    layoutMinimapState.panY = (sy - m.gutterT) - world.y * next;
    clampLayoutMinimapCamera();
    updateLayoutMinimapZoomLabel();
    drawLayoutMinimap();
}

function resetLayoutMinimapView() {
    if (!layoutMinimapState) return;
    layoutMinimapState.scale = 1;
    layoutMinimapState.panX = 0;
    layoutMinimapState.panY = 0;
    clampLayoutMinimapCamera();
    updateLayoutMinimapZoomLabel();
    drawLayoutMinimap();
}

function readableLayoutMinimapScale() {
    if (!layoutMinimapState) return 1;
    const prev = layoutMinimapState.scale || 1;
    layoutMinimapState.scale = 1;
    const m = layoutMinimapMetrics();
    layoutMinimapState.scale = prev;
    const rowScale = (18 * m.rows) / Math.max(m.innerH, 1);
    const colScale = (48 * m.cols) / Math.max(m.innerW, 1);
    return Math.min(LAYOUT_MINIMAP_MAX_SCALE, Math.max(1, rowScale, colScale));
}

function readableLayoutMinimapView() {
    if (!layoutMinimapState) return;
    layoutMinimapState.homeScale = readableLayoutMinimapScale();
    layoutMinimapState.scale = layoutMinimapState.homeScale;
    layoutMinimapState.panX = 0;
    layoutMinimapState.panY = 0;
    clampLayoutMinimapCamera();
    updateLayoutMinimapZoomLabel();
    drawLayoutMinimap();
}

function syncLayoutMinimapExpandButtons() {
    document.querySelectorAll('[data-minimap-expand]').forEach((btn) => {
        btn.setAttribute('aria-expanded', layoutMinimapWantExpanded ? 'true' : 'false');
        btn.textContent = layoutMinimapWantExpanded ? 'Collapse' : 'Expand';
        btn.title = layoutMinimapWantExpanded ? 'Exit full-width map (Esc)' : 'Open map at full width';
    });
}

function setLayoutMinimapExpanded(expanded) {
    const panel = document.querySelector('.layout-complexity-panel');
    if (!panel) return;
    layoutMinimapWantExpanded = Boolean(expanded);
    panel.classList.toggle('is-expanded', layoutMinimapWantExpanded);
    document.body.classList.toggle('layout-map-expanded', layoutMinimapWantExpanded);
    if (layoutMinimapState) layoutMinimapState.expanded = layoutMinimapWantExpanded;
    syncLayoutMinimapExpandButtons();
    requestAnimationFrame(() => {
        requestAnimationFrame(() => readableLayoutMinimapView());
    });
}

function onLayoutMapExpandKeydown(event) {
    if (event.key === 'Escape' && layoutMinimapWantExpanded) {
        event.preventDefault();
        setLayoutMinimapExpanded(false);
    }
}

function focusLayoutMinimapRect(rect) {
    if (!layoutMinimapState || !rect) return;
    const m = layoutMinimapMetrics();
    const tall = rect.h > rect.w * 6;
    const wide = rect.w > rect.h * 6;
    let scale;
    const cap = layoutMinimapMaxScale();
    if (tall) {
        scale = Math.min(cap, Math.max(layoutMinimapHomeScale() * 0.35, (m.innerW * 0.22) / Math.max(rect.w, 1)));
    } else if (wide) {
        scale = Math.min(cap, Math.max(layoutMinimapHomeScale() * 0.35, (m.innerH * 0.18) / Math.max(rect.h, 1)));
    } else {
        scale = Math.min(
            cap,
            Math.max(layoutMinimapHomeScale() * 0.25, Math.min((m.innerW * 0.7) / Math.max(rect.w, 1), (m.innerH * 0.7) / Math.max(rect.h, 1))),
        );
    }
    layoutMinimapState.scale = scale;
    if (tall) {
        layoutMinimapState.panX = m.innerW * 0.18 - rect.x * scale;
        layoutMinimapState.panY = 20 - rect.y * scale;
    } else if (wide) {
        layoutMinimapState.panX = 16 - rect.x * scale;
        layoutMinimapState.panY = m.innerH * 0.16 - rect.y * scale;
    } else {
        layoutMinimapState.panX = m.innerW / 2 - (rect.x + rect.w / 2) * scale;
        layoutMinimapState.panY = m.innerH / 2 - (rect.y + rect.h / 2) * scale;
    }
    clampLayoutMinimapCamera();
    updateLayoutMinimapZoomLabel();
    drawLayoutMinimap();
}

function focusLayoutMinimapRole(role) {
    const minimap = layoutMinimapState?.minimap;
    if (!minimap || !role) return;
    const overlay = (minimap.overlays || []).find((o) => o.role === role);
    if (!overlay) return;
    const m = layoutMinimapMetrics();
    focusLayoutMinimapRect(layoutMinimapBboxRect(overlay, minimap, m.innerW, m.innerH));
}

function focusLayoutMinimapCell(row, col) {
    const minimap = layoutMinimapState?.minimap;
    if (!minimap) return;
    const m = layoutMinimapMetrics();
    const rect = layoutMinimapBboxRect({
        start_row: row, end_row: row, start_col: col, end_col: col,
    }, minimap, m.innerW, m.innerH);
    const scale = Math.min(LAYOUT_MINIMAP_MAX_SCALE, Math.max(8, 28 / Math.max(rect.w, rect.h, 1)));
    layoutMinimapState.scale = scale;
    layoutMinimapState.panX = m.innerW / 2 - (rect.x + rect.w / 2) * scale;
    layoutMinimapState.panY = m.innerH / 2 - (rect.y + rect.h / 2) * scale;
    clampLayoutMinimapCamera();
    updateLayoutMinimapZoomLabel();
    drawLayoutMinimap();
}

function hitTestLayoutOverlay(row, col, overlays) {
    const hits = (overlays || []).filter((o) => (
        row >= Number(o.start_row) && row <= Number(o.end_row)
        && col >= Number(o.start_col) && col <= Number(o.end_col)
    ));
    hits.sort((a, b) => {
        if ((a.style === 'dot') !== (b.style === 'dot')) return a.style === 'dot' ? -1 : 1;
        return layoutOverlayArea(a) - layoutOverlayArea(b);
    });
    return hits[0] || null;
}

function setLayoutMinimapActiveRole(role, hintText) {
    if (!layoutMinimapState) return;
    const turningOff = layoutMinimapState.activeRole === role;
    if (turningOff) {
        layoutMinimapState.activeRole = null;
    } else {
        layoutMinimapState.activeRole = role || null;
    }
    const hint = document.querySelector('[data-layout-minimap-hint]');
    if (hint) {
        hint.textContent = hintText || (layoutMinimapState.activeRole
            ? `Highlighting ${(LAYOUT_ROLE_META[layoutMinimapState.activeRole] || {}).label || layoutMinimapState.activeRole}`
            : '');
    }
    document.querySelectorAll('.layout-role-row, .layout-minimap-legend-item').forEach((el) => {
        el.classList.toggle('is-active', el.getAttribute('data-role') === layoutMinimapState.activeRole);
    });
    if (!turningOff && layoutMinimapState.activeRole) {
        focusLayoutMinimapRole(layoutMinimapState.activeRole);
    } else {
        drawLayoutMinimap();
    }
}

function drawLayoutMinimapAxisLabels(ctx, m) {
    const { cssW, cssH, innerW, innerH, gutterL, gutterT, rows, cols, scale, originR, originC, colPx, rowPx } = m;
    ctx.save();
    ctx.fillStyle = '#161c24';
    ctx.fillRect(0, 0, cssW, gutterT);
    ctx.fillRect(0, 0, gutterL, cssH);
    ctx.fillStyle = '#1c2430';
    ctx.fillRect(0, 0, gutterL, gutterT);
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.35)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(gutterL, 0);
    ctx.lineTo(gutterL, cssH);
    ctx.moveTo(0, gutterT);
    ctx.lineTo(cssW, gutterT);
    ctx.stroke();

    ctx.fillStyle = '#cbd5e1';
    ctx.font = '11px Inter, sans-serif';
    ctx.textBaseline = 'middle';
    const panX = layoutMinimapState.panX || 0;
    const panY = layoutMinimapState.panY || 0;
    const rowStep = rowPx >= 16 ? 1 : Math.max(1, Math.ceil(16 / Math.max(rowPx, 1)));
    const colStep = colPx >= 28 ? 1 : Math.max(1, Math.ceil(28 / Math.max(colPx, 1)));
    ctx.textAlign = 'right';
    for (let r = originR; r < originR + rows; r += rowStep) {
        const y = gutterT + panY + ((r - originR + 0.5) / rows) * innerH * scale;
        if (y < gutterT + 4 || y > cssH - 2) continue;
        ctx.fillText(String(r + 1), gutterL - 6, y);
    }
    ctx.textAlign = 'center';
    for (let c = originC; c < originC + cols; c += colStep) {
        const x = gutterL + panX + ((c - originC + 0.5) / cols) * innerW * scale;
        if (x < gutterL + 8 || x > cssW - 8) continue;
        ctx.fillText(indexToExcelColumn(c), x, gutterT / 2);
    }
    ctx.restore();
}

function layoutMinimapVisibleSheetRange(m) {
    const tl = layoutMinimapScreenToWorld(m.gutterL, m.gutterT);
    const br = layoutMinimapScreenToWorld(m.cssW, m.cssH);
    const r0 = Math.max(m.originR, Math.floor((tl.y / m.innerH) * m.rows) + m.originR);
    const r1 = Math.min(m.originR + m.rows - 1, Math.ceil((br.y / m.innerH) * m.rows) + m.originR);
    const c0 = Math.max(m.originC, Math.floor((tl.x / m.innerW) * m.cols) + m.originC);
    const c1 = Math.min(m.originC + m.cols - 1, Math.ceil((br.x / m.innerW) * m.cols) + m.originC);
    return { r0, r1, c0, c1 };
}

function ingestLayoutMinimapCells(list) {
    if (!layoutMinimapState) return;
    if (!layoutMinimapState.cells) layoutMinimapState.cells = new Map();
    (list || []).forEach((item) => {
        if (item == null || item.t == null || item.t === '') return;
        layoutMinimapState.cells.set(`${Number(item.r)}:${Number(item.c)}`, String(item.t));
    });
}

function syncLayoutMinimapLoadButton() {
    const btn = document.querySelector('[data-minimap-load-cells]');
    if (!btn || !layoutMinimapState) return;
    const status = layoutMinimapState.cellsStatus || 'idle';
    btn.disabled = status === 'loading' || status === 'all';
    if (status === 'loading') btn.textContent = 'Loading…';
    else if (status === 'all') btn.textContent = 'Cells loaded';
    else btn.textContent = 'Load all cells';
}

function layoutMinimapEnsureMergeIndex() {
    if (!layoutMinimapState) return { covered: new Set(), list: [] };
    if (layoutMinimapState.mergeIndex) return layoutMinimapState.mergeIndex;
    const list = Array.isArray(layoutMinimapState.minimap?.merges)
        ? layoutMinimapState.minimap.merges
        : [];
    const covered = new Set();
    const origin = new Map();
    list.forEach((mg) => {
        origin.set(`${Number(mg.start_row)}:${Number(mg.start_col)}`, mg);
        for (let r = Number(mg.start_row); r <= Number(mg.end_row); r += 1) {
            for (let c = Number(mg.start_col); c <= Number(mg.end_col); c += 1) {
                if (r === Number(mg.start_row) && c === Number(mg.start_col)) continue;
                covered.add(`${r}:${c}`);
            }
        }
    });
    layoutMinimapState.mergeIndex = { covered, origin, list };
    return layoutMinimapState.mergeIndex;
}

function drawLayoutMinimapMerges(ctx, m) {
    const { list } = layoutMinimapEnsureMergeIndex();
    if (!list.length) return;
    const vis = layoutMinimapVisibleSheetRange(m);
    const minimap = layoutMinimapState.minimap;
    ctx.save();
    list.forEach((mg) => {
        if (mg.end_row < vis.r0 || mg.start_row > vis.r1 || mg.end_col < vis.c0 || mg.start_col > vis.c1) return;
        const rect = layoutMinimapBboxRect(mg, minimap, m.innerW, m.innerH);
        ctx.fillStyle = 'rgba(22, 32, 46, 0.97)';
        ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
        ctx.strokeStyle = 'rgba(186, 198, 214, 0.65)';
        ctx.lineWidth = 1.35 / m.scale;
        ctx.strokeRect(
            rect.x + 0.5 / m.scale,
            rect.y + 0.5 / m.scale,
            Math.max(rect.w - 1 / m.scale, 1 / m.scale),
            Math.max(rect.h - 1 / m.scale, 1 / m.scale),
        );
    });
    ctx.restore();
}

function drawLayoutMinimapCellValues(ctx, m) {
    const cells = layoutMinimapState.cells;
    if (!cells || cells.size === 0) return;
    if (m.rowPx < 10 || m.colPx < 28) return;
    const vis = layoutMinimapVisibleSheetRange(m);
    const minimap = layoutMinimapState.minimap;
    const { covered, origin } = layoutMinimapEnsureMergeIndex();
    const fontPx = Math.min(12, Math.max(9, m.rowPx - 5));
    ctx.save();
    ctx.font = `${fontPx / m.scale}px Inter, sans-serif`;
    ctx.fillStyle = 'rgba(248, 250, 252, 0.96)';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'middle';
    for (let r = vis.r0; r <= vis.r1; r += 1) {
        for (let c = vis.c0; c <= vis.c1; c += 1) {
            const key = `${r}:${c}`;
            if (covered.has(key)) continue;
            const merge = origin.get(key);
            let text = cells.get(key);
            if (!text && merge) {
                for (let rr = Number(merge.start_row); rr <= Number(merge.end_row) && !text; rr += 1) {
                    for (let cc = Number(merge.start_col); cc <= Number(merge.end_col); cc += 1) {
                        text = cells.get(`${rr}:${cc}`);
                        if (text) break;
                    }
                }
            }
            if (!text) continue;
            const box = merge || { start_row: r, end_row: r, start_col: c, end_col: c };
            const rect = layoutMinimapBboxRect(box, minimap, m.innerW, m.innerH);
            ctx.save();
            ctx.beginPath();
            ctx.rect(rect.x + 2 / m.scale, rect.y, Math.max(rect.w - 4 / m.scale, 1 / m.scale), rect.h);
            ctx.clip();
            ctx.fillText(text, rect.x + 4 / m.scale, rect.y + rect.h / 2);
            ctx.restore();
        }
    }
    ctx.restore();
}

async function loadLayoutMinimapCells({ all = true } = {}) {
    if (!layoutMinimapState || !currentJobId) return;
    if (layoutMinimapState.cellsStatus === 'loading') return;
    if (all && layoutMinimapState.cellsStatus === 'all') return;
    const minimap = layoutMinimapState.minimap;
    const m = layoutMinimapMetrics();
    let r0;
    let r1;
    let c0;
    let c1;
    if (all) {
        r0 = Number(minimap.start_row) || 0;
        r1 = Number(minimap.end_row) || r0;
        c0 = Number(minimap.start_col) || 0;
        c1 = Number(minimap.end_col) || c0;
    } else {
        const vis = layoutMinimapVisibleSheetRange(m);
        r0 = vis.r0;
        r1 = vis.r1;
        c0 = vis.c0;
        c1 = vis.c1;
    }
    if (r1 < r0 || c1 < c0) return;
    layoutMinimapState.cellsStatus = 'loading';
    syncLayoutMinimapLoadButton();
    const req = ++layoutMinimapPreviewReq;
    let url = `/api/demarcation/preview/${currentJobId}?sparse=1&start_row=${r0}&end_row=${r1}&start_col=${c0}&end_col=${c1}`;
    if (currentSheet) url += `&sheet_name=${encodeURIComponent(currentSheet)}`;
    if (currentSourceId) url += `&source_id=${encodeURIComponent(currentSourceId)}`;
    try {
        const res = await fetch(url);
        const data = await res.json();
        if (req !== layoutMinimapPreviewReq || !layoutMinimapState) return;
        if (data.error) {
            layoutMinimapState.cellsStatus = layoutMinimapState.cells?.size ? 'partial' : 'idle';
            syncLayoutMinimapLoadButton();
            return;
        }
        ingestLayoutMinimapCells(data.cells);
        layoutMinimapState.cellsStatus = all ? 'all' : 'partial';
        syncLayoutMinimapLoadButton();
        const hint = document.querySelector('[data-layout-minimap-hint]');
        if (hint) {
            hint.textContent = all
                ? `Loaded ${layoutMinimapState.cells.size} cells — pan and scroll without reloading`
                : `Loaded ${layoutMinimapState.cells.size} cells in view`;
        }
        drawLayoutMinimap();
    } catch (err) {
        if (layoutMinimapState) {
            layoutMinimapState.cellsStatus = layoutMinimapState.cells?.size ? 'partial' : 'idle';
            syncLayoutMinimapLoadButton();
        }
    }
}

function drawLayoutMinimap() {
    if (!layoutMinimapState?.canvas || !layoutMinimapState.minimap) return;
    const canvas = layoutMinimapState.canvas;
    const minimap = layoutMinimapState.minimap;
    const m = layoutMinimapMetrics();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(m.cssW * dpr);
    canvas.height = Math.round(m.cssH * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, m.cssW, m.cssH);
    ctx.fillStyle = '#10151c';
    ctx.fillRect(0, 0, m.cssW, m.cssH);

    ctx.save();
    ctx.beginPath();
    ctx.rect(m.gutterL, m.gutterT, m.innerW, m.innerH);
    ctx.clip();
    ctx.translate(m.gutterL + (layoutMinimapState.panX || 0), m.gutterT + (layoutMinimapState.panY || 0));
    ctx.scale(m.scale, m.scale);

    const gw = Math.max(1, Number(minimap.grid_cols) || 1);
    const gh = Math.max(1, Number(minimap.grid_rows) || 1);
    const occ = String(minimap.occupancy || '');
    const cellW = m.innerW / gw;
    const cellH = m.innerH / gh;
    const gap = m.scale >= 2.5 ? 0.4 / m.scale : 0;
    if (!m.showText) {
        ctx.fillStyle = 'rgba(226, 232, 240, 0.28)';
        for (let gy = 0; gy < gh; gy += 1) {
            for (let gx = 0; gx < gw; gx += 1) {
                if (occ[gy * gw + gx] !== '1') continue;
                ctx.fillRect(gx * cellW, gy * cellH, Math.max(cellW - gap, 0.5), Math.max(cellH - gap, 0.5));
            }
        }
    } else {
        ctx.strokeStyle = 'rgba(148, 163, 184, 0.18)';
        ctx.lineWidth = 1 / m.scale;
        const vis = layoutMinimapVisibleSheetRange(m);
        for (let r = vis.r0; r <= vis.r1 + 1; r += 1) {
            const y = ((r - m.originR) / m.rows) * m.innerH;
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(m.innerW, y);
            ctx.stroke();
        }
        for (let c = vis.c0; c <= vis.c1 + 1; c += 1) {
            const x = ((c - m.originC) / m.cols) * m.innerW;
            ctx.beginPath();
            ctx.moveTo(x, 0);
            ctx.lineTo(x, m.innerH);
            ctx.stroke();
        }
    }

    drawLayoutMinimapMerges(ctx, m);

    const active = layoutMinimapState.activeRole;
    const overlays = Array.isArray(minimap.overlays) ? minimap.overlays : [];
    const bands = overlays.filter((o) => o.style !== 'dot');
    const dots = overlays.filter((o) => o.style === 'dot');
    const paintOrder = ['metadata', 'values', 'time', 'dimensions', 'metrics', 'totals'];
    bands.sort((a, b) => paintOrder.indexOf(a.role) - paintOrder.indexOf(b.role));
    bands.forEach((overlay) => {
        const rect = layoutMinimapBboxRect(overlay, minimap, m.innerW, m.innerH);
        const isActive = overlay.role === active;
        const dimOthers = Boolean(active) && !isActive;
        const base = overlay.role === 'values' ? 0.18 : 0.07;
        const activeFill = overlay.role === 'values' ? 0.28 : 0.16;
        ctx.fillStyle = hexToRgba(layoutRoleColor(overlay.role), dimOthers ? 0.03 : (isActive ? activeFill : base));
        ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
    });
    dots.forEach((overlay) => {
        const rect = layoutMinimapBboxRect(overlay, minimap, m.innerW, m.innerH);
        const isActive = !active || overlay.role === active;
        const cx = rect.x + rect.w / 2;
        const cy = rect.y + rect.h / 2;
        const radius = Math.max(3 / m.scale, Math.min(rect.w, rect.h, 6 / m.scale));
        ctx.beginPath();
        ctx.arc(cx, cy, radius, 0, Math.PI * 2);
        ctx.fillStyle = hexToRgba(layoutRoleColor('noise'), isActive ? 0.95 : 0.25);
        ctx.fill();
    });

    drawLayoutMinimapCellValues(ctx, m);

    bands.forEach((overlay) => {
        const rect = layoutMinimapBboxRect(overlay, minimap, m.innerW, m.innerH);
        const isActive = overlay.role === active;
        const dimOthers = Boolean(active) && !isActive;
        ctx.strokeStyle = hexToRgba(layoutRoleColor(overlay.role), dimOthers ? 0.2 : (isActive ? 1 : 0.7));
        ctx.lineWidth = (isActive ? 3 : 1.5) / m.scale;
        ctx.strokeRect(rect.x + 0.5 / m.scale, rect.y + 0.5 / m.scale, Math.max(rect.w - 1 / m.scale, 1 / m.scale), Math.max(rect.h - 1 / m.scale, 1 / m.scale));
    });

    const cell = layoutMinimapState.highlightCell;
    if (cell && cell.row != null && cell.col != null) {
        const rect = layoutMinimapBboxRect({
            start_row: cell.row, end_row: cell.row, start_col: cell.col, end_col: cell.col,
        }, minimap, m.innerW, m.innerH);
        ctx.strokeStyle = '#fde68a';
        ctx.lineWidth = 2 / m.scale;
        ctx.strokeRect(rect.x, rect.y, Math.max(rect.w, 4 / m.scale), Math.max(rect.h, 4 / m.scale));
    }
    ctx.restore();
    drawLayoutMinimapAxisLabels(ctx, m);
}

function initLayoutMinimap(report) {
    if (layoutMinimapResizeObserver) {
        layoutMinimapResizeObserver.disconnect();
        layoutMinimapResizeObserver = null;
    }
    layoutMinimapState = null;
    const canvas = document.querySelector('.layout-minimap');
    const minimap = report?.minimap;
    if (!canvas || !minimap?.occupancy) {
        document.body.classList.remove('layout-map-expanded');
        return;
    }
    layoutMinimapState = {
        canvas,
        minimap,
        report,
        activeRole: null,
        highlightCell: null,
        scale: 1,
        homeScale: 0,
        panX: 0,
        panY: 0,
        cells: new Map(),
        cellsStatus: 'idle',
        mergeIndex: null,
        expanded: layoutMinimapWantExpanded,
    };
    if (layoutMinimapWantExpanded) {
        const panel = document.querySelector('.layout-complexity-panel');
        panel?.classList.add('is-expanded');
        document.body.classList.add('layout-map-expanded');
        syncLayoutMinimapExpandButtons();
    }
    updateLayoutMinimapZoomLabel();
    layoutMinimapResizeObserver = new ResizeObserver(() => {
        clampLayoutMinimapCamera();
        drawLayoutMinimap();
    });
    layoutMinimapResizeObserver.observe(canvas);
    requestAnimationFrame(() => {
        requestAnimationFrame(() => {
            readableLayoutMinimapView();
            const area = (Number(minimap.rows) || 0) * (Number(minimap.cols) || 0);
            if (area > 0 && area <= 25000) {
                void loadLayoutMinimapCells({ all: true });
            } else {
                syncLayoutMinimapLoadButton();
            }
        });
    });

    canvas.addEventListener('wheel', (event) => {
        event.preventDefault();
        if (event.ctrlKey || event.metaKey) {
            const rect = canvas.getBoundingClientRect();
            const factor = event.deltaY < 0 ? 1.18 : 1 / 1.18;
            zoomLayoutMinimapAt(event.clientX - rect.left, event.clientY - rect.top, factor);
            return;
        }
        const dx = event.deltaX || (event.shiftKey ? event.deltaY : 0);
        const dy = event.shiftKey ? 0 : event.deltaY;
        layoutMinimapState.panX -= dx;
        layoutMinimapState.panY -= dy;
        clampLayoutMinimapCamera();
        drawLayoutMinimap();
    }, { passive: false });

    canvas.addEventListener('pointerdown', (event) => {
        if (event.button !== 0) return;
        canvas.setPointerCapture(event.pointerId);
        layoutMinimapState.pointer = {
            id: event.pointerId,
            lastX: event.clientX,
            lastY: event.clientY,
            startX: event.clientX,
            startY: event.clientY,
            moved: false,
        };
        canvas.classList.add('is-panning');
    });
    canvas.addEventListener('pointermove', (event) => {
        const pointer = layoutMinimapState?.pointer;
        if (!pointer || pointer.id !== event.pointerId) return;
        const dx = event.clientX - pointer.lastX;
        const dy = event.clientY - pointer.lastY;
        if (Math.abs(event.clientX - pointer.startX) + Math.abs(event.clientY - pointer.startY) > 5) {
            pointer.moved = true;
        }
        if (!pointer.moved) return;
        layoutMinimapState.panX += dx;
        layoutMinimapState.panY += dy;
        pointer.lastX = event.clientX;
        pointer.lastY = event.clientY;
        clampLayoutMinimapCamera();
        drawLayoutMinimap();
    });
    const endPan = (event) => {
        const pointer = layoutMinimapState?.pointer;
        if (!pointer || pointer.id !== event.pointerId) return;
        canvas.classList.remove('is-panning');
        try { canvas.releasePointerCapture(event.pointerId); } catch (err) { /* already released */ }
        layoutMinimapState.pointer = pointer.moved ? { ...pointer, id: null } : null;
    };
    canvas.addEventListener('pointerup', endPan);
    canvas.addEventListener('pointercancel', endPan);

    canvas.addEventListener('click', (event) => {
        const leftover = layoutMinimapState.pointer;
        if (leftover?.moved) {
            layoutMinimapState.pointer = null;
            return;
        }
        layoutMinimapState.pointer = null;
        const { row, col } = layoutMinimapCellFromEvent(event, canvas, minimap);
        const hit = hitTestLayoutOverlay(row, col, minimap.overlays || []);
        const range = hit?.excel_range ? ` (${hit.excel_range})` : '';
        const label = hit ? `${(LAYOUT_ROLE_META[hit.role] || {}).label || hit.role}${range}` : '';
        layoutMinimapState.highlightCell = { row, col };
        if (hit) {
            layoutMinimapState.activeRole = hit.role;
            const hint = document.querySelector('[data-layout-minimap-hint]');
            if (hint) hint.textContent = label;
            document.querySelectorAll('.layout-role-row, .layout-minimap-legend-item').forEach((el) => {
                el.classList.toggle('is-active', el.getAttribute('data-role') === hit.role);
            });
            drawLayoutMinimap();
        } else {
            setLayoutMinimapActiveRole(null, '');
            drawLayoutMinimap();
        }
    });

    canvas.addEventListener('dblclick', (event) => {
        event.preventDefault();
        const rect = canvas.getBoundingClientRect();
        if ((layoutMinimapState.scale || 1) / layoutMinimapHomeScale() >= 1.35) {
            readableLayoutMinimapView();
            return;
        }
        zoomLayoutMinimapAt(event.clientX - rect.left, event.clientY - rect.top, 2.4);
    });

    canvas.addEventListener('keydown', (event) => {
        const step = 40;
        if (event.key === '+' || event.key === '=') {
            event.preventDefault();
            const { cssW, cssH } = layoutMinimapViewSize();
            zoomLayoutMinimapAt(cssW / 2, cssH / 2, 1.25);
        } else if (event.key === '-' || event.key === '_') {
            event.preventDefault();
            const { cssW, cssH } = layoutMinimapViewSize();
            zoomLayoutMinimapAt(cssW / 2, cssH / 2, 1 / 1.25);
        } else if (event.key === 'Home') {
            event.preventDefault();
            readableLayoutMinimapView();
        } else if (event.key === '0') {
            event.preventDefault();
            resetLayoutMinimapView();
        } else if (event.key === 'ArrowLeft') {
            event.preventDefault();
            layoutMinimapState.panX += step;
            clampLayoutMinimapCamera();
            drawLayoutMinimap();
        } else if (event.key === 'ArrowRight') {
            event.preventDefault();
            layoutMinimapState.panX -= step;
            clampLayoutMinimapCamera();
            drawLayoutMinimap();
        } else if (event.key === 'ArrowUp') {
            event.preventDefault();
            layoutMinimapState.panY += step;
            clampLayoutMinimapCamera();
            drawLayoutMinimap();
        } else if (event.key === 'ArrowDown') {
            event.preventDefault();
            layoutMinimapState.panY -= step;
            clampLayoutMinimapCamera();
            drawLayoutMinimap();
        }
    });

    document.querySelectorAll('[data-minimap-zoom]').forEach((btn) => {
        btn.addEventListener('click', () => {
            const action = btn.getAttribute('data-minimap-zoom');
            const { cssW, cssH } = layoutMinimapViewSize();
            if (action === 'in') zoomLayoutMinimapAt(cssW / 2, cssH / 2, 1.4);
            else if (action === 'out') zoomLayoutMinimapAt(cssW / 2, cssH / 2, 1 / 1.4);
            else if (action === 'home') readableLayoutMinimapView();
            else if (action === 'fit') resetLayoutMinimapView();
        });
    });

    document.querySelectorAll('[data-minimap-load-cells]').forEach((btn) => {
        btn.addEventListener('click', () => {
            void loadLayoutMinimapCells({ all: true });
        });
    });

    document.querySelectorAll('[data-minimap-expand]').forEach((btn) => {
        btn.addEventListener('click', () => {
            setLayoutMinimapExpanded(!layoutMinimapWantExpanded);
        });
    });
    document.removeEventListener('keydown', onLayoutMapExpandKeydown);
    document.addEventListener('keydown', onLayoutMapExpandKeydown);

    document.querySelectorAll('.layout-role-row, .layout-minimap-legend-item').forEach((el) => {
        el.addEventListener('click', () => {
            const role = el.getAttribute('data-role');
            const overlay = (minimap.overlays || []).find((o) => o.role === role);
            const hint = overlay?.excel_range ? `${(LAYOUT_ROLE_META[role] || {}).label || role} (${overlay.excel_range})` : '';
            setLayoutMinimapActiveRole(role, hint);
        });
    });

    document.querySelectorAll('.layout-sample-item').forEach((el) => {
        const activate = () => {
            const row = el.getAttribute('data-row');
            const col = el.getAttribute('data-col');
            if (row === '' || col === '') return;
            layoutMinimapState.highlightCell = { row: Number(row), col: Number(col) };
            layoutMinimapState.activeRole = 'values';
            document.querySelectorAll('.layout-role-row, .layout-minimap-legend-item').forEach((item) => {
                item.classList.toggle('is-active', item.getAttribute('data-role') === 'values');
            });
            document.querySelectorAll('.layout-sample-item').forEach((item) => item.classList.toggle('is-active', item === el));
            const hint = document.querySelector('[data-layout-minimap-hint]');
            if (hint) {
                const a1 = `${indexToExcelColumn(Number(col))}${Number(row) + 1}`;
                hint.textContent = `Sample cell ${a1}`;
            }
            focusLayoutMinimapCell(Number(row), Number(col));
        };
        el.addEventListener('click', activate);
        el.addEventListener('keydown', (event) => {
            if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                activate();
            }
        });
    });
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
                    <span class="block-type-label">Block Type: ${escapeHtml(effectiveBlockTypeLabel(block))}</span>
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

    const adapterUsed = Boolean(demarcationProposal?.layout_complexity?.adapter_used);
    const regionsHtml = adapterUsed
        ? `<details class="layout-compat-regions">
            <summary>Regions Confirm &amp; map will save (${blocks.length}) — mapping compatibility, not island classification</summary>
            ${cardsHtml}
           </details>`
        : cardsHtml;

    elements.demarcationWorkspace.innerHTML =
        buildLayoutComplexityPanel(demarcationProposal?.layout_complexity) + legendFooter + regionsHtml;
    initLayoutMinimap(demarcationProposal?.layout_complexity);
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
            layout_complexity: demarcationProposal?.layout_complexity || null,
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
        switchStep(SETUP_STEP_LAYOUT);
        await changeSource(next.source_id, next.sheet_name || '');
        return true;
    }
    switchStep(SETUP_STEP_LAYOUT);
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
        if (currentSheetNeedsStandardize()) {
            switchStep(SETUP_STEP_STANDARDIZE);
            showToast('Layout saved — assign Date / Dimensions / Metrics, then generate the standard table.', 'success');
        } else {
            switchStep(SETUP_STEP_COLUMN_STD);
            showToast('Layout saved — standardize columns (split/combine), then map.', 'success');
        }
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
    if (setupUiStep === SETUP_STEP_MAPPING && computeSheetsGuidedSetupDone(jobPeek)) {
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
            switchStep(SETUP_STEP_LAYOUT);
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
        if (currentSheetNeedsStandardize()) {
            switchStep(SETUP_STEP_STANDARDIZE);
        } else {
            switchStep(SETUP_STEP_COLUMN_STD);
        }
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
 * Primary = Config enterprise + media hierarchy + date + spends/impressions/clicks.
 */
function inferRoleFromSemantics(col) {
    if (!col) return 'supporting';
    const cls = String(col.classification || '').toLowerCase();
    const dec = String(col.decision || '').toLowerCase();
    const ctype = String(col.column_type || '').toLowerCase();
    const target = String(col.target_column || '').trim();
    if (target && target !== 'No match' && primaryTargetColumns.has(target)) {
        return 'primary';
    }
    const isMetricLike =
        ctype === 'metric' ||
        cls.includes('delivery') ||
        cls.includes('cost') ||
        cls.includes('engagement') ||
        cls.includes('metric');
    if (dec === 'discard' || ctype === 'blank' || cls.includes('blank') || cls.includes('derived')) {
        return 'exclude';
    }
    if (isMetricLike) {
        return 'exclude';
    }
    return 'supporting';
}

/** 1-based source index for Supporting Meta suffixes (_1, _2, …). */
function currentSourceSuffixIndex() {
    const list = Array.isArray(setupSourceRegistryOrder) ? setupSourceRegistryOrder : [];
    const sid = String(currentSourceId || '');
    const idx = list.findIndex((s) => String(s?.source_id || '') === sid);
    return idx >= 0 ? idx + 1 : 1;
}

function applyOutputAliasForRole(col) {
    if (!col) return;
    const target = String(col.target_column || '').trim();
    const role = String(col.role || 'supporting').trim().toLowerCase();
    if (!target || target.toLowerCase() === 'no match' || role === 'exclude') {
        col.output_alias = '';
        return;
    }
    if (role === 'primary') {
        col.output_alias = target;
    } else if (role === 'supporting') {
        col.output_alias = `${target}_${currentSourceSuffixIndex()}`;
    } else {
        col.output_alias = '';
    }
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
        col.output_alias = '';
        return;
    }
    if (role === 'primary') {
        col.decision = 'Keep';
        applyOutputAliasForRole(col);
        return;
    }
    col.decision = 'Metadata';
    applyOutputAliasForRole(col);
}

function applyDecisionRule(col) {
    if (!col) return;
    if (col.role) {
        applyRoleRule(col);
        return;
    }
    col.decision = inferDecisionFromTarget(col);
}

function compactMappingKey(value) {
    return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
}

function dateRoleFromVisibleWords(name) {
    const tokens = String(name || '')
        .toLowerCase()
        .split(/[\s_\-/|>.,;:()[\]{}]+/)
        .filter(Boolean);
    if (tokens.some((t) => t === 'start' || t === 'from' || t === 'begin')) return 'range_start';
    if (tokens.some((t) => t === 'end' || t === 'to' || t === 'until' || t === 'finish')) return 'range_end';
    if (tokens.includes('year')) return 'part_year';
    if (tokens.includes('month')) return 'part_month';
    if (tokens.includes('day')) return 'part_day';
    if (tokens.includes('quarter') || tokens.some((t) => /^q[1-4]$/.test(t))) return 'part_quarter';
    return '';
}

/** Config Date aliases whose wording already carries a role. Whole name only. */
const DATE_ROLE_WHOLE_ALIASES = [
    'interval start', 'interval end',
    'start date', 'end date',
    'flight start', 'flight end',
    'order start date', 'order end date',
];

function dateRoleFromWholeAlias(name) {
    const key = compactMappingKey(name);
    if (!key) return '';
    for (const alias of DATE_ROLE_WHOLE_ALIASES) {
        if (compactMappingKey(alias) === key) {
            return dateRoleFromVisibleWords(alias);
        }
    }
    return '';
}

function inferDateSemanticFromColumn(col) {
    if (!col || typeof col !== 'object') return '';
    const raw = String(col.column_name || '').trim();
    const name = raw.toLowerCase();
    const target = String(col.target_column || '').trim().toLowerCase();
    const ctype = String(col.column_type || '').trim().toLowerCase();
    if (!(ctype.includes('date') || isDateLikeTargetName(target) || isLikelyDateColumnName(name))) {
        return '';
    }
    // Whole-name alias first: intervalStart == "interval start". Do not split camelCase.
    const fromAlias = dateRoleFromWholeAlias(raw);
    if (fromAlias) return fromAlias;
    const fromVisible = dateRoleFromVisibleWords(raw);
    if (fromVisible) return fromVisible;
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
        if (!col.role) {
            col.role = inferRoleFromSemantics(col);
        }
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

    // Synonym / column-shaping matching only — no LLM required for Guided Setup mapping.

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
        live.textContent = 'Matching columns by synonyms and column-shaping names…';
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
        targetColumnMeta = new Map();
        const opts = Array.isArray(data.target_column_options) ? data.target_column_options : [];
        for (const o of opts) {
            if (!o || !o.id) continue;
            targetColumnMeta.set(String(o.id), {
                id: String(o.id),
                name: String(o.name || o.id),
                kind: String(o.kind || 'dimension'),
                supports_currency: Boolean(o.supports_currency) || String(o.id) === 'spends' || String(o.id) === 'spend',
            });
        }
        for (const id of data.target_columns || []) {
            if (!id || targetColumnMeta.has(id)) continue;
            const isMetric = primaryTargetColumns.has(id) && /spend|impression|click|roas|view|metric/i.test(id);
            targetColumnMeta.set(id, {
                id,
                name: id.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
                kind: isMetric ? 'metric' : 'dimension',
                supports_currency: id === 'spends' || id === 'spend',
            });
        }
        // Ensure known metrics are marked even if options omitted
        for (const mid of ['spends', 'spend', 'impressions', 'clicks', 'video_views', 'roas']) {
            if (!targetColumnMeta.has(mid) && targetColumnOptions.includes(mid)) {
                targetColumnMeta.set(mid, {
                    id: mid,
                    name: mid === 'spends' || mid === 'spend' ? 'Spend'
                        : mid === 'roas' ? 'ROAS (Campaign KPI)'
                        : mid.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()),
                    kind: 'metric',
                    supports_currency: mid === 'spends' || mid === 'spend',
                });
            } else if (targetColumnMeta.has(mid)) {
                const m = targetColumnMeta.get(mid);
                m.kind = 'metric';
                if (mid === 'spends' || mid === 'spend') m.supports_currency = true;
            }
        }
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

function targetOptionLabel(id) {
    if (!id || id === 'No match') return 'No match';
    const meta = targetColumnMeta.get(id);
    if (meta && meta.name) return meta.name;
    return String(id).replace(/_/g, ' ');
}

function isMetricTarget(id) {
    if (!id || id === 'No match') return false;
    const meta = targetColumnMeta.get(id);
    if (meta && meta.kind === 'metric') return true;
    return /^(spends?|impressions|clicks|video_views|roas)$/i.test(String(id));
}

function metricSupportsCurrency(id) {
    if (!id) return false;
    const meta = targetColumnMeta.get(id);
    if (meta && meta.supports_currency) return true;
    return /^(spends?|spend)$/i.test(String(id));
}

function valueScaleNoteFor(scale) {
    const s = Number(scale) || 1;
    if (s === 1000) return "values in thousands ('000)";
    if (s === 1000000) return 'values in millions';
    return '';
}

/** Target-column <select> with Metrics / Dimensions groups + add custom metric. */
function buildTargetColumnOptionsHtml(selectedTarget) {
    const current = selectedTarget || 'No match';
    const ids = Array.isArray(targetColumnOptions) && targetColumnOptions.length > 0
        ? targetColumnOptions.filter((o) => o && o !== 'No match')
        : [];
    const metrics = [];
    const dims = [];
    for (const id of ids) {
        if (isMetricTarget(id)) metrics.push(id);
        else dims.push(id);
    }
    metrics.sort((a, b) => targetOptionLabel(a).localeCompare(targetOptionLabel(b), undefined, { sensitivity: 'base' }));
    dims.sort((a, b) => targetOptionLabel(a).localeCompare(targetOptionLabel(b), undefined, { sensitivity: 'base' }));

    const opt = (id) => {
        const sel = id === current ? ' selected' : '';
        return `<option value="${escapeAttr(id)}"${sel}>${escapeHtml(targetOptionLabel(id))}</option>`;
    };
    let html = `<option value="No match"${current === 'No match' ? ' selected' : ''}>No match</option>`;
    if (metrics.length) {
        html += `<optgroup label="Metrics">${metrics.map(opt).join('')}</optgroup>`;
    }
    if (dims.length) {
        html += `<optgroup label="Dimensions">${dims.map(opt).join('')}</optgroup>`;
    }
    html += `<optgroup label="Custom"><option value="${ADD_CUSTOM_METRIC}">+ Add additional metric…</option></optgroup>`;
    if (current && current !== 'No match' && current !== ADD_CUSTOM_METRIC && !ids.includes(current)) {
        html += `<option value="${escapeAttr(current)}" selected>${escapeHtml(targetOptionLabel(current))}</option>`;
    }
    return html;
}

function buildMetricMetaSelectHtml(columnName, col, blockId) {
    if (!col || !isMetricTarget(col.target_column)) return '';
    const target = String(col.target_column || '');
    const safeColumnName = JSON.stringify(columnName || '');
    const safeBlockId = JSON.stringify(blockId || '');
    let scale = '1';
    try {
        const n = Number(col.value_scale);
        if (n === 1000 || n === 1000000) scale = String(n);
        else if (n && n !== 1) scale = String(n);
    } catch (e) { /* ignore */ }
    const scaleOpts = VALUE_SCALE_CHOICES.map((c) => (
        `<option value="${escapeAttr(c.value)}" ${String(c.value) === scale ? 'selected' : ''}>${escapeHtml(c.label)}</option>`
    )).join('');
    let currencyHtml = '';
    if (metricSupportsCurrency(target)) {
        const cur = String(col.metric_currency || '');
        const curOpts = CURRENCY_CHOICES.map((c) => (
            `<option value="${escapeAttr(c.value)}" ${c.value === cur ? 'selected' : ''}>${escapeHtml(c.label)}</option>`
        )).join('');
        currencyHtml = `
            <div class="mapping-header-target mapping-header-target--bare">
                <select class="mapping-target-select mapping-target-select--metric-currency sia-select sia-select--compact"
                    aria-label="Currency for ${escapeAttr(columnName)}"
                    onchange='updateCardMetricCurrency(${safeColumnName}, this.value, ${safeBlockId})'>
                    ${curOpts}
                </select>
            </div>`;
    }
    return `
        <div class="mapping-header-target mapping-header-target--bare">
            <select class="mapping-target-select mapping-target-select--metric-scale sia-select sia-select--compact"
                aria-label="Value scale for ${escapeAttr(columnName)}"
                onchange='updateCardValueScale(${safeColumnName}, this.value, ${safeBlockId})'>
                ${scaleOpts}
            </select>
        </div>
        ${currencyHtml}`;
}

window.updateCardValueScale = function (columnName, value, blockId) {
    if (!columnName) return;
    const col = findMappingColumn(columnName, blockId);
    if (!col) return;
    const scale = Number(value) || 1;
    col.value_scale = scale;
    col.value_scale_note = valueScaleNoteFor(scale);
    scheduleMappingDraftSave();
    renderMappingCards();
};

window.updateCardMetricCurrency = function (columnName, value, blockId) {
    if (!columnName) return;
    const col = findMappingColumn(columnName, blockId);
    if (!col) return;
    col.metric_currency = String(value || '').trim();
    scheduleMappingDraftSave();
    renderMappingCards();
};

async function promptAndAddCustomMetric(columnName, blockId) {
    const name = window.prompt('Name the additional metric (e.g. Conversions, Viewability):');
    if (!name || !String(name).trim()) {
        renderMappingCards();
        return;
    }
    try {
        const res = await fetch(`/api/mapping/custom-metric/${encodeURIComponent(currentJobId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: String(name).trim() }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) throw new Error(data.error || `Could not add metric (${res.status})`);
        const metric = data.metric || {};
        const mid = metric.id;
        if (Array.isArray(data.target_columns)) {
            targetColumnOptions = ['No match', ...data.target_columns];
        } else if (mid && !targetColumnOptions.includes(mid)) {
            targetColumnOptions.push(mid);
        }
        if (Array.isArray(data.target_column_options)) {
            for (const o of data.target_column_options) {
                if (o && o.id) {
                    targetColumnMeta.set(String(o.id), {
                        id: String(o.id),
                        name: String(o.name || o.id),
                        kind: String(o.kind || 'metric'),
                        supports_currency: Boolean(o.supports_currency),
                    });
                }
            }
        } else if (mid) {
            targetColumnMeta.set(mid, {
                id: mid,
                name: metric.name || mid,
                kind: 'metric',
                supports_currency: false,
            });
        }
        if (mid) primaryTargetColumns.add(mid);
        const col = findMappingColumn(columnName, blockId);
        if (col && mid) {
            col.target_column = mid;
            col.target_match_method = 'manual';
            col.target_match_confidence = 1.0;
            col.role = 'primary';
            col.decision = 'Keep';
            if (col.value_scale == null) col.value_scale = 1;
        }
        scheduleMappingDraftSave();
        renderMappingCards();
        showToast(`Added metric “${metric.name || mid}”.`, 'success');
    } catch (e) {
        showToast(e.message || 'Failed to add metric', 'error');
        renderMappingCards();
    }
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
    if (newTarget === ADD_CUSTOM_METRIC) {
        void promptAndAddCustomMetric(columnName, blockId);
        return;
    }
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
    if (!isMetricTarget(target)) {
        col.metric_currency = '';
    } else {
        if (col.value_scale == null || col.value_scale === '' || Number(col.value_scale) === 0) {
            col.value_scale = 1;
        }
        if (!metricSupportsCurrency(target)) {
            col.metric_currency = '';
        }
    }
    if (target !== 'No match') {
        col.role = primaryTargetColumns.has(target) ? 'primary' : 'supporting';
        col.decision = col.role === 'primary' ? 'Keep' : 'Metadata';
        applyOutputAliasForRole(col);
    } else {
        if (col.role !== 'exclude') {
            col.role = inferRoleFromSemantics(col);
        }
        applyRoleRule(col);
    }
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
    const inlineMetricControls = buildMetricMetaSelectHtml(columnName, col, blockId);

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
    const splitFrom = String(col.split_from || col.packed_source_column || '').trim();
    const outputAlias = String(col.output_alias || '').trim();
    const safeColumnName = JSON.stringify(columnName);
    const safeBlockId = JSON.stringify(blockId || '');
    return `
        <div class="mapping-card mapping-card--by-source${isExcluded ? ' mapping-card--excluded' : ''}${reusedPeer ? ' mapping-card--reused-peer' : ''}" data-source-col="${escapeAttr(columnName)}" data-block-id="${escapeAttr(blockId || '')}" data-role="${escapeAttr(role)}">
            <div class="mapping-card-header mapping-card-header--by-source">
                <div class="mapping-header-left">
                    <span class="mapping-source-col-label" title="Source column from this block">${columnName ? escapeHtml(columnName) : '<em class="text-muted">Unnamed column</em>'}</span>
                    ${splitFrom ? `<span class="hierarchy-pill muted" title="Split from packed column">from ${escapeHtml(splitFrom)}</span>` : ''}
                    <span class="col-type-badge ${badgeClass}">${escapeHtml(colType)}</span>
                    ${outputAlias && !isExcluded ? `<span class="hierarchy-pill ok" title="Output name when collating sources">→ ${escapeHtml(outputAlias)}</span>` : ''}
                </div>
                <div class="mapping-header-dropdown-row">
                    ${inlineDateControls}
                    ${inlineMetricControls}
                    <div class="mapping-header-target mapping-header-target--bare">
                        <select class="mapping-target-select mapping-target-select--target-field sia-select sia-select--compact" aria-label="Target column for ${escapeAttr(columnName)}"
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
    const standardizedBanner =
        mappingHeaderDerivation && mappingHeaderDerivation.format === 'standardized_table'
            ? `<div class="mapping-reuse-banner" role="status"><strong>Standard table</strong> — these columns are Date, dimensions, and metrics from the tidy-up step, not the original messy grid.</div>`
            : '';

    const restoreWindowScrollY = setupUiStep === SETUP_STEP_MAPPING ? window.scrollY : null;

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
        scopeIntro + standardizedBanner + reuseBanner + duplicateTargetsBanner + dateRangeSemanticHintBanner + cardsHtml;

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

// ===== Step 1.5: Standardize messy layout =====

function collectStandardizeAssignment() {
    const base = (standardizeState && standardizeState.assignment) ? { ...standardizeState.assignment } : {};
    const names = [];
    document.querySelectorAll('[data-std-dim-name]').forEach((input) => names.push(input.value.trim()));
    if (names.length) base.dimension_names = names;
    const include = { ...(base.include_roles || {}) };
    document.querySelectorAll('[data-std-include]').forEach((input) => {
        include[input.getAttribute('data-std-include')] = input.checked;
    });
    base.include_roles = include;
    const excludeTotals = document.getElementById('stdExcludeTotals');
    const excludeNoise = document.getElementById('stdExcludeNoise');
    if (excludeTotals) base.exclude_totals = excludeTotals.checked;
    if (excludeNoise) base.exclude_noise = excludeNoise.checked;
    return base;
}

function standardizeRoleColor(key) {
    return (LAYOUT_ROLE_META[key] || {}).color || '#64748b';
}

function renderStandardizeWorkspace(payload) {
    const host = elements.standardizeWorkspace;
    const statusEl = document.getElementById('standardizeStatus');
    if (!host) return;
    if (!payload || payload.error) {
        if (statusEl) {
            statusEl.textContent = 'Failed';
            statusEl.className = 'status-badge danger';
        }
        host.innerHTML = `<p class="standardize-error">${escapeHtml(payload?.error || 'Could not build a standard table.')}</p>`;
        return;
    }
    if (statusEl) {
        statusEl.textContent = payload.ok ? (payload.applied ? 'Saved' : 'Preview ready') : 'Needs review';
        statusEl.className = payload.ok ? 'status-badge success' : 'status-badge warning';
    }
    const score = payload.complexity_score != null ? Number(payload.complexity_score) : null;
    const cls = String(payload.classification || '');
    const reasons = Array.isArray(payload.reasons) ? payload.reasons : [];
    const roles = Array.isArray(payload.roles) ? payload.roles : [];
    const assignment = payload.assignment || {};
    const preview = payload.preview || {};
    const columnRoles = payload.column_roles || preview.column_roles || {};
    const columns = Array.isArray(preview.columns) ? preview.columns : [];
    const rows = Array.isArray(preview.rows) ? preview.rows : [];

    const roleCards = roles.map((role) => {
        const key = role.key;
        const color = standardizeRoleColor(key);
        const editable = key === 'dimensions' && Array.isArray(role.names) && role.names.length;
        const excludeCard = key === 'noise' || key === 'totals';
        const namesHtml = editable
            ? `<div class="standardize-dim-names">${role.names.map((name, i) => `
                <label>Dimension ${i + 1} column name
                    <input type="text" data-std-dim-name="${i}" value="${escapeAttr(name)}" />
                </label>`).join('')}</div>`
            : '';
        const includeHtml = excludeCard
            ? ''
            : `<label class="inline-checkbox"><input type="checkbox" data-std-include="${escapeAttr(key)}" ${role.included ? 'checked' : ''} /> Include in standard table</label>`;
        return `<article class="standardize-role-card" style="--role-color:${color}">
            <h4>${escapeHtml(role.label || key)} <span class="role-kind">${escapeHtml(String(role.kind || ''))}</span></h4>
            <p>${escapeHtml(String(role.detail || ''))}</p>
            ${includeHtml}
            ${namesHtml}
        </article>`;
    }).join('');

    const head = columns.map((col) => {
        const role = columnRoles[col] || '';
        const color = role === 'date' ? LAYOUT_ROLE_META.time.color
            : role === 'dimension' ? LAYOUT_ROLE_META.dimensions.color
            : LAYOUT_ROLE_META.metrics.color;
        return `<th><span>${escapeHtml(col)}</span><span class="standardize-col-role" style="--role-color:${color}">${escapeHtml(role || 'metric')}</span></th>`;
    }).join('');
    const body = rows.map((row) => (
        `<tr>${columns.map((col) => `<td title="${escapeAttr(String(row[col] ?? ''))}">${escapeHtml(String(row[col] ?? ''))}</td>`).join('')}</tr>`
    )).join('');

    host.innerHTML = `
        <section class="standardize-intro">
            <h3>Convert this messy sheet into a standard table</h3>
            <p>
                ${score != null ? `Complexity score ${escapeHtml(String(score))} · ` : ''}
                Band: ${escapeHtml(cls || 'n/a')}
                ${payload.sheet_type ? ` · ${escapeHtml(String(payload.sheet_type).replace(/_/g, ' '))}` : ''}.
                Confirm Date / period, Dimensions, Metrics, and Values. Comments, noise, and totals stay out of the table.
                The result has <strong>Date</strong>, dimension, and metric <strong>column headers</strong>, with one row per period × dimension combination — then column mapping uses this table.
            </p>
            ${reasons.length ? `<ul class="layout-complexity-reasons">${reasons.map((r) => `<li>${escapeHtml(String(r))}</li>`).join('')}</ul>` : ''}
        </section>
        <div class="standardize-role-grid">${roleCards}</div>
        <div class="standardize-toggles">
            <label><input type="checkbox" id="stdExcludeTotals" ${assignment.exclude_totals !== false ? 'checked' : ''} /> Exclude totals</label>
            <label><input type="checkbox" id="stdExcludeNoise" ${assignment.exclude_noise !== false ? 'checked' : ''} /> Exclude comments / noise</label>
        </div>
        <section class="standardize-preview-wrap">
            <h4>Standard table preview</h4>
            <p class="standardize-preview-meta">${escapeHtml(payload.message || '')}${preview.row_count != null ? ` · ${preview.row_count} row(s)` : ''}</p>
            ${columns.length ? `
            <div class="table-container">
                <table class="standardize-preview-table">
                    <thead><tr>${head}</tr></thead>
                    <tbody>${body || '<tr><td colspan="' + columns.length + '">No preview rows</td></tr>'}</tbody>
                </table>
            </div>` : `<p class="standardize-error">No standard columns yet. Adjust roles and click Generate table.</p>`}
        </section>`;
}

async function loadStandardize(force = false) {
    const host = elements.standardizeWorkspace;
    const statusEl = document.getElementById('standardizeStatus');
    if (host) {
        host.innerHTML = `<div class="empty-state"><span class="spinner"></span><p>Building a standard Date / Dimensions / Metrics table…</p></div>`;
    }
    if (statusEl) {
        statusEl.textContent = 'Generating…';
        statusEl.className = 'status-badge scanning';
    }
    try {
        if (!currentJobId) throw new Error('No active job.');
        let payload;
        if (force) {
            const r = await fetch(`/api/layout/standardize/${currentJobId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    source_id: currentSourceId,
                    sheet_name: currentSheet,
                    assignment: collectStandardizeAssignment(),
                    apply: false,
                }),
            });
            payload = await r.json();
            if (!r.ok || payload.error) throw new Error(payload.error || `Standardize failed (${r.status})`);
        } else {
            const qs = new URLSearchParams();
            if (currentSheet) qs.set('sheet_name', currentSheet);
            if (currentSourceId) qs.set('source_id', currentSourceId);
            const r = await fetch(`/api/layout/standardize/preview/${currentJobId}?${qs}`, { cache: 'no-store' });
            payload = await r.json();
            if (!r.ok || payload.error) throw new Error(payload.error || `Standardize preview failed (${r.status})`);
        }
        standardizeState = payload;
        renderStandardizeWorkspace(payload);
    } catch (e) {
        standardizeState = { error: e.message };
        renderStandardizeWorkspace(standardizeState);
        showToast(e.message, 'error');
    }
}

async function confirmStandardizeAndOpenMapping() {
    const btn = elements.confirmStandardizeMapBtn;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Saving…';
    }
    try {
        if (!currentJobId) throw new Error('No active job.');
        const r = await fetch(`/api/layout/standardize/${currentJobId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                source_id: currentSourceId,
                sheet_name: currentSheet,
                assignment: collectStandardizeAssignment(),
                apply: true,
            }),
        });
        const payload = await r.json();
        if (!r.ok || payload.error || !payload.ok) {
            throw new Error(payload.error || payload.message || `Could not save standard table (${r.status})`);
        }
        standardizeState = payload;
        renderStandardizeWorkspace(payload);
        switchStep(SETUP_STEP_COLUMN_STD);
        showToast('Standard table saved — split/combine columns, then map attributes.', 'success');
    } catch (e) {
        showToast(e.message, 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Use table & continue →';
        }
    }
}

function attrTargetOptions(selected, targets, { includeCustom = false } = {}) {
    const normalized = (targets || []).map((t) => {
        if (typeof t === 'string') return { id: t, name: t };
        return { id: t.id || '', name: t.name || t.id || '' };
    }).filter((t) => t.id);
    normalized.sort((a, b) => String(a.name).localeCompare(String(b.name), undefined, { sensitivity: 'base' }));
    const opts = [`<option value="">— attribute —</option>`];
    for (const t of normalized) {
        opts.push(`<option value="${escapeAttr(t.id)}" ${t.id === selected ? 'selected' : ''}>${escapeHtml(t.name)}</option>`);
    }
    if (includeCustom) {
        const customSel = selected === COLSTD_CUSTOM ? 'selected' : '';
        opts.push(`<option value="${COLSTD_CUSTOM}" ${customSel}>Name a new dimension…</option>`);
    }
    return opts.join('');
}

/** Ensure each split has a `parts[]` model for the Column shaping UI. */
function normalizeColumnStdSplit(cf) {
    if (!cf || typeof cf !== 'object') return cf;
    const dims = Array.isArray(cf.target_dimensions) ? [...cf.target_dimensions] : [];
    const partSamples = Array.isArray(cf.part_samples) ? cf.part_samples : [];
    let parts = Array.isArray(cf.parts) ? cf.parts.map((p) => ({ ...p })) : null;
    const n = Math.max(
        Number(cf.part_count) || 0,
        dims.length,
        partSamples.length,
        parts ? parts.length : 0,
        2,
    );
    while (dims.length < n) dims.push('');
    if (!parts || parts.length === 0) {
        parts = [];
        for (let i = 0; i < n; i += 1) {
            const t = dims[i] || '';
            parts.push({
                index: i,
                sample: partSamples[i] || '',
                combine_with_previous: false,
                target: t && t !== COLSTD_CUSTOM ? t : '',
                custom_name: '',
                user_set: false,
            });
        }
    } else {
        while (parts.length < n) {
            const i = parts.length;
            parts.push({
                index: i,
                sample: partSamples[i] || '',
                combine_with_previous: false,
                target: dims[i] || '',
                custom_name: '',
                user_set: false,
            });
        }
        parts = parts.slice(0, n).map((p, i) => ({
            index: i,
            sample: p.sample || partSamples[i] || '',
            combine_with_previous: i > 0 && Boolean(p.combine_with_previous),
            target: p.target === COLSTD_CUSTOM ? COLSTD_CUSTOM : (p.target || ''),
            custom_name: p.custom_name || '',
            user_set: Boolean(p.user_set),
        }));
    }
    cf.part_count = n;
    cf.parts = parts;
    cf.target_dimensions = dims;
    if (cf.single_dimension == null) cf.single_dimension = false;
    if (cf.accepted == null) cf.accepted = !cf.single_dimension;
    cf.single_target = cf.single_target || '';
    cf.single_custom_name = cf.single_custom_name || '';
    return cf;
}

/** Collapse parts with combine_with_previous into output_columns for persistence. */
function syncSplitDerivedFields(cf) {
    if (!cf || !Array.isArray(cf.parts)) return;
    const dims = [];
    const outputs = [];
    let current = null;
    for (const part of cf.parts) {
        dims.push(
            part.combine_with_previous && current
                ? (current.target === COLSTD_CUSTOM ? '' : current.target)
                : (part.target === COLSTD_CUSTOM ? '' : (part.target || '')),
        );
        if (part.combine_with_previous && current) {
            current.part_indexes.push(part.index);
            continue;
        }
        const isCustom = part.target === COLSTD_CUSTOM || (!part.target && part.custom_name);
        current = {
            part_indexes: [part.index],
            target: isCustom ? '' : (part.target || ''),
            custom_name: isCustom ? (part.custom_name || '') : '',
        };
        outputs.push(current);
    }
    cf.target_dimensions = dims;
    cf.output_columns = outputs;
}

async function loadColumnStd(force = false) {
    const ws = elements.columnStdWorkspace;
    if (!ws || !currentJobId || !currentSourceId) return;
    if (!force && columnStdState && columnStdState.source_id === currentSourceId) {
        renderColumnStdWorkspace(columnStdState);
        return;
    }
    ws.innerHTML = '<div class="empty-state"><span class="spinner"></span><p>Detecting multipart columns…</p></div>';
    const status = document.getElementById('columnStdStatus');
    if (status) {
        status.textContent = 'Loading…';
        status.className = 'status-badge scanning';
    }
    try {
        const data = await fetchJson(
            `/api/column-standardize/propose/${encodeURIComponent(currentJobId)}?source_id=${encodeURIComponent(currentSourceId)}`,
        );
        const splits = (Array.isArray(data.splits) ? data.splits : []).map((s) => normalizeColumnStdSplit({ ...s }));
        const attrLabels = [
            ...(Array.isArray(data.attribute_options) ? data.attribute_options : []),
            ...(data.media_hierarchy || []).map((l) => ({ id: l.id, name: l.name })),
            ...(data.common_attributes || []).map((a) => ({ id: a.id, name: a.name })),
        ];
        const labelById = {};
        for (const a of attrLabels) {
            if (a && a.id) labelById[a.id] = a.name || a.id;
        }
        const targetOptsList = (data.attribute_options && data.attribute_options.length)
            ? data.attribute_options.map((t) => ({ id: t.id, name: t.name || t.id }))
            : (data.attribute_targets || []).map((t) => ({ id: t, name: labelById[t] || t }));
        columnStdState = {
            source_id: currentSourceId,
            columns: data.columns || [],
            splits,
            combines: Array.isArray(data.combines) ? data.combines.map((c) => ({ ...c })) : [],
            destination_grain_columns: Array.isArray(data.destination_grain_columns)
                ? [...data.destination_grain_columns]
                : [],
            attribute_targets: data.attribute_targets || [],
            attribute_options: targetOptsList,
            media_hierarchy: data.media_hierarchy || [],
            common_attributes: data.common_attributes || [],
        };
        renderColumnStdWorkspace(columnStdState);
        if (status) {
            const n = splits.length;
            status.textContent = n ? `${n} multipart` : 'Ready';
            status.className = n ? 'status-badge warning' : 'status-badge';
        }
    } catch (e) {
        columnStdState = { error: e.message, source_id: currentSourceId };
        if (ws) ws.innerHTML = `<div class="empty-state"><p class="standardize-error">${escapeHtml(e.message)}</p></div>`;
        if (status) {
            status.textContent = 'Error';
            status.className = 'status-badge';
        }
        showToast(e.message, 'error');
    }
}

function renderColumnStdWorkspace(state) {
    const ws = elements.columnStdWorkspace;
    if (!ws) return;
    if (!state || state.error) {
        ws.innerHTML = `<div class="empty-state"><p class="standardize-error">${escapeHtml(state?.error || 'No data')}</p></div>`;
        return;
    }
    const targets = state.attribute_options || state.attribute_targets || [];
    const attrLabels = [
        ...(state.media_hierarchy || []).map((l) => ({ id: l.id, name: l.name })),
        ...(state.common_attributes || []).map((a) => ({ id: a.id, name: a.name })),
        ...(Array.isArray(state.attribute_options) ? state.attribute_options : []),
    ];
    const labelById = Object.fromEntries(attrLabels.filter((a) => a && a.id).map((a) => [a.id, a.name || a.id]));
    const targetOptsList = Array.isArray(state.attribute_options) && state.attribute_options.length
        ? state.attribute_options
        : (targets || []).map((t) => (typeof t === 'string' ? { id: t, name: labelById[t] || t } : t));
    const multipartCols = new Set((state.splits || []).map((s) => s.source_column));

    const splitsHtml = (state.splits || []).map((cf, idx) => {
        normalizeColumnStdSplit(cf);
        const modeSplit = !cf.single_dimension;
        const delim = cf.delimiter || '';
        const status = cf.single_dimension
            ? '<span class="hierarchy-pill muted">Keep as single</span>'
            : '<span class="hierarchy-pill ok">Split</span>';

        let body = '';
        if (!modeSplit) {
            const singleSel = (cf.single_target === COLSTD_CUSTOM || cf.single_custom_name)
                ? COLSTD_CUSTOM
                : (cf.single_target || '');
            body = `
                <div class="column-std-single-row">
                    <label>Map whole column to
                        <select class="sia-select sia-select--compact" data-colstd-action="single-target" data-split-idx="${idx}">
                            ${attrTargetOptions(singleSel, targetOptsList, { includeCustom: true })}
                        </select>
                    </label>
                    <input type="text" class="${singleSel === COLSTD_CUSTOM ? '' : 'hidden'}"
                        data-colstd-action="single-custom" data-split-idx="${idx}"
                        placeholder="New dimension name"
                        value="${escapeAttr(cf.single_custom_name || '')}">
                </div>`;
        } else {
            const partRows = (cf.parts || []).map((part, pi) => {
                const sel = part.custom_name && (!part.target || part.target === COLSTD_CUSTOM)
                    ? COLSTD_CUSTOM
                    : (part.target || '');
                const showCustom = sel === COLSTD_CUSTOM;
                return `<tr class="column-std-part-row" data-split-idx="${idx}" data-part-idx="${pi}">
                    <td class="column-std-part-idx">Part ${pi + 1}</td>
                    <td class="column-std-part-sample"><code>${escapeHtml(part.sample || '—')}</code></td>
                    <td>${pi === 0 ? '—' : `<label class="column-std-combine-chk">
                        <input type="checkbox" data-colstd-action="combine-prev" data-split-idx="${idx}" data-part-idx="${pi}"
                            ${part.combine_with_previous ? 'checked' : ''}>
                        Combine with previous
                    </label>`}</td>
                    <td>
                        <select class="sia-select sia-select--compact" data-colstd-action="part-target" data-split-idx="${idx}" data-part-idx="${pi}"
                            ${part.combine_with_previous ? 'disabled' : ''}>
                            ${attrTargetOptions(sel, targetOptsList, { includeCustom: true })}
                        </select>
                        <input type="text" class="column-std-custom-name sia-input ${showCustom && !part.combine_with_previous ? '' : 'hidden'}"
                            data-colstd-action="part-custom" data-split-idx="${idx}" data-part-idx="${pi}"
                            placeholder="New dimension name"
                            value="${escapeAttr(part.custom_name || '')}"
                            ${part.combine_with_previous ? 'disabled' : ''}>
                    </td>
                </tr>`;
            }).join('');
            body = `
                <div class="column-std-parts-wrap">
                    <p class="column-std-parts-hint">Assign each part to a media dimension, or combine adjacent parts into one column.</p>
                    <table class="column-std-parts-table">
                        <thead><tr><th></th><th>Sample</th><th>Combine</th><th>Output dimension</th></tr></thead>
                        <tbody>${partRows}</tbody>
                    </table>
                </div>`;
        }

        return `<div class="column-std-split-card" data-split-idx="${idx}">
            <div class="column-std-split-head">
                <div>
                    <strong>${escapeHtml(cf.source_column)}</strong>
                    <span class="column-std-multipart-badge" title="${escapeAttr(cf.reason || '')}">Multipart · “${escapeHtml(delim)}”</span>
                    ${status}
                </div>
                <div class="column-std-mode-toggle" role="group" aria-label="Split or keep single">
                    <button type="button" class="btn-sm ${modeSplit ? 'btn-primary' : 'btn-secondary'}"
                        data-colstd-action="accept-split" data-split-idx="${idx}">Split</button>
                    <button type="button" class="btn-sm ${!modeSplit ? 'btn-primary' : 'btn-secondary'}"
                        data-colstd-action="keep-single" data-split-idx="${idx}">Keep as single</button>
                </div>
            </div>
            <div class="hierarchy-combined-samples">${escapeHtml((cf.samples || []).slice(0, 4).join(' · '))}</div>
            ${body}
        </div>`;
    }).join('') || '<p class="hierarchy-source-meta">No multipart columns detected on this sheet.</p>';

    const otherCols = (state.columns || []).filter((c) => !multipartCols.has(c));
    const otherHtml = otherCols.length
        ? `<details class="column-std-other"><summary>Other columns (${otherCols.length}) — not multipart</summary>
            <p class="hierarchy-source-meta">${escapeHtml(otherCols.join(', '))}</p></details>`
        : '';

    const grainChecks = (state.media_hierarchy || []).map((lv) => {
        const checked = (state.destination_grain_columns || []).includes(lv.id);
        return `<label class="column-std-grain-chip">
            <input type="checkbox" data-colstd-action="grain" value="${escapeAttr(lv.id)}" ${checked ? 'checked' : ''}>
            ${escapeHtml(lv.name)}
        </label>`;
    }).join('');

    ws.innerHTML = `
        <div class="column-std-intro">
            <p>Columns with multipart text (stable separator) are marked below. Choose <strong>Split</strong> or <strong>Keep as single</strong>.
            After a split, combine adjacent parts into one output and map each output to a media dimension — or name a new one.</p>
        </div>
        <section class="column-std-block">
            <h3>Multipart columns</h3>
            <div class="column-std-splits">${splitsHtml}</div>
            ${otherHtml}
        </section>
        <section class="column-std-block">
            <h3>Destination grain columns (Schema Mapping)</h3>
            <div class="column-std-grains">${grainChecks}</div>
        </section>
    `;

    if (ws.dataset.bound !== '1') {
        ws.dataset.bound = '1';
        ws.addEventListener('change', onColumnStdChange);
        ws.addEventListener('click', onColumnStdClick);
        ws.addEventListener('input', onColumnStdChange);
    }
}

function onColumnStdChange(e) {
    const el = e.target;
    const action = el.dataset?.colstdAction;
    if (!action || !columnStdState) return;
    const si = Number(el.dataset.splitIdx);
    const pi = Number(el.dataset.partIdx);
    const split = columnStdState.splits?.[si];

    if (action === 'part-target' && split?.parts?.[pi]) {
        const part = split.parts[pi];
        if (el.value === COLSTD_CUSTOM) {
            part.target = COLSTD_CUSTOM;
        } else {
            part.target = el.value;
            part.custom_name = '';
        }
        // Flag the analyst's own choice so re-detection keeps it and refreshes
        // the parts nobody touched.
        part.user_set = true;
        syncSplitDerivedFields(split);
        renderColumnStdWorkspace(columnStdState);
    } else if (action === 'part-custom' && split?.parts?.[pi]) {
        split.parts[pi].custom_name = el.value;
        split.parts[pi].target = COLSTD_CUSTOM;
        split.parts[pi].user_set = true;
        syncSplitDerivedFields(split);
    } else if (action === 'combine-prev' && split?.parts?.[pi]) {
        split.parts[pi].combine_with_previous = Boolean(el.checked);
        split.parts[pi].user_set = true;
        syncSplitDerivedFields(split);
        renderColumnStdWorkspace(columnStdState);
    } else if (action === 'single-target' && split) {
        if (el.value === COLSTD_CUSTOM) {
            split.single_target = COLSTD_CUSTOM;
        } else {
            split.single_target = el.value;
            split.single_custom_name = '';
        }
        renderColumnStdWorkspace(columnStdState);
    } else if (action === 'single-custom' && split) {
        split.single_custom_name = el.value;
        split.single_target = COLSTD_CUSTOM;
    } else if (action === 'grain') {
        const id = el.value;
        const set = new Set(columnStdState.destination_grain_columns || []);
        if (el.checked) set.add(id);
        else set.delete(id);
        columnStdState.destination_grain_columns = [...set];
    }
    scheduleColumnStdSave();
}

function onColumnStdClick(e) {
    const btn = e.target.closest('[data-colstd-action]');
    if (!btn || !columnStdState) return;
    const action = btn.dataset.colstdAction;
    if (action === 'accept-split') {
        const split = columnStdState.splits[Number(btn.dataset.splitIdx)];
        if (split) {
            split.accepted = true;
            split.single_dimension = false;
            normalizeColumnStdSplit(split);
            syncSplitDerivedFields(split);
            renderColumnStdWorkspace(columnStdState);
            scheduleColumnStdSave();
        }
    } else if (action === 'keep-single') {
        const split = columnStdState.splits[Number(btn.dataset.splitIdx)];
        if (split) {
            split.accepted = false;
            split.single_dimension = true;
            renderColumnStdWorkspace(columnStdState);
            scheduleColumnStdSave();
        }
    }
}

function scheduleColumnStdSave() {
    columnStdDirty = true;
    if (columnStdSaveTimer) clearTimeout(columnStdSaveTimer);
    columnStdSaveTimer = setTimeout(() => {
        columnStdSaveTimer = null;
        void persistColumnStdDraft(false);
    }, 600);
}

function resolveColumnStdSourceId() {
    if (currentSourceId) return String(currentSourceId);
    if (columnStdState?.source_id) return String(columnStdState.source_id);
    const first = (lastSetupSources || [])[0] || (setupSourceRegistryOrder || [])[0];
    return first?.source_id ? String(first.source_id) : '';
}

function prepareColumnStdPayload(complete) {
    if (!columnStdState) return null;
    const sourceId = resolveColumnStdSourceId();
    if (!sourceId) return null;
    for (const s of columnStdState.splits || []) {
        normalizeColumnStdSplit(s);
        // Explicit Split mode (not keep-as-single) must be marked accepted on save
        if (complete && !s.single_dimension) {
            s.accepted = true;
        }
        syncSplitDerivedFields(s);
        if (s.single_target === COLSTD_CUSTOM) s.single_target = '';
        for (const p of s.parts || []) {
            if (p.target === COLSTD_CUSTOM) p.target = '';
        }
        for (const o of s.output_columns || []) {
            if (o.target === COLSTD_CUSTOM) o.target = '';
        }
    }
    return {
        source_id: sourceId,
        sheet_name: currentSheet || columnStdState.sheet_name || '',
        splits: columnStdState.splits || [],
        combines: columnStdState.combines || [],
        destination_grain_columns: columnStdState.destination_grain_columns || [],
        complete: Boolean(complete),
    };
}

async function persistColumnStdDraft(complete = false) {
    const sourceId = resolveColumnStdSourceId();
    if (!currentJobId) {
        console.warn('Column shaping save skipped: no job_id');
        return false;
    }
    if (!sourceId) {
        console.warn('Column shaping save skipped: no source_id');
        return false;
    }
    if (!columnStdState || columnStdState.error) {
        console.warn('Column shaping save skipped: no valid state');
        return false;
    }
    currentSourceId = sourceId;
    const payload = prepareColumnStdPayload(complete);
    if (!payload) return false;
    try {
        const res = await fetch(`/api/column-standardize/save/${encodeURIComponent(currentJobId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.error) {
            const msg = data.error || data.message || `Save failed (${res.status})`;
            throw new Error(msg);
        }
        columnStdDirty = false;
        try {
            if (data.ux_stepper_summary) {
                refreshUxStepperFromJob({
                    ...(lastSetupJobForHeader || {}),
                    ux_stepper_summary: data.ux_stepper_summary,
                    ux_source_progress: data.ux_stepper_summary.ux_source_progress,
                });
            }
        } catch (uiErr) {
            console.warn('UX refresh after column shaping save failed', uiErr);
        }
        return true;
    } catch (e) {
        console.warn('Column shaping draft save failed', e);
        persistColumnStdDraft.lastError = e?.message || String(e);
        return false;
    }
}

function flushSetupDraftsBeacon() {
    try {
        if (setupUiStep === SETUP_STEP_MAPPING && getAllMappingRows().length > 0 && currentJobId) {
            const rows = sanitizeMappingForApi(getAllMappingRows());
            const body = JSON.stringify({
                mapping: rows,
                sheet_name: currentSheet,
                source_id: currentSourceId,
            });
            if (navigator.sendBeacon) {
                navigator.sendBeacon(
                    `/api/mapping/submit/${encodeURIComponent(currentJobId)}`,
                    new Blob([body], { type: 'application/json' }),
                );
            } else {
                void persistMappingDraft();
            }
        }
        if (columnStdState && currentJobId && currentSourceId && columnStdDirty) {
            const payload = prepareColumnStdPayload(false);
            if (payload && navigator.sendBeacon) {
                navigator.sendBeacon(
                    `/api/column-standardize/save/${encodeURIComponent(currentJobId)}`,
                    new Blob([JSON.stringify(payload)], { type: 'application/json' }),
                );
                columnStdDirty = false;
            } else if (payload) {
                void persistColumnStdDraft(false);
            }
        }
    } catch (e) {
        console.warn('flushSetupDraftsBeacon failed', e);
    }
}

async function confirmColumnStdAndOpenMapping() {
    const btn = elements.confirmColumnStdBtn;
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Saving…';
    }
    try {
        if (!currentJobId) throw new Error('No active job. Return to Upload and open this job again.');
        if (!resolveColumnStdSourceId()) throw new Error('No active sheet/source selected.');
        if (!columnStdState || columnStdState.error) await loadColumnStd(true);
        if (!columnStdState || columnStdState.error) {
            throw new Error(columnStdState?.error || 'Column shaping is not ready yet.');
        }
        persistColumnStdDraft.lastError = '';
        const ok = await persistColumnStdDraft(true);
        if (!ok) {
            throw new Error(persistColumnStdDraft.lastError || 'Could not save column shaping.');
        }
        switchStep(SETUP_STEP_MAPPING);
        showToast('Column shaping saved — map columns to media attributes.', 'success');
    } catch (e) {
        showToast(e.message || 'Could not save column shaping.', 'error');
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Save & map →';
        }
    }
}

// ===== Transitions =====

function switchStep(step) {
    const demSticky = document.getElementById('demarcationToolbarStickyRow');
    const stdSticky = document.getElementById('standardizeToolbarStickyRow');
    const colStdSticky = document.getElementById('columnStdToolbarStickyRow');
    const mapSticky = document.getElementById('mappingToolbarStickyRow');
    const prev = setupUiStep;
    const next = Number(step);

    // Best-effort flush when leaving a step (nav / back) so drafts aren't lost
    if (prev === SETUP_STEP_MAPPING && next !== SETUP_STEP_MAPPING) {
        if (mappingSaveTimer) {
            clearTimeout(mappingSaveTimer);
            mappingSaveTimer = null;
        }
        void persistMappingDraft();
    }
    if (prev === SETUP_STEP_COLUMN_STD && next !== SETUP_STEP_COLUMN_STD && columnStdState && columnStdDirty) {
        if (columnStdSaveTimer) {
            clearTimeout(columnStdSaveTimer);
            columnStdSaveTimer = null;
        }
        void persistColumnStdDraft(false);
    }

    setupUiStep = next;
    if (layoutMinimapWantExpanded && next !== SETUP_STEP_LAYOUT) setLayoutMinimapExpanded(false);

    const showLayout = next === SETUP_STEP_LAYOUT;
    const showStd = next === SETUP_STEP_STANDARDIZE;
    const showColStd = next === SETUP_STEP_COLUMN_STD;
    const showMap = next === SETUP_STEP_MAPPING;

    if (elements.tabDemarcation) elements.tabDemarcation.classList.toggle('active', showLayout);
    if (elements.tabMapping) elements.tabMapping.classList.toggle('active', showMap);
    if (elements.demarcationSection) elements.demarcationSection.classList.toggle('hidden', !showLayout);
    if (elements.standardizeSection) elements.standardizeSection.classList.toggle('hidden', !showStd);
    if (elements.columnStdSection) elements.columnStdSection.classList.toggle('hidden', !showColStd);
    if (elements.mappingSection) elements.mappingSection.classList.toggle('hidden', !showMap);
    if (demSticky) demSticky.classList.toggle('hidden', !showLayout);
    if (stdSticky) stdSticky.classList.toggle('hidden', !showStd);
    if (colStdSticky) colStdSticky.classList.toggle('hidden', !showColStd);
    if (mapSticky) mapSticky.classList.toggle('hidden', !showMap);

    if (showLayout) {
        loadDemarcation(false);
    } else if (showStd) {
        loadStandardize(false);
    } else if (showColStd) {
        loadColumnStd(true);
    } else if (showMap) {
        mappingProposal = null;
        mappingBlocks = null;
        loadMapping(false);
    }
    refreshUxStepperFromJob(lastSetupJobForHeader);
}

async function persistCurrentMappingSilently() {
    const rows = getAllMappingRows();
    if (!currentJobId || rows.length === 0) {
        return;
    }
    if (setupUiStep !== SETUP_STEP_MAPPING) return;
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
        if (setupUiStep === SETUP_STEP_MAPPING && mappingProposal && mappingProposal.length > 0) {
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
