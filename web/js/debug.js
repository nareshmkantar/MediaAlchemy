/**
 * Debug Page Logic - 3-Tab Debug Workflow
 */

let jobData = null;
let eventMap = {};
let jobDebugEventMap = {};

document.addEventListener('DOMContentLoaded', () => {
    const currentJob = getCurrentJob();
    const jobId = currentJob && currentJob.jobId;

    if (!jobId) {
        showToast('No job selected', 'warning');
        return;
    }

    document.getElementById('jobId').textContent = jobId;
    window.resultsJobId = jobId;
    initResultsExportControls(jobId);
    initExportsAdvancedDropdown();
    initDebugScrollLayout();
    initTabs();
    loadJobData(jobId);
});

function downloadJobExcel(jobId) {
    if (!jobId) return;
    window.location.href = `/api/download/${encodeURIComponent(jobId)}/excel`;
    showToast('Downloading Excel file...', 'success');
}

function initExportsAdvancedDropdown() {
    const wrap = document.getElementById('exportsAdvanced');
    const toggle = document.getElementById('exportsAdvancedToggle');
    const menu = document.getElementById('exportsAdvancedMenu');
    if (!wrap || !toggle || !menu) return;

    const close = () => {
        wrap.classList.remove('is-open');
        menu.hidden = true;
        toggle.setAttribute('aria-expanded', 'false');
    };

    toggle.addEventListener('click', (e) => {
        e.stopPropagation();
        const willOpen = menu.hidden;
        if (willOpen) {
            wrap.classList.add('is-open');
            menu.hidden = false;
            toggle.setAttribute('aria-expanded', 'true');
        } else {
            close();
        }
    });

    menu.addEventListener('click', (e) => e.stopPropagation());
    document.addEventListener('click', close);
}

function initResultsExportControls(jobId) {
    const go = (pathSuffix) => {
        if (!jobId) return;
        window.location.href = `/api/download/${encodeURIComponent(jobId)}/${pathSuffix}`;
    };
    document.getElementById('downloadExcelBtn')?.addEventListener('click', () => go('excel'));
    document.getElementById('previewResultBtn')?.addEventListener('click', () => toggleResultsPreviewPanel(jobId));
    document.getElementById('downloadTableCsvBtn')?.addEventListener('click', () => go('data'));
    document.getElementById('downloadPreTransformBtn')?.addEventListener('click', () => go('pre-transform'));
    document.getElementById('downloadPostTransformBtn')?.addEventListener('click', () => go('post-transform'));
    document.getElementById('downloadArtifactsZipBtn')?.addEventListener('click', () => go('artifacts'));
}

function toggleResultsPreviewPanel(jobId) {
    const panel = document.getElementById('resultsPreviewPanel');
    const btn = document.getElementById('previewResultBtn');
    if (!panel || !btn) return;
    const willOpen = panel.hasAttribute('hidden');
    if (willOpen) {
        panel.removeAttribute('hidden');
        btn.classList.add('is-active');
        btn.setAttribute('aria-expanded', 'true');
        renderResultsPreviewPanel(jobId);
        panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } else {
        panel.setAttribute('hidden', '');
        btn.classList.remove('is-active');
        btn.setAttribute('aria-expanded', 'false');
    }
}

function renderResultsPreviewPanel(jobId) {
    const host = document.getElementById('resultsPreviewTableHost');
    const meta = document.getElementById('resultsPreviewMeta');
    if (!host) return;
    const rows = Array.isArray(jobData?.data_preview) ? jobData.data_preview : [];
    const total = jobData && jobData.total_rows != null ? Number(jobData.total_rows) : null;
    if (meta) {
        if (total != null && !Number.isNaN(total)) {
            meta.textContent = `${total} row(s) exported · showing ${Math.min(rows.length, 20)} in preview`;
        } else {
            meta.textContent = rows.length ? `${rows.length} preview row(s)` : '';
        }
    }
    host.innerHTML = renderFinalOutputPreview(
        rows,
        resolvePreviewColumnOrder(rows, jobData?.data_preview_column_order, jobData),
    );
}

function initTabs() {
    document.querySelectorAll('.debug-tab').forEach(tab => {
        tab.addEventListener('click', () => {
            const step = tab.dataset.step;

            // Update tabs
            document.querySelectorAll('.debug-tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');

            // Update panels
            document.querySelectorAll('.debug-panel').forEach(p => p.classList.remove('active'));
            document.getElementById(`panel-${step}`).classList.add('active');
        });
    });
}

function updateResultsSourceHint(data) {
    const el = document.getElementById('resultsSourceHint');
    if (!el) return;
    const reg = data?.source_registry;
    const traces = data?.source_traces;
    const nReg = Array.isArray(reg) ? reg.length : 0;
    let nTrace = Array.isArray(traces) ? traces.length : 0;
    if (nTrace <= 1 && Array.isArray(data?.debug_events) && data.debug_events.length) {
        const tagged = new Set(
            data.debug_events.map((e) => (e && e.source_id != null ? String(e.source_id).trim() : '')).filter(Boolean)
        );
        if (tagged.size > 1) {
            nTrace = tagged.size;
        }
    }
    if (nReg > 1 && nTrace <= 1) {
        el.hidden = false;
        el.textContent =
            `This job lists ${nReg} sources in the registry, but the trace reflects a single pipeline run (one workbook through the graph). ` +
            'The relationship review can still show two source previews from that registry; only sources included in the executed path appear in the trace. ' +
            'To run every upload and merge explicitly, use the multi-source flow (for example Process all sources).';
    } else {
        el.hidden = true;
        el.textContent = '';
    }
}

async function loadJobData(jobId) {
    try {
        jobData = await fetchJson(`/api/debug/${jobId}`);
        document.getElementById('jobStatus').textContent = jobData.status;
        updateResultsSourceHint(jobData);

        renderJobDebugTimeline(jobData);
        if (jobData.trace || (jobData.debug_events && jobData.debug_events.length) || (jobData.llm_traces && jobData.llm_traces.length)) {
            renderDebugData(jobData.trace || { steps: [] });
        } else {
            const traceTree = document.getElementById('traceTree');
            if (traceTree && !traceTree.innerHTML.trim()) {
                traceTree.innerHTML =
                    '<div class="empty-state">No agent trace yet — run processing with Debug Mode enabled.</div>';
            }
        }
        renderStateSnapshots(jobData.state_snapshots || []);

        renderLifecycleTimeline(jobData);

        try {
            const statusJob = await getJobStatus(jobId);
            if (typeof refreshJobExportButtons === 'function') {
                refreshJobExportButtons(statusJob);
            }
        } catch {
            /* ignore */
        }

        const prevPanel = document.getElementById('resultsPreviewPanel');
        if (prevPanel && !prevPanel.hasAttribute('hidden')) {
            renderResultsPreviewPanel(jobId);
        }
    } catch (e) {
        showToast('Failed to load job data: ' + e.message, 'error');
    }
}

function downloadSnapshot(filename) {
    const safe = encodeURIComponent(String(filename || '').split(/[/\\]/).pop() || '');
    if (!safe) return;
    const url = `/api/download/snapshot/${safe}`;
    window.open(url, '_blank');
    showToast(`Downloading ${filename}...`, 'success');
}

window.downloadSnapshot = downloadSnapshot;

function buildMiniTableFromSnapshotPreview(columns, rows) {
    const cols = Array.isArray(columns) ? columns.map((c) => String(c)) : [];
    if (!cols.length) {
        return '<p class="empty-state">No columns in this snapshot.</p>';
    }
    const esc = (v) => escapeHtml(v == null ? '' : String(v));
    const formatCell = (val) => {
        if (val === null || val === undefined) return '';
        if (typeof val === 'string' && /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(val)) {
            return esc(val.split('T')[0].split(' ')[0]);
        }
        return esc(val);
    };
    const bodyRows = Array.isArray(rows) ? rows : [];
    return `
        <div class="mini-data-preview" style="max-height: 60vh; overflow: auto;">
            <table>
                <thead><tr>${cols.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
                <tbody>
                    ${bodyRows
                        .map(
                            (row) =>
                                `<tr>${cols.map((h) => `<td>${formatCell(row[h])}</td>`).join('')}</tr>`
                        )
                        .join('')}
                </tbody>
            </table>
        </div>`;
}

async function previewSnapshotWorkbook(filename) {
    const jobId = window.resultsJobId || (typeof getCurrentJob === 'function' && getCurrentJob()?.jobId);
    if (!jobId || !filename) {
        showToast('Missing job or snapshot name', 'warning');
        return;
    }
    document.getElementById('modalSnapshotActions')?.remove();
    try {
        const data = await fetchJson(
            `/api/jobs/${encodeURIComponent(jobId)}/snapshot-preview?file=${encodeURIComponent(filename)}`
        );
        const cols = data.columns || [];
        const bodyRows = data.rows || [];
        const sheetLabel = Number.isFinite(data.sheet) ? data.sheet + 1 : 1;
        const title = `${filename} · ${data.row_count ?? bodyRows.length}×${data.column_count ?? cols.length} (sheet ${sheetLabel})`;
        const modalTitle = document.getElementById('modalTitle');
        if (modalTitle) modalTitle.textContent = title;
        const note =
            '<p style="margin: 0 0 0.5rem; color: var(--text-muted); font-size: 0.75rem;">Server preview: first sheet only, up to 100 rows (same style as layout demarcation). Download the snapshot for the full grid.</p>';
        const modalEl = document.getElementById('modalContent');
        if (modalEl) {
            modalEl.classList.remove('modal-content-plain');
            modalEl.innerHTML = note + buildMiniTableFromSnapshotPreview(cols, bodyRows);
        }
        document.getElementById('modalOverlay')?.classList.add('active');
        document.body.style.overflow = 'hidden';
    } catch (e) {
        showToast('Snapshot preview failed: ' + (e.message || e), 'error');
    }
}

window.previewSnapshotWorkbook = previewSnapshotWorkbook;

const LIFECYCLE_PHASE_META = {
    setup:          { icon: '🚀', label: 'Setup' },
    load:           { icon: '📂', label: 'Load File' },
    analyze:        { icon: '🔎', label: 'Analyze Structure' },
    mapping:        { icon: '🧭', label: 'Resolve Mapping' },
    relationships:  { icon: '🔗', label: 'Infer Relationships' },
    plan:           { icon: '🗺️', label: 'Plan' },
    plan_review:    { icon: '🙋', label: 'Plan Review' },
    resume:         { icon: '▶️', label: 'Resume' },
    execute:        { icon: '🔧', label: 'Execute Tools' },
    execute_pause:  { icon: '⏸️', label: 'Execute Paused (HITL)' },
    verify:         { icon: '🧪', label: 'Verify Output' },
    replan:         { icon: '🔁', label: 'Replan' },
    finalize:       { icon: '📦', label: 'Finalize' },
    output_ready:   { icon: '✅', label: 'Output Ready' },
    error:          { icon: '❌', label: 'Error' }
};

function safeJson(value) {
    return escapeHtml(JSON.stringify(value || {}, null, 2));
}

const SNAPSHOT_PANEL_KEYS = [
    {
        id: 'agent_state',
        label: 'AgentState',
        pick: (s) => s?.agent_state,
        kind: 'state_diff',
        singleHint: 'Run progress, frames, plan tools, HITL, and history.',
    },
    {
        id: 'context',
        label: 'Context',
        pick: (s) => s?.context,
        kind: 'context_diff',
        singleHint: 'Planner context: deferrals, setup counts, planning alignment.',
    },
    {
        id: 'memory',
        label: 'Memory / Job',
        pick: (s) => s?.memory,
        kind: 'memory_diff',
        singleHint: 'Multi-source job orchestration only (registry, deferrals across sources)—not the current sheet.',
    },
    {
        id: 'node_output',
        label: 'Node output',
        pick: (s) => s?.decision,
        kind: 'node_output',
        singleHint: 'Artifacts this step produced — expand each result to inspect context_packet, mapping_summary, etc.',
    },
];

const snapshotInspectorUi = {
    panel: 'agent_state', // source identity is shown in the banner above tabs, not a separate panel
    view: 'table',
    pairLabel: null,
    showChangedOnly: true,
};

function snapshotPairIsPaired(pair) {
    return Boolean(pair?.before && pair?.after);
}

function snapshotPanelObjects(pair, panelDef) {
    const pick = panelDef.pick;
    if (panelDef.kind === 'node_output') {
        const snap = pair?.after || pair?.before || (pair?.others || [])[0];
        return { before: {}, after: {}, single: snap ? pick(snap) || {} : {} };
    }
    if (snapshotPairIsPaired(pair)) {
        return {
            before: pick(pair.before) || {},
            after: pick(pair.after) || {},
            single: null,
        };
    }
    const snap = pair?.after || pair?.before || (pair?.others || [])[0];
    return { before: {}, after: {}, single: snap ? pick(snap) || {} : {} };
}

function snapshotPanelStats(pair, panelDef) {
    if (panelDef.kind === 'node_output') {
        const snap = pair?.after || pair?.before || (pair?.others || [])[0];
        const decision = (snap && snap.decision) || {};
        const outputs = decision.outputs && typeof decision.outputs === 'object' ? decision.outputs : {};
        const outputCount = Object.keys(outputs).length;
        const legacyCount =
            (decision.tool_step ? 1 : 0) +
            (decision.grain_and_tools ? 1 : 0) +
            (decision.hitl ? 1 : 0) +
            (decision.load ? 1 : 0);
        const inputCount = Array.isArray(decision.input_keys) ? decision.input_keys.length : 0;
        const total = outputCount || legacyCount || inputCount;
        return {
            mode: 'single',
            paired: false,
            changedCount: 0,
            totalCount: total,
            hasData: total > 0,
        };
    }
    const { before, after, single } = snapshotPanelObjects(pair, panelDef);
    const paired = snapshotPairIsPaired(pair);
    if (!paired) {
        const rows = flattenSnapshotObject(single || {});
        const nonEmpty = rows.filter((r) => r.value !== undefined && r.value !== null && r.value !== '');
        return {
            mode: 'single',
            paired: false,
            changedCount: 0,
            totalCount: nonEmpty.length,
            hasData: nonEmpty.length > 0,
        };
    }
    const compareRows = buildSnapshotCompareRows(before, after);
    const changed = compareRows.filter((r) => r.changed);
    return {
        mode: 'diff',
        paired: true,
        changedCount: changed.length,
        totalCount: compareRows.length,
        hasData: compareRows.length > 0,
    };
}

function snapshotPanelEmptyMessage(panelDef, stats, pair) {
    if (!stats.hasData) {
        return `<p class="snapshot-panel-hint">No ${escapeHtml(panelDef.label)} data in this capture.</p>`;
    }
    if (stats.mode === 'diff' && stats.changedCount === 0) {
        const skipHint =
            panelDef.id === 'context' || panelDef.id === 'memory'
                ? ' Job-level fields are attached to every node snapshot but usually only change during planning or multi-source orchestration — use Source or AgentState here.'
                : '';
        return `<p class="snapshot-panel-hint snapshot-panel-hint--unchanged">No changes in ${escapeHtml(panelDef.label)} for this step.${escapeHtml(skipHint)}</p>`;
    }
    if (stats.mode === 'single') {
        const phase = String((pair?.after || pair?.before || {}).phase || '');
        return `<p class="snapshot-panel-hint">${escapeHtml(panelDef.singleHint || '')}${phase ? ` <span class="trace-chip trace-chip--muted">${escapeHtml(phase)}</span>` : ''}</p>`;
    }
    return '';
}

function initDebugScrollLayout() {
    const panel = document.getElementById('panel-1');
    const jobSection = document.getElementById('jobDebugSection');
    const agentSection = document.getElementById('agentTraceSection');
    const collapseBtn = document.getElementById('jobDebugCollapseBtn');
    const jobBody = document.getElementById('jobDebugBody');
    if (!panel || !jobSection || !agentSection) return;

    if (collapseBtn && jobBody) {
        collapseBtn.addEventListener('click', () => {
            const collapsed = jobSection.classList.toggle('is-collapsed');
            collapseBtn.setAttribute('aria-expanded', String(!collapsed));
            collapseBtn.textContent = collapsed ? '▶' : '▼';
            if (!collapsed) {
                panel.scrollTo({ top: jobSection.offsetTop, behavior: 'smooth' });
            }
        });
    }

    const stickyObserver = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                jobSection.classList.toggle('is-header-stuck', !entry.isIntersecting && entry.boundingClientRect.top < 0);
            });
        },
        { root: panel, threshold: [0], rootMargin: '-1px 0px 0px 0px' }
    );
    const bodyTop = jobSection.querySelector('.job-debug-body');
    if (bodyTop) stickyObserver.observe(bodyTop);

    const agentObserver = new IntersectionObserver(
        (entries) => {
            entries.forEach((entry) => {
                agentSection.classList.toggle('is-reached', entry.isIntersecting);
            });
        },
        { root: panel, threshold: 0.05 }
    );
    agentObserver.observe(agentSection);
}

function formatSnapshotCell(value) {
    if (value === undefined) return '—';
    if (value === null) return 'null';
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (Array.isArray(value)) {
        if (!value.length) return '(empty list)';
        return value.map((v) => String(v)).join(', ');
    }
    if (typeof value === 'object') {
        if (Array.isArray(value.items) && value.count != null) {
            return formatSnapshotCell(value.items);
        }
        return JSON.stringify(value);
    }
    return String(value);
}

/** HTML for table cells: arrays render as bullet lists. */
function formatSnapshotCellHtml(value) {
    if (value === undefined) return '—';
    if (value === null) return 'null';
    if (typeof value === 'boolean') return value ? 'true' : 'false';
    if (Array.isArray(value)) {
        if (!value.length) return '<span class="snapshot-empty-list">(empty)</span>';
        const items = value.map((v) => {
            if (v !== null && typeof v === 'object') {
                return `<li><code>${escapeHtml(JSON.stringify(v))}</code></li>`;
            }
            return `<li>${escapeHtml(String(v))}</li>`;
        });
        return `<ul class="snapshot-value-list">${items.join('')}</ul>`;
    }
    if (typeof value === 'object') {
        if (Array.isArray(value.items)) {
            return formatSnapshotCellHtml(value.items);
        }
        return `<code>${escapeHtml(JSON.stringify(value))}</code>`;
    }
    return escapeHtml(String(value));
}

function flattenSnapshotObject(obj, prefix = '') {
    const rows = [];
    if (obj === null || obj === undefined) {
        rows.push({ path: prefix || '(root)', value: obj, kind: 'scalar' });
        return rows;
    }
    if (Array.isArray(obj)) {
        rows.push({ path: prefix || '(root)', value: obj, kind: 'list' });
        return rows;
    }
    if (typeof obj !== 'object') {
        rows.push({ path: prefix || '(root)', value: obj, kind: 'scalar' });
        return rows;
    }
    Object.keys(obj).forEach((key) => {
        const path = prefix ? `${prefix}.${key}` : key;
        const val = obj[key];
        if (val !== null && typeof val === 'object' && !Array.isArray(val)) {
            const isListHead =
                Array.isArray(val.items) &&
                (val.count != null || typeof val.count === 'number');
            if (isListHead) {
                rows.push({ path, value: val.items, kind: 'list' });
            } else {
                rows.push(...flattenSnapshotObject(val, path));
            }
        } else {
            rows.push({
                path,
                value: val,
                kind: Array.isArray(val) ? 'list' : 'scalar',
            });
        }
    });
    return rows;
}

function snapshotValuesEqual(a, b) {
    return JSON.stringify(a) === JSON.stringify(b);
}

function buildSnapshotCompareRows(beforeObj, afterObj) {
    const bRows = flattenSnapshotObject(beforeObj || {});
    const aRows = flattenSnapshotObject(afterObj || {});
    const map = new Map();
    bRows.forEach((r) => map.set(r.path, { path: r.path, before: r.value, after: undefined }));
    aRows.forEach((r) => {
        const existing = map.get(r.path) || { path: r.path, before: undefined, after: undefined };
        existing.after = r.value;
        map.set(r.path, existing);
    });
    return [...map.values()]
        .sort((x, y) => x.path.localeCompare(y.path))
        .map((row) => ({
            ...row,
            changed: !snapshotValuesEqual(row.before, row.after),
        }));
}

function groupSnapshotPairs(snapshots) {
    const byLabel = new Map();
    (snapshots || []).forEach((snap) => {
        if (!snap) return;
        const label = String(snap.label || 'snapshot');
        if (!byLabel.has(label)) byLabel.set(label, { label, before: null, after: null, others: [] });
        const g = byLabel.get(label);
        const phase = String(snap.phase || '').toLowerCase();
        if (phase.includes('before')) g.before = snap;
        else if (phase.includes('after')) g.after = snap;
        else g.others.push(snap);
    });
    return [...byLabel.values()];
}

function pickSnapshotPairForEvent(snapshots, event) {
    const groups = groupSnapshotPairs(snapshots);
    if (!groups.length) return null;
    const mod = String(event?.module || '');
    const preferred =
        groups.find((g) => g.label === `state.${mod}`) ||
        groups.find((g) => g.label.includes(mod)) ||
        groups.find((g) => g.before && g.after) ||
        groups[0];
    return preferred;
}

function renderSnapshotValueTable(rows) {
    if (!rows.length) {
        return '<p class="empty-state">No parameters in this panel.</p>';
    }
    return `
        <div class="snapshot-compare-table-wrap">
            <table class="snapshot-compare-table snapshot-value-table">
                <thead>
                    <tr>
                        <th>Parameter</th>
                        <th>Value</th>
                    </tr>
                </thead>
                <tbody>
                    ${rows
                        .map(
                            (row) => `
                        <tr>
                            <td class="snapshot-param">${escapeHtml(row.path)}</td>
                            <td class="snapshot-val">${formatSnapshotCellHtml(row.value)}</td>
                        </tr>`
                        )
                        .join('')}
                </tbody>
            </table>
        </div>`;
}

function renderSnapshotCompareTable(rows, opts = {}) {
    const showChangedOnly = Boolean(opts.showChangedOnly);
    const panelDef = opts.panelDef;
    let displayRows = rows;
    if (showChangedOnly) {
        const changed = rows.filter((r) => r.changed);
        if (!changed.length) {
            return '';
        }
        displayRows = changed;
    }
    if (!displayRows.length) {
        return '<p class="empty-state">No parameters in this panel.</p>';
    }
    const beforeCol =
        panelDef?.id === 'decision' ? 'Before decision' : 'Before';
    const afterCol =
        panelDef?.id === 'decision' ? 'After decision' : 'After';
    return `
        <div class="snapshot-compare-table-wrap">
            <table class="snapshot-compare-table">
                <thead>
                    <tr>
                        <th>Parameter</th>
                        <th>${escapeHtml(beforeCol)}</th>
                        <th>${escapeHtml(afterCol)}</th>
                    </tr>
                </thead>
                <tbody>
                    ${displayRows
                        .map(
                            (row) => `
                        <tr class="${row.changed ? 'snapshot-row-changed' : ''}">
                            <td class="snapshot-param">${escapeHtml(row.path)}</td>
                            <td class="snapshot-val">${formatSnapshotCellHtml(row.before)}</td>
                            <td class="snapshot-val">${formatSnapshotCellHtml(row.after)}</td>
                        </tr>`
                        )
                        .join('')}
                </tbody>
            </table>
        </div>`;
}

function renderSnapshotCompareJson(beforeObj, afterObj, opts = {}) {
    if (opts.single) {
        return `<pre class="code-block trace-code-block snapshot-json-single">${safeJson(beforeObj || {})}</pre>`;
    }
    const beforeLabel = opts.panelDef?.id === 'decision' ? 'Before decision' : 'Before';
    const afterLabel = opts.panelDef?.id === 'decision' ? 'After decision' : 'After';
    return `
        <div class="snapshot-json-split">
            <div class="snapshot-json-col">
                <h5>${escapeHtml(beforeLabel)}</h5>
                <pre class="code-block trace-code-block">${safeJson(beforeObj || {})}</pre>
            </div>
            <div class="snapshot-json-col">
                <h5>${escapeHtml(afterLabel)}</h5>
                <pre class="code-block trace-code-block">${safeJson(afterObj || {})}</pre>
            </div>
        </div>`;
}

function renderNodeOutputPanel(decision, pair) {
    const dec = decision && typeof decision === 'object' ? decision : {};
    const phase = String((pair?.after || pair?.before || {}).phase || '');
    const parts = [];

    if (dec.phase_note) {
        parts.push(`<p class="snapshot-panel-hint">${escapeHtml(String(dec.phase_note))}</p>`);
    }

    const outputs = dec.outputs && typeof dec.outputs === 'object' ? dec.outputs : null;
    if (outputs && Object.keys(outputs).length) {
        const items = Object.keys(outputs)
            .sort()
            .map((key, idx) => {
                const block = outputs[key] || {};
                const summary = block.summary != null ? String(block.summary) : '';
                const detail = block.detail != null ? block.detail : block;
                const modalId = `snapshot-output-${idx}-${key.replace(/[^a-z0-9_-]/gi, '_')}`;
                window.__snapshotOutputModalCache = window.__snapshotOutputModalCache || {};
                window.__snapshotOutputModalCache[modalId] = detail;
                return `
                    <details class="snapshot-output-item" ${idx === 0 ? 'open' : ''}>
                        <summary class="snapshot-output-summary">
                            <code class="snapshot-output-key">${escapeHtml(key)}</code>
                            <span class="snapshot-output-summary-text">${escapeHtml(summary)}</span>
                            <button type="button" class="btn btn-secondary btn-sm snapshot-output-expand-btn"
                                title="Open full JSON"
                                onclick="event.preventDefault(); event.stopPropagation(); openSnapshotOutputModal('${escapeAttr(modalId)}', '${escapeAttr(key)}');">
                                JSON
                            </button>
                        </summary>
                        <pre class="code-block trace-code-block snapshot-output-detail">${safeJson(detail)}</pre>
                    </details>`;
            })
            .join('');
        parts.push(`<div class="snapshot-output-drilldown">${items}</div>`);
    } else if (phase === 'node_before') {
        const inputKeys = Array.isArray(dec.input_keys) ? dec.input_keys : [];
        const previews = dec.inputs_preview && typeof dec.inputs_preview === 'object' ? dec.inputs_preview : {};
        if (inputKeys.length) {
            parts.push('<h5 class="snapshot-output-section-title">Inputs</h5>');
            parts.push(
                `<p class="snapshot-panel-hint">Declared inputs for this node. Expand a key to see what was already on state.</p>`
            );
            inputKeys.forEach((key, idx) => {
                const preview = previews[key];
                parts.push(`
                    <details class="snapshot-output-item" ${idx === 0 ? 'open' : ''}>
                        <summary class="snapshot-output-summary">
                            <code class="snapshot-output-key">${escapeHtml(String(key))}</code>
                        </summary>
                        <pre class="code-block trace-code-block snapshot-output-detail">${safeJson(
                            preview != null ? preview : {}
                        )}</pre>
                    </details>`);
            });
        }
    } else {
        const resultKeys = (dec.node && dec.node.result_keys) || dec.result_keys || [];
        if (Array.isArray(resultKeys) && resultKeys.length) {
            parts.push(
                `<p class="snapshot-panel-hint snapshot-panel-hint--unchanged">Result keys only (no drill-down captured — re-run the job to populate expandable payloads).</p>`
            );
            parts.push(
                `<ul class="snapshot-output-key-list">${resultKeys
                    .map((k) => `<li><code>${escapeHtml(String(k))}</code></li>`)
                    .join('')}</ul>`
            );
        }
        if (dec.tool_step || dec.grain_and_tools || dec.hitl || dec.load) {
            parts.push('<h5 class="snapshot-output-section-title">Step metadata</h5>');
            parts.push(
                `<pre class="code-block trace-code-block">${safeJson({
                    tool_step: dec.tool_step,
                    grain_and_tools: dec.grain_and_tools,
                    hitl: dec.hitl,
                    load: dec.load,
                    node: dec.node,
                })}</pre>`
            );
        }
    }

    if (!parts.length) {
        return '<p class="snapshot-panel-hint">No node output metadata for this capture.</p>';
    }
    return parts.join('');
}

function openSnapshotOutputModal(modalId, title) {
    const cache = window.__snapshotOutputModalCache || {};
    const detail = cache[modalId];
    openModal(title || 'Node output', JSON.stringify(detail != null ? detail : {}, null, 2), false);
}
window.openSnapshotOutputModal = openSnapshotOutputModal;

function renderSnapshotInspectorBody(pair, ui) {
    const panelDef = SNAPSHOT_PANEL_KEYS.find((p) => p.id === ui.panel) || SNAPSHOT_PANEL_KEYS[0];
    const stats = snapshotPanelStats(pair, panelDef);
    const hint = snapshotPanelEmptyMessage(panelDef, stats, pair);
    const { before, after, single } = snapshotPanelObjects(pair, panelDef);

    if (panelDef.kind === 'node_output') {
        return renderNodeOutputPanel(single, pair);
    }

    const changedOnly = ui.showChangedOnly !== false;
    if (changedOnly && stats.mode === 'diff' && stats.changedCount === 0) {
        return hint;
    }

    if (ui.view === 'json') {
        if (stats.mode === 'single') {
            return hint + renderSnapshotCompareJson(single, {}, { single: true, panelDef });
        }
        return hint + renderSnapshotCompareJson(before, after, { panelDef });
    }

    if (stats.mode === 'single') {
        const rows = flattenSnapshotObject(single || {});
        return hint + renderSnapshotValueTable(rows);
    }

    const compareRows = buildSnapshotCompareRows(before, after);
    return (
        hint +
        renderSnapshotCompareTable(compareRows, {
            showChangedOnly: changedOnly,
            panelDef,
        })
    );
}

function snapshotSourceFromPair(pair) {
    const snap = pair?.after || pair?.before || (pair?.others || [])[0];
    return snap?.source && typeof snap.source === 'object' ? snap.source : null;
}

/** Always-visible run identity (not a tab—distinct from Memory / Job). */
function renderSnapshotSourceBanner(pair) {
    const src = snapshotSourceFromPair(pair);
    if (!src || !Object.keys(src).length) {
        return '';
    }
    const chips = [];
    if (src.source_id) {
        chips.push(`<span class="trace-chip"><strong>Source</strong> ${escapeHtml(String(src.source_id))}</span>`);
    }
    if (src.sheet_name) {
        chips.push(`<span class="trace-chip">Sheet ${escapeHtml(String(src.sheet_name))}</span>`);
    }
    if (src.file_name || src.file_path) {
        chips.push(
            `<span class="trace-chip">${escapeHtml(String(src.file_name || src.file_path))}</span>`
        );
    }
    if (src.processing_sheet && src.processing_sheet !== src.sheet_name) {
        chips.push(`<span class="trace-chip">Processing ${escapeHtml(String(src.processing_sheet))}</span>`);
    }
    if (src.date_granularity) {
        chips.push(`<span class="trace-chip">${escapeHtml(String(src.date_granularity))} grain</span>`);
    }
    if (src.date_shape) {
        chips.push(`<span class="trace-chip">Date ${escapeHtml(String(src.date_shape))}</span>`);
    }
    const uids = Array.isArray(src.uid_columns) ? src.uid_columns : [];
    if (uids.length) {
        chips.push(
            `<span class="trace-chip" title="${escapeAttr(uids.join(', '))}">UID cols (${uids.length})</span>`
        );
    }
    return `<div class="snapshot-source-banner" role="note">${chips.join('')}</div>`;
}

function renderSnapshotPanelTabs(pair, ui) {
    return SNAPSHOT_PANEL_KEYS.map((p) => {
        const stats = snapshotPanelStats(pair, p);
        const changedBadge =
            stats.mode === 'diff' && stats.changedCount > 0
                ? `<span class="snapshot-panel-tab-badge">${stats.changedCount}</span>`
                : '';
        return `<button type="button" class="snapshot-panel-tab ${p.id === ui.panel ? 'active' : ''}${stats.mode === 'single' ? ' snapshot-panel-tab--single' : ''}" data-panel="${p.id}" role="tab" title="${escapeAttr(p.singleHint || p.label)}">${escapeHtml(p.label)}${changedBadge}</button>`;
    }).join('');
}

function wireSnapshotInspectorControls(host, pairs, event) {
    if (!host || !pairs.length) return;

    const getPair = () =>
        pairs.find((p) => p.label === snapshotInspectorUi.pairLabel) ||
        pickSnapshotPairForEvent(
            pairs.flatMap((p) => [p.before, p.after, ...(p.others || [])].filter(Boolean)),
            event
        ) ||
        pairs[0];

    const rerenderBody = () => {
        const body = host.querySelector('.snapshot-inspector-body');
        if (body) body.innerHTML = renderSnapshotInspectorBody(getPair(), snapshotInspectorUi);
    };

    host.querySelectorAll('.snapshot-panel-tab').forEach((btn) => {
        btn.onclick = () => {
            snapshotInspectorUi.panel = btn.getAttribute('data-panel') || 'agent_state';
            host.querySelectorAll('.snapshot-panel-tab').forEach((b) => b.classList.remove('active'));
            btn.classList.add('active');
            rerenderBody();
        };
    });

    host.querySelectorAll('.snapshot-view-toggle-btn').forEach((btn) => {
        btn.onclick = () => {
            snapshotInspectorUi.view = btn.getAttribute('data-view') || 'table';
            host.querySelectorAll('.snapshot-view-toggle-btn').forEach((b) => b.classList.remove('active'));
            btn.classList.add('active');
            rerenderBody();
        };
    });

    const changedOnlyInput = host.querySelector('.snapshot-changed-only-input');
    if (changedOnlyInput) {
        changedOnlyInput.onchange = () => {
            snapshotInspectorUi.showChangedOnly = changedOnlyInput.checked;
            rerenderBody();
        };
    }

    const pairSelect = host.querySelector('.snapshot-pair-select');
    if (pairSelect) {
        pairSelect.onchange = () => {
            snapshotInspectorUi.pairLabel = pairSelect.value;
            refreshSnapshotInspector(host, pairs, event);
        };
    }
}

function wireSnapshotInspector(host, pairs, event) {
    if (!host || !pairs.length) return;

    const getPair = () =>
        pairs.find((p) => p.label === snapshotInspectorUi.pairLabel) ||
        pickSnapshotPairForEvent(
            pairs.flatMap((p) => [p.before, p.after, ...(p.others || [])].filter(Boolean)),
            event
        ) ||
        pairs[0];

    const initial = getPair();
    if (initial && !snapshotInspectorUi.pairLabel) snapshotInspectorUi.pairLabel = initial.label;

    wireSnapshotInspectorControls(host, pairs, event);
}

function renderSnapshotInspectorHtml(snapshots, event, opts = {}) {
    const pairs = groupSnapshotPairs(snapshots);
    if (!pairs.length) return '';
    const pair =
        pairs.find((p) => p.label === snapshotInspectorUi.pairLabel) ||
        pickSnapshotPairForEvent(snapshots, event) ||
        pairs[0];
    snapshotInspectorUi.pairLabel = pair?.label || null;

    const pairOptions =
        pairs.length > 1
            ? `<label class="snapshot-pair-label">Snapshot
                <select class="snapshot-pair-select">${pairs
                    .map(
                        (p) =>
                            `<option value="${escapeHtml(p.label)}" ${p.label === pair?.label ? 'selected' : ''}>${escapeHtml(p.label)}</option>`
                    )
                    .join('')}</select></label>`
            : `<span class="trace-chip">${escapeHtml(pair?.label || '')}</span>`;

    const paired = snapshotPairIsPaired(pair);
    const phaseHint = paired
        ? '<span class="trace-chip trace-chip--muted">node_before → node_after</span>'
        : '<span class="trace-chip trace-chip--muted">single capture</span>';
    const headingHtml = opts.title
        ? `<div class="snapshot-inspector-title">${escapeHtml(opts.title)}${opts.count !== undefined ? ` <span class="trace-chip">${escapeHtml(String(opts.count))}</span>` : ''}</div>`
        : '';
    const isNodeOutputPanel = snapshotInspectorUi.panel === 'node_output';
    const changedOnlyToggle =
        paired && !isNodeOutputPanel
            ? `<label class="snapshot-changed-only-toggle">
            <input type="checkbox" class="snapshot-changed-only-input" ${snapshotInspectorUi.showChangedOnly !== false ? 'checked' : ''} />
            Changed only
        </label>`
            : '';
    const viewToggle = !isNodeOutputPanel
        ? `<div class="snapshot-view-toggle" role="group" aria-label="View mode">
                        <button type="button" class="snapshot-view-toggle-btn ${snapshotInspectorUi.view === 'table' ? 'active' : ''}" data-view="table">Table</button>
                        <button type="button" class="snapshot-view-toggle-btn ${snapshotInspectorUi.view === 'json' ? 'active' : ''}" data-view="json">JSON</button>
                    </div>`
        : '';

    return `
        <div class="snapshot-inspector" data-snapshot-inspector data-paired="${paired ? '1' : '0'}">
            <div class="snapshot-inspector-meta-row">
                <div class="snapshot-inspector-meta">
                    ${headingHtml}
                    ${pairOptions}
                    ${isNodeOutputPanel ? '<span class="trace-chip trace-chip--muted">node output (after step)</span>' : phaseHint}
                </div>
                <div class="snapshot-inspector-meta-actions">
                    ${changedOnlyToggle}
                    ${viewToggle}
                </div>
            </div>
            ${renderSnapshotSourceBanner(pair)}
            <div class="snapshot-inspector-toolbar">
                <div class="snapshot-panel-tabs" role="tablist">
                    ${renderSnapshotPanelTabs(pair, snapshotInspectorUi)}
                </div>
            </div>
            <div class="snapshot-inspector-body">
                ${renderSnapshotInspectorBody(pair, snapshotInspectorUi)}
            </div>
        </div>`;
}

function refreshSnapshotInspector(host, pairsOrPair, event) {
    const pairs = Array.isArray(pairsOrPair) ? pairsOrPair : pairsOrPair ? [pairsOrPair] : [];
    const pair =
        pairs.find((p) => p.label === snapshotInspectorUi.pairLabel) || pairs[0];
    if (!host || !pair) return;
    const paired = snapshotPairIsPaired(pair);
    host.dataset.paired = paired ? '1' : '0';
    const meta = host.querySelector('.snapshot-inspector-meta');
    const actions = host.querySelector('.snapshot-inspector-meta-actions');
    if (meta) {
        const phaseChip = meta.querySelector('.trace-chip--muted');
        if (phaseChip) {
            phaseChip.textContent = paired ? 'node_before → node_after' : 'single capture';
        }
    }
    if (actions) {
        let toggle = actions.querySelector('.snapshot-changed-only-toggle');
        if (paired && !toggle) {
            actions.insertAdjacentHTML(
                'afterbegin',
                `<label class="snapshot-changed-only-toggle">
                    <input type="checkbox" class="snapshot-changed-only-input" ${snapshotInspectorUi.showChangedOnly !== false ? 'checked' : ''} />
                    Changed only
                </label>`
            );
        } else if (!paired && toggle) {
            toggle.remove();
        }
    }
    let banner = host.querySelector('.snapshot-source-banner');
    const bannerHtml = renderSnapshotSourceBanner(pair);
    if (bannerHtml) {
        if (banner) banner.outerHTML = bannerHtml;
        else {
            const toolbar = host.querySelector('.snapshot-inspector-toolbar');
            if (toolbar) toolbar.insertAdjacentHTML('beforebegin', bannerHtml);
        }
    } else if (banner) {
        banner.remove();
    }
    const tabs = host.querySelector('.snapshot-panel-tabs');
    if (tabs) tabs.innerHTML = renderSnapshotPanelTabs(pair, snapshotInspectorUi);
    const body = host.querySelector('.snapshot-inspector-body');
    if (body) body.innerHTML = renderSnapshotInspectorBody(pair, snapshotInspectorUi);
    wireSnapshotInspectorControls(host, pairs, event);
}

function snapshotSummary(snapshot) {
    const src = snapshot?.source || {};
    const state = snapshot?.agent_state || {};
    const run = state.run || {};
    const frames = state.frames || {};
    const plan = state.plan || {};
    const context = snapshot?.context || {};
    const deferrals = context.deferrals || {};
    const decision = snapshot?.decision || {};
    const grain = decision.grain_and_tools || {};
    const bits = [];
    if (src.source_id) bits.push(String(src.source_id).split(':').pop());
    if (run.multi_source_active_batch !== undefined) bits.push(`multi=${run.multi_source_active_batch}`);
    if (deferrals.weekly_rollups_to_post_union !== undefined) {
        bits.push(`defer_weekly=${deferrals.weekly_rollups_to_post_union}`);
    }
    if (grain.defer_grain !== undefined) bits.push(`defer_grain=${grain.defer_grain}`);
    const deferred = plan.deferred_post_collate_tools || grain.deferred_until_after_union;
    if (Array.isArray(deferred) && deferred.length) bits.push(`deferred=${deferred.length}`);
    const cur = frames.current;
    if (cur) bits.push(`frame=${cur.rows || 0}×${cur.cols || 0}`);
    return bits.join(' · ');
}

function renderSnapshotPanels(snapshot, event) {
    const pairs = groupSnapshotPairs([snapshot]);
    const pair = pairs[0] || { label: snapshot?.label, before: snapshot, after: null };
    snapshotInspectorUi.panel = 'agent_state';
    snapshotInspectorUi.view = 'table';
    snapshotInspectorUi.pairLabel = pair.label;
    snapshotInspectorUi.showChangedOnly = true;
    const html = renderSnapshotInspectorHtml([snapshot], event || { module: '' });
    return html;
}

function renderStateSnapshots(snapshots) {
    const host = document.getElementById('stateSnapshotContent');
    if (!host) return;
    const rows = Array.isArray(snapshots) ? snapshots : [];
    if (!rows.length) {
        host.innerHTML = '<div class="empty-state">No state snapshots recorded yet. Enable Debug Mode and re-run the job.</div>';
        return;
    }
    const bySource = new Map();
    rows.forEach((snap) => {
        const key = String(snap.source_id || 'job');
        if (!bySource.has(key)) bySource.set(key, []);
        bySource.get(key).push(snap);
    });
    host.innerHTML = `
        <div class="planner-review-card" style="margin-bottom: 16px;">
            <h4>State / Context / Memory snapshots</h4>
            <p class="planner-review-help">Point-in-time compact snapshots linked to graph nodes, tool calls, and job orchestration. Full data previews remain in the Trace tab tool details.</p>
        </div>
        ${Array.from(bySource.entries()).map(([sourceId, sourceRows]) => {
            const byLabel = new Map();
            sourceRows.forEach((snap) => {
                const label = snap.label || 'state.snapshot';
                if (!byLabel.has(label)) byLabel.set(label, []);
                byLabel.get(label).push(snap);
            });
            return `
            <section class="debug-source-section">
                <h3 class="debug-source-heading">Source <code>${escapeHtml(sourceId)}</code> · ${sourceRows.length} snapshot(s)</h3>
                <div style="display:flex; flex-direction:column; gap:12px;">
                    ${[...byLabel.entries()]
                        .map(([label, snaps], idx) => {
                            const mod = label.startsWith('state.') ? label.slice(6) : label;
                            return `
                            <details class="planner-review-card" ${idx === 0 ? 'open' : ''}>
                                <summary style="cursor:pointer; display:flex; justify-content:space-between; gap:12px; align-items:center;">
                                    <span><code>${escapeHtml(label)}</code></span>
                                    <span class="trace-chip trace-chip--muted">${snaps.length} capture(s)</span>
                                </summary>
                                ${renderSnapshotInspectorHtml(snaps, { module: mod })}
                            </details>`;
                        })
                        .join('')}
                </div>
            </section>`;
        }).join('')}
    `;
    host.querySelectorAll('[data-snapshot-inspector]').forEach((inspectorEl) => {
        const details = inspectorEl.closest('details');
        const label = details?.querySelector('summary code')?.textContent?.trim() || '';
        const section = inspectorEl.closest('.debug-source-section');
        const sourceId = section?.querySelector('.debug-source-heading code')?.textContent?.trim() || 'job';
        const snaps = rows.filter(
            (s) => String(s.source_id || 'job') === sourceId && String(s.label || '') === label
        );
        wireSnapshotInspector(inspectorEl, groupSnapshotPairs(snaps), {
            module: label.startsWith('state.') ? label.slice(6) : label,
        });
    });
}

function snapshotsForEvent(eventId) {
    const rows = Array.isArray(jobData?.state_snapshots) ? jobData.state_snapshots : [];
    return rows.filter((snap) => snap && snap.anchor_event_id === eventId);
}

/** Column order for preview tables (job field, last tool trace, or row keys). */
function resolvePreviewColumnOrder(rows, explicitOrder, jobData) {
    if (Array.isArray(explicitOrder) && explicitOrder.length) {
        return explicitOrder.map((c) => String(c));
    }
    const fromJob = jobData?.data_preview_column_order;
    if (Array.isArray(fromJob) && fromJob.length) {
        return fromJob.map((c) => String(c));
    }
    const tools = jobData?.tool_executions;
    if (Array.isArray(tools)) {
        for (let i = tools.length - 1; i >= 0; i--) {
            const order = tools[i]?.output_preview?.column_order;
            if (Array.isArray(order) && order.length) {
                return order.map((c) => String(c));
            }
        }
    }
    if (!Array.isArray(rows) || !rows.length) return [];
    const keys = Object.keys(rows[0] || {});
    return keys;
}

/** Reorder sample row keys to match column_order for JSON display. */
function normalizeOutputPreviewForDisplay(preview) {
    if (!preview || typeof preview !== 'object') return preview;
    const order = preview.column_order;
    const sample = preview.sample;
    if (!Array.isArray(order) || !order.length || !Array.isArray(sample) || !sample.length) {
        return preview;
    }
    return {
        ...preview,
        sample: sample.map((row) => {
            const out = {};
            for (const col of order) {
                if (row && Object.prototype.hasOwnProperty.call(row, col)) {
                    out[col] = row[col];
                }
            }
            if (row && typeof row === 'object') {
                for (const k of Object.keys(row)) {
                    if (!Object.prototype.hasOwnProperty.call(out, k)) {
                        out[k] = row[k];
                    }
                }
            }
            return out;
        }),
    };
}

function formatConfidencePct(value) {
    const num = Number(value);
    if (!Number.isFinite(num)) return 'N/A';
    return `${(num * 100).toFixed(1)}%`;
}

function renderJobDebugTimeline(data) {
    const tree = document.getElementById('jobDebugTree');
    const detail = document.getElementById('jobDebugDetail');
    if (!tree) return;

    const events = Array.isArray(data?.job_debug_events) ? data.job_debug_events : [];
    jobDebugEventMap = {};
    events.forEach((e) => {
        if (e && e.event_id) jobDebugEventMap[e.event_id] = e;
    });

    const dfs = data?.data_files_summary;
    const setup = data?.setup_summary;
    const statsChips = [];
    if (dfs && (dfs.file_count || dfs.total_sheets)) {
        statsChips.push(`${dfs.file_count} file(s) · ${dfs.total_sheets} sheet(s)`);
    }
    if (setup && setup.source_count) {
        const doneL = (setup.per_source || []).filter((s) => s.layout_complete).length;
        const doneM = (setup.per_source || []).filter((s) => s.mapping_complete).length;
        statsChips.push(`${setup.source_count} source(s) · layout ${doneL}/${setup.source_count} · mapping ${doneM}/${setup.source_count}`);
    }
    const statsHtml = statsChips.length
        ? `<ul class="debug-stats-bar">${statsChips.map((t) => `<li>${escapeHtml(t)}</li>`).join('')}</ul>`
        : '';

    if (!events.length) {
        tree.innerHTML =
            statsHtml +
            '<div class="empty-state">No orchestration events yet.</div>';
        if (detail) {
            detail.innerHTML = '<div class="empty-state">Select an orchestration step</div>';
        }
        return;
    }

    const ordered = [...events].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    const roots = buildEventTree(ordered);
    tree.innerHTML = statsHtml + roots.map((n) => renderJobDebugTreeNode(n)).join('');

    const first = ordered.find((e) => e.status === 'error') || ordered[0];
    if (first && first.event_id) {
        const hdr = tree.querySelector(`.tree-node-header[data-job-debug-id="${first.event_id}"]`);
        if (hdr) hdr.classList.add('selected');
        selectJobDebugNode(first.event_id);
    }
}

function jobDebugTreeLabel(node) {
    return String(node.display_label || node.label || node.module || node.phase || 'step');
}

function renderJobDebugTreeNode(node, depth = 0) {
    const hasChildren = node.children && node.children.length > 0;
    const typeClass = `type-${node.process_type || 'job_debug'}`;
    const statusWarn = node.status === 'warning' || node.status === 'error';
    let icon = '◎';
    if (node.phase === 'upload' || node.phase === 'inventory') icon = '▣';
    if (node.phase === 'demarcation') icon = '▤';
    if (node.phase === 'mapping') icon = '▥';
    if (node.phase === 'agent_run') icon = '▶';
    if (node.phase === 'collation' || node.phase === 'deferred') icon = '⇄';
    if (node.phase === 'export') icon = '✓';
    if (statusWarn) icon = '!';

    const toggleBtn = hasChildren
        ? `<span id="job-toggle-${node.event_id}" class="tree-toggle" style="margin-left: auto; color: var(--text-secondary);">▼</span>`
        : '';
    const paddingLeft = 10 + depth * 15;
    const label = escapeHtml(jobDebugTreeLabel(node));
    const phase = escapeHtml(String(node.phase || ''));
    const warnClass = statusWarn ? ' tree-node-header--warning' : '';

    return `
        <div class="tree-node">
            <div class="tree-node-header ${typeClass}${warnClass}"
                 data-job-debug-id="${node.event_id}"
                 onclick="handleJobDebugNodeClick('${node.event_id}', ${hasChildren})"
                 style="padding-left: ${paddingLeft}px; padding-right: 10px;">
                <div class="tree-node-header-top">
                    <span class="tree-icon">${icon}</span>
                    <span class="tree-badge ${typeClass}">${phase}</span>
                    <span class="tree-label">${label}</span>
                    ${toggleBtn}
                </div>
            </div>
            ${hasChildren ? `<div id="job-children-${node.event_id}" style="display: block;">${node.children.map((c) => renderJobDebugTreeNode(c, depth + 1)).join('')}</div>` : ''}
        </div>`;
}

function toggleJobDebugNode(eventId) {
    const childrenContainer = document.getElementById(`job-children-${eventId}`);
    const toggleIcon = document.getElementById(`job-toggle-${eventId}`);
    if (childrenContainer) {
        const isHidden = childrenContainer.style.display === 'none';
        childrenContainer.style.display = isHidden ? 'block' : 'none';
        if (toggleIcon) toggleIcon.textContent = isHidden ? '▼' : '▶';
    }
}

function handleJobDebugNodeClick(eventId, hasChildren) {
    const tree = document.getElementById('jobDebugTree');
    if (tree) {
        tree.querySelectorAll('.tree-node-header').forEach((h) => h.classList.remove('selected'));
        const hdr = tree.querySelector(`.tree-node-header[data-job-debug-id="${eventId}"]`);
        if (hdr) hdr.classList.add('selected');
    }
    selectJobDebugNode(eventId);
    if (hasChildren) toggleJobDebugNode(eventId);
}

function renderJobDebugMappingTable(mappingTable) {
    if (!Array.isArray(mappingTable) || !mappingTable.length) return '';
    const hasSource = mappingTable.some((r) => r && r.source_id);
    const rows = mappingTable
        .map((r) => {
            const unresolved = !!r.unresolved;
            const rowClass = unresolved ? 'job-debug-table-row--warn' : '';
            const conf =
                r.confidence != null && Number.isFinite(Number(r.confidence))
                    ? `${(Number(r.confidence) * 100).toFixed(0)}%`
                    : '—';
            return `<tr class="${rowClass}">
                ${hasSource ? `<td>${escapeHtml(String(r.source_id || ''))}</td>` : ''}
                <td>${escapeHtml(String(r.source_column || '—'))}</td>
                <td>${escapeHtml(String(r.target_column || '—'))}</td>
                <td>${escapeHtml(conf)}</td>
            </tr>`;
        })
        .join('');
    return `
        <div class="trace-detail-section job-debug-detail-section">
            <h4>Column mappings</h4>
            <table class="job-debug-detail-table">
                <thead><tr>
                    ${hasSource ? '<th>Source</th>' : ''}
                    <th>Sheet column</th><th>Template field</th><th>Confidence</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}

function renderJobDebugLayoutTable(layoutBlocks) {
    if (!Array.isArray(layoutBlocks) || !layoutBlocks.length) return '';
    const hasSource = layoutBlocks.some((b) => b && b.source_id);
    const rows = layoutBlocks
        .map(
            (b) => `<tr>
                ${hasSource ? `<td>${escapeHtml(String(b.source_id || ''))}</td>` : ''}
                <td>${escapeHtml(String(b.block_label || '—'))}</td>
                <td>${escapeHtml(String(b.category || '—'))}</td>
                <td>${escapeHtml(String(b.start_row ?? '—'))}–${escapeHtml(String(b.end_row ?? '—'))}</td>
                <td>${escapeHtml(String(b.start_col ?? '—'))}–${escapeHtml(String(b.end_col ?? '—'))}</td>
                <td>${escapeHtml(String(b.header_row ?? '—'))}</td>
            </tr>`
        )
        .join('');
    return `
        <div class="trace-detail-section job-debug-detail-section">
            <h4>Layout blocks</h4>
            <table class="job-debug-detail-table">
                <thead><tr>
                    ${hasSource ? '<th>Source</th>' : ''}
                    <th>Block</th><th>Category</th><th>Rows</th><th>Cols</th><th>Header row</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>`;
}

function renderJobDebugUploadFiles(files) {
    if (!Array.isArray(files) || !files.length) return '';
    const items = files
        .map(
            (f) =>
                `<li><strong>${escapeHtml(String(f.file_name || 'file'))}</strong> — ${escapeHtml(
                    (f.sheets || []).join(', ') || 'no sheets'
                )}</li>`
        )
        .join('');
    return `<div class="trace-detail-section"><h4>Workbooks</h4><ul class="job-debug-file-list">${items}</ul></div>`;
}

function renderJobDebugSaveHistory(meta) {
    const hist = meta && Array.isArray(meta.save_history) ? meta.save_history : [];
    if (!hist.length) return '';
    const rows = hist
        .map(
            (h) =>
                `<tr><td>${formatTime(h.timestamp)}</td><td>${escapeHtml(String(h.summary || ''))}</td></tr>`
        )
        .join('');
    return `
        <details class="job-debug-save-history">
            <summary>Earlier saves (${hist.length})</summary>
            <table class="job-debug-detail-table"><thead><tr><th>Time</th><th>Summary</th></tr></thead><tbody>${rows}</tbody></table>
        </details>`;
}

function renderJobDebugDetailBody(ev, meta) {
    const parts = [];
    const statusChipClass =
        ev.status === 'error'
            ? 'trace-chip--error'
            : ev.status === 'warning'
              ? 'trace-chip--warning'
              : ev.status === 'success'
                ? 'trace-chip--success'
                : '';

    if (ev.input_summary) {
        parts.push(`<p class="trace-detail-summary${ev.status === 'warning' ? ' trace-detail-summary--warn' : ''}">${escapeHtml(ev.input_summary)}</p>`);
    }
    if (ev.source_id) {
        parts.push(
            `<p class="job-debug-source-line"><strong>Source:</strong> <code>${escapeHtml(String(ev.source_id))}</code>${ev.sheet_name ? ` · sheet <strong>${escapeHtml(String(ev.sheet_name))}</strong>` : ''}</p>`
        );
    }

    if (meta.files) parts.push(renderJobDebugUploadFiles(meta.files));
    if (meta.mapping_table) parts.push(renderJobDebugMappingTable(meta.mapping_table));
    if (meta.layout_blocks) parts.push(renderJobDebugLayoutTable(meta.layout_blocks));
    parts.push(renderJobDebugSaveHistory(meta));

    if (ev.phase === 'agent_run') {
        if (meta.hitl_pending != null || meta.rows != null || meta.overall_confidence != null) {
            const conf =
                meta.overall_confidence != null && Number.isFinite(Number(meta.overall_confidence))
                    ? ` · confidence ${(Number(meta.overall_confidence) * 100).toFixed(0)}%`
                    : '';
            parts.push(
                `<div class="trace-detail-section"><h4>Run outcome</h4><p class="trace-result-line">${
                    meta.hitl_pending
                        ? 'Paused for human review (see Review tab).'
                        : `Rows after run: ${escapeHtml(String(meta.rows ?? '—'))}${escapeHtml(conf)}`
                }</p></div>`
            );
        }
        parts.push(
            '<p class="block-inline-note">Select this step to focus <strong>Agent Trace</strong> for the same source below.</p>'
        );
    }

    const detailKeys = new Set([
        'mapping_table',
        'layout_blocks',
        'files',
        'save_history',
        'save_count',
    ]);
    const metaRest = {};
    Object.keys(meta).forEach((k) => {
        if (!detailKeys.has(k)) metaRest[k] = meta[k];
    });
    if (Object.keys(metaRest).length) {
        parts.push(
            `<details class="job-debug-raw-meta"><summary>Technical metadata</summary><pre class="code-block">${escapeHtml(JSON.stringify(metaRest, null, 2))}</pre></details>`
        );
    }

    return { parts: parts.join(''), statusChipClass };
}

function selectJobDebugNode(eventId) {
    const ev = jobDebugEventMap[eventId];
    const detail = document.getElementById('jobDebugDetail');
    if (!detail) return;
    if (!ev) {
        detail.innerHTML = '<div class="empty-state">Step not found</div>';
        return;
    }

    const meta = ev.metadata && typeof ev.metadata === 'object' ? ev.metadata : {};
    const snaps = (jobData?.state_snapshots || []).filter(
        (s) =>
            s &&
            (s.label === ev.label ||
                (ev.source_id && String(s.source_id) === String(ev.source_id) && String(s.phase || '') === String(ev.phase || '')))
    );

    let snapLinks = '';
    if (snaps.length) {
        snapLinks =
            '<div class="trace-detail-section"><h4>State snapshots</h4><ul>' +
            snaps
                .slice(0, 5)
                .map(
                    (s) =>
                        `<li><button type="button" class="btn-link" onclick="document.querySelector('.debug-tab[data-step=\\'4\\']')?.click()">${escapeHtml(s.label || s.snapshot_id || 'snapshot')}</button></li>`
                )
                .join('') +
            '</ul></div>';
    }

    const title = jobDebugTreeLabel(ev);
    const { parts, statusChipClass } = renderJobDebugDetailBody(ev, meta);

    detail.innerHTML = `
        <div class="trace-detail-header">
            <h3 class="trace-detail-title">${escapeHtml(title)}</h3>
            <div class="trace-detail-meta">
                <span class="badge job_debug">job</span>
                <span class="trace-chip">${escapeHtml(ev.phase || '')}</span>
                <span class="trace-chip ${statusChipClass}">${escapeHtml(ev.status || '')}</span>
                <span class="trace-chip trace-chip--muted">${formatTime(ev.timestamp)}</span>
            </div>
        </div>
        ${parts}
        ${snapLinks}
    `;
    detail.scrollTop = 0;

    if (ev.phase === 'agent_run' && ev.source_id && String(ev.source_id) !== '__collation__') {
        focusAgentTraceForSource(ev.source_id);
    }
}

function focusAgentTraceForSource(sourceId) {
    const sid = String(sourceId || '').trim();
    if (!sid) return;
    const section = document.querySelector('.agent-trace-section');
    if (section) {
        section.classList.add('is-focused');
    }
    const runs = jobData?.source_traces || [];
    const idx = runs.findIndex((r) => String(r?.source_id) === sid);
    if (idx >= 0) {
        const host = document.getElementById(`traceTreeSrc-${idx}`);
        const sourceSection = host?.closest('.debug-source-section');
        const listPane = host?.closest('.debug-split-list');
        if (sourceSection && listPane) {
            listPane.scrollTop = Math.max(0, sourceSection.offsetTop - listPane.offsetTop - 8);
        }
        const firstHdr = host?.querySelector('.tree-node-header[data-event-id]');
        if (firstHdr) {
            firstHdr.click();
        }
    }
}

window.handleJobDebugNodeClick = handleJobDebugNodeClick;

function mergeDebugEvents(jobEvents = [], traceEvents = []) {
    const merged = [];
    const seen = new Set();
    [jobEvents, traceEvents].forEach(source => {
        (source || []).forEach(event => {
            if (!event || !event.event_id || seen.has(event.event_id)) return;
            seen.add(event.event_id);
            merged.push(event);
        });
    });
    return merged;
}

function attachJudgeMetrics(events, trace) {
    const traceSteps = Array.isArray(trace?.steps) ? trace.steps : [];
    if (!events.length || !traceSteps.length) return events;

    const stepBuckets = {};
    traceSteps.forEach(step => {
        if (!step || !step.step || !step.judge_metrics) return;
        const key = String(step.step);
        if (!stepBuckets[key]) stepBuckets[key] = [];
        stepBuckets[key].push(step.judge_metrics);
    });

    const nodeOrder = events
        .filter(event => event && event.process_type === 'node')
        .slice()
        .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

    nodeOrder.forEach(event => {
        const bucket = stepBuckets[event.module];
        if (bucket && bucket.length && !event.judge_metrics) {
            event.judge_metrics = bucket.shift();
        }
    });

    return events;
}

function renderLifecycleTimeline(data) {
    const container = document.getElementById('lifecycleTimeline');
    const statusEl = document.getElementById('lifecycleStatus');
    const downloadBtn = document.getElementById('lifecycleDownloadBtn');
    if (!container) return;

    const events = (data && data.lifecycle_events) || [];
    const outputFile = data && (data.output_file || (data.trace && data.trace.output_file));

    if (statusEl) {
        const hitlPending = data && data.hitl_pending;
        if (hitlPending) {
            statusEl.textContent = 'Paused (HITL)';
            statusEl.style.color = 'var(--warning, #d97706)';
        } else if (outputFile) {
            statusEl.textContent = 'Output ready';
            statusEl.style.color = 'var(--success, #059669)';
        } else {
            statusEl.textContent = (data && data.status) || '-';
            statusEl.style.color = 'var(--text-secondary)';
        }
    }

    if (downloadBtn) {
        if (outputFile) {
            downloadBtn.style.display = 'inline-flex';
            downloadBtn.onclick = () => downloadJobExcel(data.job_id);
        } else {
            downloadBtn.style.display = 'none';
        }
    }

    if (!events.length) {
        container.innerHTML = '<div class="empty-state">No lifecycle events recorded for this run.</div>';
        return;
    }

    const statusStyles = {
        ok:      { dot: 'var(--success, #059669)', badge: 'success' },
        info:    { dot: 'var(--text-secondary)',   badge: '' },
        pending: { dot: 'var(--warning, #d97706)', badge: 'warning' },
        warning: { dot: 'var(--warning, #d97706)', badge: 'warning' },
        error:   { dot: 'var(--danger, #dc2626)',  badge: 'error' }
    };

    const rows = events.map((ev, idx) => {
        const meta = LIFECYCLE_PHASE_META[ev.phase] || { icon: '•', label: ev.phase || 'step' };
        const ss = statusStyles[ev.status] || statusStyles.info;
        const ts = ev.timestamp ? formatTime(ev.timestamp) : '';
        const md = ev.metadata && Object.keys(ev.metadata).length
            ? `<pre class="code-block" style="margin-top: 6px; max-height: 140px; overflow: auto;">${escapeHtml(JSON.stringify(ev.metadata, null, 2))}</pre>`
            : '';
        return `
            <li class="lifecycle-item" style="display: flex; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border);">
                <div class="lifecycle-dot" style="width: 10px; height: 10px; border-radius: 50%; background: ${ss.dot}; margin-top: 7px; flex-shrink: 0;"></div>
                <div style="flex: 1;">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="lifecycle-phase-icon" aria-hidden="true">${meta.icon}</span>
                        <strong>${escapeHtml(meta.label)}</strong>
                        <span class="status-badge ${ss.badge}" style="margin-left: 8px;">${escapeHtml(ev.status || 'info')}</span>
                        <span style="margin-left: auto; color: var(--text-tertiary); font-size: 0.8rem;">#${idx + 1} · ${escapeHtml(ts)}</span>
                    </div>
                    ${ev.message ? `<div style="margin-top: 4px; color: var(--text-secondary);">${escapeHtml(ev.message)}</div>` : ''}
                    ${md}
                </div>
            </li>
        `;
    });

    container.innerHTML = `<ul class="lifecycle-list" style="list-style: none; padding: 0; margin: 0;">${rows.join('')}</ul>`;
}

function formatVerifierRoutingHtml(parsedOutput) {
    const po = parsedOutput && typeof parsedOutput === 'object' ? parsedOutput : {};
    const rows = [];
    const add = (label, key) => {
        if (po[key] === undefined || po[key] === '') return;
        rows.push(`<tr><td style="padding:4px 10px 4px 0;color:var(--text-secondary);white-space:nowrap">${escapeHtml(label)}</td><td style="padding:4px 0"><strong>${escapeHtml(String(po[key]))}</strong></td></tr>`);
    };
    add('Model JSON is_flat', 'model_is_flat');
    add('After weekly-contract merge', 'after_weekly_contract_merge_is_flat');
    add('Graph uses (verify_output)', 'graph_is_flat_after_verify_node');
    add('Business rules forced not-flat', 'business_rules_blocked_flat');
    add('Business rule issues', 'business_rule_issue_count');
    add('Weekly contract extra issues', 'weekly_contract_issues_added');
    if (!rows.length) return '';

    const modelFlat = po.model_is_flat === true;
    const graphFlat = po.graph_is_flat_after_verify_node === true;
    const mismatch = modelFlat && !graphFlat;
    const banner = mismatch
        ? `<p class="debug-routing-banner">The model JSON says <strong>is_flat: true</strong>, but LangGraph used <strong>is_flat: false</strong> after deterministic checks (weekly contract merge and/or business rules). Use the table below.</p>`
        : '';

    return `${banner}<div class="detail-section"><h4>Routing truth (LangGraph)</h4><table style="border-collapse:collapse;font-size:13px">${rows.join('')}</table><p class="block-inline-note" style="margin:10px 0 0">Expand <strong>Model raw JSON</strong> below for the exact LLM response text.</p></div>`;
}

/**
 * When source_traces was not persisted (length 1) but debug_events rows carry
 * source_id, split the tree so both uploads appear in the Debug UI.
 */
function buildSyntheticSourceRunsFromTaggedEvents(trace) {
    const reg = jobData.source_registry;
    if (!Array.isArray(reg) || reg.length < 2) return null;
    const merged = mergeDebugEvents(jobData.debug_events, (trace && trace.debug_events) || []);
    if (!merged.length) return null;
    const bySid = new Map();
    merged.forEach((e) => {
        if (!e || e.source_id == null || String(e.source_id).trim() === '') return;
        const sid = String(e.source_id).trim();
        if (!bySid.has(sid)) bySid.set(sid, []);
        bySid.get(sid).push(e);
    });
    const runs = [];
    bySid.forEach((events, sid) => {
        if (!events.length) return;
        if (String(sid).trim() === '__collation__') {
            return;
        }
        const meta = reg.find((r) => String(r.source_id || '').trim() === sid) || {};
        runs.push({
            source_id: sid,
            sheet_name: meta.sheet_name || '',
            trace: { debug_events: events },
        });
    });
    runs.sort((a, b) => {
        const aCol = String(a.source_id || '').trim() === '__collation__';
        const bCol = String(b.source_id || '').trim() === '__collation__';
        if (aCol && !bCol) return 1;
        if (bCol && !aCol) return -1;
        return String(a.source_id).localeCompare(String(b.source_id));
    });
    return runs.length > 1 ? runs : null;
}

function renderDebugData(trace) {
    let sourceRuns = jobData.source_traces || trace.source_runs || [];
    const synthetic = buildSyntheticSourceRunsFromTaggedEvents(trace);
    if ((!Array.isArray(sourceRuns) || sourceRuns.length <= 1) && synthetic) {
        sourceRuns = synthetic;
    }
    if (Array.isArray(sourceRuns) && sourceRuns.length > 1) {
        renderMultiSourceDebug(trace, sourceRuns);
        renderRunSummary(jobData.llm_summary || {}, trace);
        return;
    }

    // Build event map from merged debug events
    let events = mergeDebugEvents(jobData.debug_events, trace.debug_events);
    if (events.length === 0 && Array.isArray(trace.steps) && trace.steps.length > 0) {
        events = synthesizeTraceEvents(trace.steps);
    }
    events = attachJudgeMetrics(events, trace);
    eventMap = {};
    events.forEach(e => eventMap[e.event_id] = e);

    // Tab 1: Trace Tree
    if (events.length > 0) {
        renderExecutionTree(events, 'traceTree', { skipHint: true });
    } else if (jobData.llm_traces && jobData.llm_traces.length > 0) {
        // Fallback to legacy LLM list if no hierarchical events
        renderLLMCallsTab(jobData.llm_traces, jobData.tool_executions);
    } else {
        document.getElementById('traceTree').innerHTML = '<div class="empty-state">No trace recorded</div>';
    }

    // Tab 3: Run Summary
    renderRunSummary(jobData.llm_summary || {}, trace);
}

/**
 * Multi-source jobs: one LangGraph pass per source; debug_events are tagged with source_id.
 */
function renderMultiSourceDebug(trace, sourceRuns) {
    const tree = document.getElementById('traceTree');
    if (!tree) return;

    const baseEvents = jobData.debug_events || [];
    const hint = '';

    eventMap = {};
    let sectionsHtml = '';
    sourceRuns.forEach((run, idx) => {
        const sid = run.source_id != null ? String(run.source_id) : '';
        if (sid === '__collation__') {
            return;
        }
        const sn = String(run.sheet_name || run.processing_sheet || '');
        sectionsHtml +=
            '<section class="debug-source-section">' +
            `<h4 class="debug-source-heading">${escapeHtml(sn || sid)} <code>${escapeHtml(sid)}</code></h4>` +
            `<div class="debug-source-tree trace-tree" id="traceTreeSrc-${idx}"></div>` +
            '</section>';
    });
    tree.innerHTML = hint + sectionsHtml;

    let firstSelectable = null;
    sourceRuns.forEach((run, idx) => {
        const sid = run.source_id;
        if (String(sid || '').trim() === '__collation__') {
            return;
        }
        let events = baseEvents.filter((e) => e && e.source_id === sid);
        if (!events.length && run.trace && Array.isArray(run.trace.debug_events)) {
            events = [...run.trace.debug_events];
        }
        events = attachJudgeMetrics(events, run.trace || {});
        events.forEach((e) => {
            if (e && e.event_id) eventMap[e.event_id] = e;
        });
        if (!events.length && run.trace && Array.isArray(run.trace.steps)) {
            events = synthesizeTraceEvents(run.trace.steps);
            events.forEach((e) => {
                if (e && e.event_id) eventMap[e.event_id] = e;
            });
        }
        renderExecutionTree(events, `traceTreeSrc-${idx}`, { skipInitialSelect: true });
        if (!firstSelectable && events.length && events[0].event_id) {
            firstSelectable = events[0].event_id;
        }
    });

    if (firstSelectable) {
        const hdr = tree.querySelector(`.tree-node-header[data-event-id="${firstSelectable}"]`);
        if (hdr) hdr.classList.add('selected');
        selectNode(firstSelectable);
    } else {
        tree.insertAdjacentHTML(
            'beforeend',
            '<p class="empty-state" style="margin:12px 0">No hierarchical events for these sources (enable Debug Mode and re-run).</p>'
        );
    }

    if (!Object.keys(eventMap).length && jobData.llm_traces && jobData.llm_traces.length > 0) {
        renderLLMCallsTab(jobData.llm_traces, jobData.tool_executions);
    }

    const colSid = '__collation__';
    const collEvents = (baseEvents || []).filter(
        (e) => e && String(e.source_id || '').trim() === colSid
    );
    if (collEvents.length) {
        tree.insertAdjacentHTML(
            'beforeend',
            '<section class="debug-source-section">' +
                '<h4 class="debug-source-heading">Post-merge collation</h4>' +
                '<div class="debug-source-tree trace-tree" id="traceTreeSrc-collation"></div>' +
                '</section>'
        );
        collEvents.forEach((e) => {
            if (e && e.event_id) eventMap[e.event_id] = e;
        });
        renderExecutionTree(collEvents, 'traceTreeSrc-collation', { skipInitialSelect: true });
    }
}

function wireSnapshotActionButtons(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('.snapshot-preview-btn[data-snapshot-file]').forEach((btn) => {
        const f = btn.getAttribute('data-snapshot-file');
        if (!f) return;
        btn.onclick = (e) => {
            e.stopPropagation();
            previewSnapshotWorkbook(f);
        };
    });
    root.querySelectorAll('.snapshot-download-btn[data-snapshot-file]').forEach((btn) => {
        const f = btn.getAttribute('data-snapshot-file');
        if (!f) return;
        btn.onclick = (e) => {
            e.stopPropagation();
            downloadSnapshot(f);
        };
    });
}

function synthesizeTraceEvents(steps) {
    return steps.map((step, idx) => {
        const moduleName = step.module || step.step || step.node || step.name || `Step ${idx + 1}`;
        const rawIn = step.input;
        let operation = step.action || step.operation || step.status || 'step';
        if (rawIn === 'graph_execution') {
            operation = 'NODE';
        } else if (typeof rawIn === 'string' && rawIn.length) {
            operation = rawIn;
        }
        const outMsg = step.message || step.description || step.output || '';
        const inputSummary =
            moduleName === 'load_file' && outMsg
                ? outMsg
                : step.input_summary || '';
        return {
            event_id: `step-${idx}`,
            parent_event_id: null,
            module: moduleName,
            operation,
            process_type: 'node',
            status: step.status || 'success',
            duration_ms: step.duration_ms || 0,
            timestamp: step.timestamp || new Date().toISOString(),
            input_summary: inputSummary,
            message: outMsg,
            result: step.result || step.output || step.message || '',
        };
    });
}

function renderExecutionTree(events, containerId = 'traceTree', opts = {}) {
    const tree = typeof containerId === 'string' ? document.getElementById(containerId) : containerId;
    if (!tree) return;

    if (!events.length) {
        tree.innerHTML = '<div class="empty-state">No trace events</div>';
        return;
    }

    const orderedEvents = [...events].sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
    const roots = buildEventTree(orderedEvents);
    const traceHint = opts.skipHint
        ? ''
        : `<p class="block-inline-note debug-trace-hint">Expand nodes for sub-steps. For <strong>output_verifier</strong>, open the detail pane: <strong>Routing truth</strong> is what the graph used; raw JSON is the model verbatim.</p>`;
    tree.innerHTML = traceHint + roots.map(node => renderTreeNode(node)).join('');

    if (opts.skipInitialSelect) {
        return;
    }
    const firstError = orderedEvents.find((e) => e.status === 'error');
    const initialEvent = firstError || orderedEvents[0];
    if (initialEvent) {
        const initialHeader = tree.querySelector(`.tree-node-header[data-event-id="${initialEvent.event_id}"]`);
        if (initialHeader) {
            initialHeader.classList.add('selected');
        }
        selectNode(initialEvent.event_id);
    }
}

function toggleNode(eventId) {
    const childrenContainer = document.getElementById(`children-${eventId}`);
    const toggleIcon = document.getElementById(`toggle-${eventId}`);

    if (childrenContainer) {
        const isHidden = childrenContainer.style.display === 'none';
        childrenContainer.style.display = isHidden ? 'block' : 'none';
        if (toggleIcon) {
            toggleIcon.textContent = isHidden ? '▼' : '▶';
        }
    }
}

function handleNodeClick(eventId, hasChildren) {
    // Select the node
    document.querySelectorAll('.tree-node-header').forEach(h => h.classList.remove('selected'));
    const header = document.querySelector(`.tree-node-header[data-event-id="${eventId}"]`);
    if (header) header.classList.add('selected');

    selectNode(eventId);

    // Toggle if it has children
    if (hasChildren) {
        toggleNode(eventId);
    }
}

function renderTreeNode(node, depth = 0) {
    const hasChildren = node.children && node.children.length > 0;
    const typeClass = `type-${node.process_type}`;

    // Small glyphs (trace row density; avoid large emoji)
    let icon = '·';
    if (node.process_type === 'node') icon = '◇';
    if (node.process_type === 'llm_call') icon = '◆';
    if (node.process_type === 'tool_execution') icon = '▸';
    if (node.process_type === 'job_debug') icon = '◎';
    if (node.process_type === 'hitl_pause') icon = '⏸';
    if (node.process_type === 'hitl_resume') icon = '▶';

    // Status color (Icon ONLY)
    let iconStyle = 'color: var(--text-secondary);'; // Default gray/white
    if (node.status === 'error') iconStyle = 'color: var(--danger);';
    if (node.status === 'pending' && node.process_type === 'hitl_pause') iconStyle = 'color: var(--warning);';
    if (node.status === 'success' && node.process_type === 'tool_execution') iconStyle = 'color: var(--success);';
    if (node.status === 'success' && node.process_type === 'hitl_resume') iconStyle = 'color: var(--success);';

    // Toggle button (Chevron on Right)
    const toggleBtn = hasChildren
        ? `<span id="toggle-${node.event_id}" class="tree-toggle" style="margin-left: auto; color: var(--text-secondary);">▼</span>`
        : ``;

    // Indentation applied to padding to keep full-width clickability
    const paddingLeft = 10 + (depth * 15);

    // No second-line previews on the left: prompts, params, keys, and summaries
    // belong only in the detail pane (select a row).
    const snippetRow = '';

    const mod = escapeHtml(String(node.module || ''));

    return `
        <div class="tree-node">
            <div class="tree-node-header" 
                 data-event-id="${node.event_id}"
                 onclick="handleNodeClick('${node.event_id}', ${hasChildren})"
                 style="padding-left: ${paddingLeft}px; padding-right: 10px;">
                <div class="tree-node-header-top">
                    <span class="tree-icon" style="${iconStyle}">${icon}</span>
                    <span class="tree-badge ${typeClass}">${escapeHtml(String(node.operation || ''))}</span>
                    <span class="tree-label" style="color: var(--text-primary);">${mod}</span>
                    ${node.duration_ms ? `<span class="tree-duration">${node.duration_ms.toFixed(0)}ms</span>` : ''}
                    ${toggleBtn}
                </div>
                ${snippetRow}
            </div>
            ${hasChildren ? `
                <div id="children-${node.event_id}" style="display: block;">
                    ${node.children.map(c => renderTreeNode(c, depth + 1)).join('')}
                </div>
            ` : ''}
        </div>
    `;
}

function selectNode(eventId) {
    const event = eventMap[eventId];
    if (!event) return;

    // Store globally for modal access
    window.selectedEvent = event;
    const traceId = 'selectedEvent'; // Key in window

    const detail = document.getElementById('nodeDetail');
    if (!detail) return;

    let content = `
        <div class="trace-detail-header">
            <h3 class="trace-detail-title">${escapeHtml(event.module)}</h3>
            <div class="trace-detail-meta">
                <span class="badge ${event.process_type}">${escapeHtml(event.process_type)}</span>
                <span class="trace-chip">${escapeHtml(event.operation || '')}</span>
                ${event.duration_ms ? `<span class="trace-chip">${event.duration_ms.toFixed(0)}ms</span>` : ''}
                <span class="trace-chip trace-chip--muted">${formatTime(event.timestamp)}</span>
            </div>
        </div>
    `;

    const linkedSnapshots = snapshotsForEvent(eventId);
    if (linkedSnapshots.length) {
        snapshotInspectorUi.panel = 'agent_state';
        snapshotInspectorUi.view = 'table';
        snapshotInspectorUi.pairLabel = null;
        snapshotInspectorUi.showChangedOnly = true;
        content += `
            <div class="detail-section snapshot-detail-section">
                ${renderSnapshotInspectorHtml(linkedSnapshots, event, { title: 'State snapshots', count: linkedSnapshots.length })}
            </div>`;
    }

    // NEW: Display per-step Judge Metrics if present
    if (event.judge_metrics) {
        const metrics = event.judge_metrics;
        const logicColor = metrics.logic_score > 0.8 ? 'var(--success)' : (metrics.logic_score > 0.5 ? 'var(--warning)' : 'var(--danger)');
        const fidelityColor = metrics.fidelity_score > 0.8 ? 'var(--success)' : (metrics.fidelity_score > 0.5 ? 'var(--warning)' : 'var(--danger)');

        content += `
            <div class="detail-section" style="background: rgba(15, 23, 42, 0.3); padding: 12px; border-radius: var(--radius-md); margin-bottom: 16px;">
                <h4 style="margin: 0 0 10px 0; font-size: 0.85rem; color: var(--text-secondary);">⚖️ LLM Judge Evaluation</h4>
                <div style="display: flex; gap: 20px; margin-bottom: 10px;">
                    <div>
                        <span style="font-size: 0.75rem; color: var(--text-tertiary);">Logic Score</span>
                        <div style="font-size: 1.2rem; font-weight: 700; color: ${logicColor};">${(metrics.logic_score * 100).toFixed(0)}%</div>
                    </div>
                    <div>
                        <span style="font-size: 0.75rem; color: var(--text-tertiary);">Fidelity Score</span>
                        <div style="font-size: 1.2rem; font-weight: 700; color: ${fidelityColor};">${(metrics.fidelity_score * 100).toFixed(0)}%</div>
                    </div>
                </div>
                ${metrics.critique ? `<p style="font-size: 0.85rem; color: var(--text-secondary); margin: 0; border-top: 1px solid var(--border); padding-top: 8px;">${escapeHtml(metrics.critique)}</p>` : ''}
            </div>
        `;
    }

    // Render details based on type
    if (event.process_type === 'llm_call') {
        const routingHtml = event.module === 'output_verifier'
            ? formatVerifierRoutingHtml(event.parsed_output)
            : '';

        const hasRagContext = event.retrieved_examples && event.retrieved_examples.length > 0;
        const ragContextHtml = hasRagContext ? `
            <div class="prompt-section">
                <h5>
                    RAG Context (${event.retrieved_examples.length})
                    <button class="expand-btn" onclick="event.stopPropagation(); openModalFromTrace('${traceId}', 'retrieved_examples', 'RAG Context')">
                        ⟨⟩
                    </button>
                </h5>
                <pre class="code-block scrollable">${escapeHtml(JSON.stringify(event.retrieved_examples, null, 2).substring(0, 300))}${JSON.stringify(event.retrieved_examples, null, 2).length > 300 ? '...' : ''}</pre>
            </div>
        ` : '';

        content += `
            <div class="detail-section">
                <h4>Conf: ${formatConfidencePct(event.confidence_score)} | Tokens: ${event.input_tokens || 0} -> ${event.output_tokens || 0} | Model: ${event.model || 'N/A'}</h4>
            </div>
            ${routingHtml}
            
            <div class="vertical-layout" style="display: flex; flex-direction: column; gap: 20px;">
                <!-- TOP: Input (System + RAG + User) -->
                <div class="input-panel">
                    <div class="panel-title">📥 INPUT</div>
                    
                    <div class="prompt-section">
                        <h5>
                            System Prompt
                            <button class="expand-btn" onclick="event.stopPropagation(); openModalFromTrace('${traceId}', 'system_prompt', 'System Prompt')">
                                ⟨⟩
                            </button>
                        </h5>
                        <pre class="code-block scrollable" style="max-height: 300px;">${escapeHtml((event.system_prompt || 'N/A').substring(0, 800))}${(event.system_prompt || '').length > 800 ? '...' : ''}</pre>
                    </div>
                    
                    ${ragContextHtml}
                    
                    <div class="prompt-section">
                        <h5>
                            User Prompt
                            <button class="expand-btn" onclick="event.stopPropagation(); openModalFromTrace('${traceId}', 'user_prompt', 'User Prompt')">
                                ⟨⟩
                            </button>
                        </h5>
                        <pre class="code-block scrollable" style="max-height: 200px;">${escapeHtml((event.user_prompt || 'N/A').substring(0, 600))}${(event.user_prompt || '').length > 600 ? '...' : ''}</pre>
                    </div>
                </div>
                
                <!-- BOTTOM: Output (Response) -->
                <div class="output-panel">
                    <div class="panel-title">📤 OUTPUT</div>
                    
                    ${event.module === 'plan_generator' && event.parsed_output && event.parsed_output.tool_calls ? `
                    <div class="detail-section" style="margin-bottom: 16px;">
                        <h4>Executable plan (${event.parsed_output.tool_calls.length} steps)</h4>
                        <p class="block-inline-note" style="margin:0 0 8px">
                            This is what <code>execute_tools</code> runs after validation and post-processing.
                            The raw model JSON below may differ (e.g. <code>expand_grouped_block</code> vs <code>fill_merged</code>).
                        </p>
                        <pre class="code-block scrollable" style="max-height: 360px;">${safeJson(event.parsed_output.tool_calls)}</pre>
                    </div>
                    ` : ''}
                    <details class="debug-raw-response" ${event.module === 'output_verifier' ? '' : (event.module === 'plan_generator' ? '' : 'open')}>
                        <summary class="debug-raw-response-summary">Model raw JSON (verbatim)</summary>
                        <div class="prompt-section">
                            <h5>
                                Response
                                <button class="expand-btn" onclick="event.stopPropagation(); openModalFromTrace('${traceId}', 'raw_response', 'Response')">
                                    ⟨⟩
                                </button>
                            </h5>
                            <pre class="code-block scrollable" style="max-height: 400px;">${escapeHtml((event.raw_response || 'N/A'))}</pre>
                        </div>
                    </details>
                </div>
            </div>
        `;
    } else if (event.process_type === 'tool_execution') {
        const statusChipClass =
            event.status === 'success'
                ? 'trace-chip--success'
                : event.status === 'error'
                  ? 'trace-chip--error'
                  : '';
        const stageChip = event.pipeline_stage
            ? `<span class="trace-chip trace-chip--stage">${escapeHtml(String(event.pipeline_stage))}</span>`
            : '';
        const resultMsg = escapeHtml(event.result || 'No details available');

        let snapshotCard = '';
        if (event.snapshot_path) {
            const snapBase = String(event.snapshot_path).replace(/\\/g, '/').split('/').pop();
            const snapAttr = escapeHtml(snapBase);
            snapshotCard = `
                <div class="trace-summary-card trace-summary-card--snapshot">
                    <div class="trace-summary-card-head">
                        <span class="trace-summary-label">Data snapshot</span>
                        <div class="snapshot-action-row snapshot-action-row--inline">
                            <button type="button" class="btn btn-secondary btn-sm snapshot-btn-compact snapshot-preview-btn" data-snapshot-file="${snapAttr}">Preview</button>
                            <button type="button" class="btn btn-primary btn-sm snapshot-btn-compact snapshot-download-btn" data-snapshot-file="${snapAttr}">Download</button>
                        </div>
                    </div>
                    <p class="snapshot-action-note snapshot-action-note--inline">Preview: first sheet, up to 100 rows · Download: full file</p>
                </div>
            `;
        } else if (String(event.module || '').startsWith('collation.')) {
            snapshotCard = `
                <div class="trace-summary-card trace-summary-card--snapshot trace-summary-card--muted">
                    <span class="trace-summary-label">Data snapshot</span>
                    <p class="snapshot-action-note snapshot-action-note--inline">In-memory step only — no workbook snapshot.</p>
                </div>
            `;
        }

        content += `
            <div class="trace-tool-summary">
                ${snapshotCard}
                <div class="trace-summary-card trace-summary-card--status">
                    <div class="trace-summary-card-head">
                        <span class="trace-summary-label">Outcome</span>
                        <div class="trace-chip-row">
                            <span class="trace-chip ${statusChipClass}">${escapeHtml(event.status || '')}</span>
                            ${stageChip}
                        </div>
                    </div>
                    <p class="trace-result-line">${resultMsg}</p>
                </div>
            </div>
            <div class="trace-detail-panels">
                <div class="trace-detail-panel">
                    <h4 class="trace-panel-title">Parameters</h4>
                    <pre class="code-block trace-code-block">${safeJson(event.params || {})}</pre>
                </div>
        `;

        if (event.input_preview && Object.keys(event.input_preview).length > 0) {
            content += `
                <div class="trace-detail-panel">
                    <h4 class="trace-panel-title">Input preview</h4>
                    <pre class="code-block trace-code-block">${safeJson(event.input_preview)}</pre>
                </div>
            `;
        }

        if (event.output_preview && Object.keys(event.output_preview).length > 0) {
            content += `
                <div class="trace-detail-panel">
                    <h4 class="trace-panel-title">Output preview</h4>
                    <pre class="code-block trace-code-block">${safeJson(normalizeOutputPreviewForDisplay(event.output_preview))}</pre>
                </div>
            `;
        }

        content += `</div>`;

    } else if (event.process_type === 'hitl_pause' || event.process_type === 'hitl_resume') {
        const meta = event.metadata && typeof event.metadata === 'object' ? event.metadata : {};
        const checkpoints = Array.isArray(meta.checkpoints) ? meta.checkpoints : [];
        const reviewUrl = meta.review_url || '/review.html';
        const pauseLabel = event.process_type === 'hitl_pause' ? 'Human review pause' : 'Resume after review';
        const statusChip =
            event.status === 'pending'
                ? 'trace-chip--warning'
                : event.status === 'success' || event.status === 'approve'
                  ? 'trace-chip--success'
                  : '';

        let checkpointRows = '';
        if (checkpoints.length) {
            checkpointRows = checkpoints
                .map((cp) => {
                    const title = escapeHtml(String(cp.title || cp.checkpoint_type || cp.checkpoint_id || 'checkpoint'));
                    const ctype = escapeHtml(String(cp.checkpoint_type || ''));
                    const sev = escapeHtml(String(cp.severity || ''));
                    const resolved = cp.resolved ? 'yes' : 'no';
                    return `<tr><td>${title}</td><td>${ctype}</td><td>${sev}</td><td>${resolved}</td></tr>`;
                })
                .join('');
        }

        const deletionCount = meta.deletion_preview_count || 0;
        const lowConfCount = meta.low_confidence_count || 0;

        content += `
            <div class="trace-hitl-summary">
                <div class="trace-summary-card">
                    <div class="trace-summary-card-head">
                        <span class="trace-summary-label">${pauseLabel}</span>
                        <span class="trace-chip ${statusChip}">${escapeHtml(event.status || '')}</span>
                    </div>
                    <p class="trace-result-line">${escapeHtml(event.message || event.input_summary || '')}</p>
                    ${event.process_type === 'hitl_pause' ? `
                    <p class="block-inline-note" style="margin:8px 0 0">
                        Trigger node: <code>${escapeHtml(String(meta.trigger_node || ''))}</code>
                        · Checkpoints: ${checkpoints.length}
                        · Deletion previews: ${deletionCount}
                        · Low confidence: ${lowConfCount}
                    </p>
                    <a class="btn btn-secondary btn-sm" href="${escapeHtml(reviewUrl)}" style="margin-top:10px">Open Review</a>
                    ` : `
                    <p class="block-inline-note" style="margin:8px 0 0">
                        Action: <code>${escapeHtml(String(meta.action || ''))}</code>
                        · Checkpoint: <code>${escapeHtml(String(meta.checkpoint_id || ''))}</code>
                    </p>
                    `}
                </div>
                ${checkpointRows ? `
                <div class="detail-section">
                    <h4>Checkpoints</h4>
                    <table class="trace-inline-table" style="width:100%;font-size:12px">
                        <thead><tr><th>Title</th><th>Type</th><th>Severity</th><th>Resolved</th></tr></thead>
                        <tbody>${checkpointRows}</tbody>
                    </table>
                </div>` : ''}
                ${Array.isArray(meta.deletion_previews) && meta.deletion_previews.length ? `
                <div class="detail-section">
                    <h4>Deletion previews</h4>
                    <pre class="code-block trace-code-block">${safeJson(meta.deletion_previews)}</pre>
                </div>` : ''}
            </div>
        `;
    } else {
        // Generic node
        if (event.metadata && typeof event.metadata === 'object') {
            const metaKeys = Object.keys(event.metadata).filter(
                (k) => k !== 'tool_sequence' && k !== 'inline_pipeline_steps',
            );
            if (metaKeys.length) {
                const metaSubset = {};
                metaKeys.forEach((k) => {
                    metaSubset[k] = event.metadata[k];
                });
                content += `<div class="detail-section"><h4>Metadata</h4><pre class="code-block">${safeJson(metaSubset)}</pre></div>`;
            }
        }

        const inlineSteps = event.metadata && Array.isArray(event.metadata.inline_pipeline_steps)
            ? event.metadata.inline_pipeline_steps
            : [];
        if (inlineSteps.length) {
            const rowsHtml = inlineSteps
                .map((s) => {
                    const status = escapeHtml(String(s.status || ''));
                    const label = escapeHtml(String(s.label || s.id || 'step'));
                    const kind = escapeHtml(String(s.kind || ''));
                    const detail = s.detail != null && s.detail !== '' ? escapeHtml(String(s.detail)) : '—';
                    const badgeClass =
                        status === 'ok'
                            ? 'success'
                            : status === 'error'
                              ? 'error'
                              : status === 'skipped'
                                ? ''
                                : '';
                    return `
                        <tr>
                            <td style="vertical-align:top;padding:6px 10px 6px 0;font-family:system-ui,sans-serif">${label}</td>
                            <td style="vertical-align:top;padding:6px 8px 6px 0;white-space:nowrap"><span class="badge">${kind}</span></td>
                            <td style="vertical-align:top;padding:6px 8px 6px 0;white-space:nowrap"><span class="status-badge ${badgeClass}" style="font-size:11px">${status}</span></td>
                            <td style="vertical-align:top;padding:6px 0;color:var(--text-secondary);font-size:12px">${detail}</td>
                        </tr>`;
                })
                .join('');
            content += `
                <div class="detail-section">
                    <h4>Fixed pipeline steps</h4>
                    <p class="block-inline-note" style="margin:0 0 8px">Deterministic functions and bounded LLM calls that always run inside this graph node — <strong>not</strong> planner <code>suggested_tools</code> / <code>execute_tools</code> steps.</p>
                    <table style="width:100%;border-collapse:collapse;font-size:13px">
                        <thead><tr>
                            <th style="text-align:left;padding:4px 10px 4px 0;color:var(--text-tertiary)">Step</th>
                            <th style="text-align:left;padding:4px 8px;color:var(--text-tertiary)">Kind</th>
                            <th style="text-align:left;padding:4px 8px;color:var(--text-tertiary)">Status</th>
                            <th style="text-align:left;padding:4px 0;color:var(--text-tertiary)">Detail</th>
                        </tr></thead>
                        <tbody>${rowsHtml}</tbody>
                    </table>
                </div>`;
        }

        // Check for Tool Sequence metadata (e.g. from generate_plan)
        if (event.metadata && event.metadata.tool_sequence) {
            const sequence = event.metadata.tool_sequence;
            content += `
                <div class="detail-section">
                    <h4>Tool Sequence (${sequence.length})</h4>
                    <div style="display: flex; flex-direction: column; gap: 15px;">
             `;

            sequence.forEach((tool, idx) => {
                const fullName = String(tool.tool || 'Unknown Tool').trim();
                const shortName = toolExecutionShortLabel(fullName);
                const fullEsc = escapeHtml(fullName);
                const shortEsc = escapeHtml(shortName);
                content += `
                    <div style="background: var(--bg-secondary); border-radius: 4px; border: 1px solid var(--border); overflow: hidden;">
                        <div class="trace-card-header" style="background: var(--bg-secondary); padding: 8px 12px; font-weight: 600;">
                            <span title="${fullEsc}">${idx + 1}. ${shortEsc}</span>
                        </div>
                        <div style="padding: 10px;">
                            <div style="margin-bottom: 8px;">
                                <strong style="color: var(--text-secondary); font-size: 0.85em;">Reason:</strong>
                                <div style="margin-top: 4px;">${escapeHtml(tool.reason || 'No reason provided')}</div>
                            </div>
                            <div>
                                <strong style="color: var(--text-secondary); font-size: 0.85em;">Params:</strong>
                                <pre style="font-size: 0.75em; background: var(--bg-primary); padding: 5px; border-radius: 2px; margin-top: 4px;">${safeJson(tool.params || {})}</pre>
                            </div>
                        </div>
                    </div>
                 `;
            });

            content += `
                    </div>
                </div>
             `;
        }

        // Check for prompts (System + User)
        if (event.system_prompt || event.user_prompt) {
            content += `
                <div class="input-panel" style="margin-top: 20px;">
                    <div class="panel-title">📥 CONTEXT</div>
                    
                    ${event.system_prompt ? `
                    <div class="prompt-section">
                        <h5>
                            System Prompt
                            <button class="expand-btn" onclick="event.stopPropagation(); openModalFromTrace('${traceId}', 'system_prompt', 'System Prompt')">
                                ⟨⟩
                            </button>
                        </h5>
                        <pre class="code-block scrollable" style="max-height: 200px;">${escapeHtml(event.system_prompt.substring(0, 500))}${event.system_prompt.length > 500 ? '...' : ''}</pre>
                    </div>` : ''}
                    
                    ${event.user_prompt ? `
                    <div class="prompt-section">
                        <h5>
                            User Input
                            <button class="expand-btn" onclick="event.stopPropagation(); openModalFromTrace('${traceId}', 'user_prompt', 'User Input')">
                                ⟨⟩
                            </button>
                        </h5>
                        <pre class="code-block scrollable" style="max-height: 150px;">${escapeHtml(event.user_prompt.substring(0, 400))}${event.user_prompt.length > 400 ? '...' : ''}</pre>
                    </div>` : ''}
                </div>
            `;
        }

        if (event.input_summary) {
            content += `<div class="detail-section"><h4>Input</h4><pre class="code-block">${escapeHtml(event.input_summary)}</pre></div>`;
        }
        if (event.output_summary) {
            content += `<div class="detail-section"><h4>Output</h4><pre class="code-block">${escapeHtml(event.output_summary)}</pre></div>`;
        }
    }

    detail.innerHTML = content;
    detail.scrollTop = 0;
    wireSnapshotActionButtons(detail);
    const inspector = detail.querySelector('[data-snapshot-inspector]');
    if (inspector && linkedSnapshots.length) {
        wireSnapshotInspector(inspector, groupSnapshotPairs(linkedSnapshots), event);
    }
}

/** Last segment after `.` for tool execution headers (full id stays in `title`). */
function toolExecutionShortLabel(fullName) {
    const s = String(fullName || '').trim();
    if (!s) return '?';
    const dot = s.lastIndexOf('.');
    return dot >= 0 ? s.slice(dot + 1) : s;
}


// ===== New LLM Observer Trace Rendering =====

// ===== Tab 1: LLM Calls =====

function renderLLMCallsTab(traces, toolExecutions = []) {
    const tree = document.getElementById('traceTree');
    const detail = document.getElementById('nodeDetail');
    if (!tree || !detail) return;

    let html = '';

    // LLM Calls section
    if (traces.length > 0) {
        html += '<h4 style="color: var(--text-secondary); margin-bottom: 0.5rem;">🧠 LLM Calls</h4>';
        html += traces.map((t, i) => `
            <div class="llm-trace-card" onclick="showTraceDetail(${i});">
                <div class="trace-card-header">
                    <span class="trace-component">${t.component}</span>
                    <span class="trace-stats">
                        <span class="stat ${t.success ? 'success' : 'error'}">${t.success ? '✓' : '✗'}</span>
                        <span class="stat">${(t.latency_ms || 0).toFixed(0)}ms</span>
                        <span class="stat">${t.input_tokens || 0} in / ${t.output_tokens || 0} out</span>
                    </span>
                </div>
                <div class="trace-card-body">
                    <div class="trace-field">
                        <span class="field-label">Model:</span>
                        <span class="field-value">${t.model_id || 'N/A'}</span>
                    </div>
                </div>
            </div>
        `).join('');
    }

    // Tool Executions section
    if (toolExecutions.length > 0) {
        html += '<h4 style="color: var(--text-secondary); margin: 1rem 0 0.5rem;">🔧 Tool Executions</h4>';
        const stages = [
            ...new Set(
                toolExecutions
                    .map((x) => (x.pipeline_stage ? String(x.pipeline_stage).trim() : ''))
                    .filter(Boolean),
            ),
        ].sort();
        const anyMissingStage = toolExecutions.some((t) => !t.pipeline_stage);
        if (stages.length > 0 || anyMissingStage) {
            html += `<div class="tool-stage-filter" style="margin: 0 0 0.5rem 0; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                <label for="toolStageFilter" style="color: var(--text-secondary); font-size: 0.85rem;">Pipeline stage</label>
                <select id="toolStageFilter" aria-label="Filter tools by pipeline stage" style="max-width: 14rem;">
                    <option value="">All</option>
                    ${stages.map((s) => `<option value="${escapeHtml(s)}">${escapeHtml(s)}</option>`).join('')}
                    ${anyMissingStage ? '<option value="__unknown__">(none)</option>' : ''}
                </select>
            </div>`;
        }

        /** Consecutive tools with the same nominal stage → one band (reduces scroll noise). */
        const stageBands = [];
        for (let i = 0; i < toolExecutions.length; i++) {
            const row = toolExecutions[i];
            const ps = row.pipeline_stage ? String(row.pipeline_stage).trim() : '';
            const bandKey = ps || '__none__';
            const prev = stageBands[stageBands.length - 1];
            if (prev && prev.key === bandKey) {
                prev.items.push(row);
            } else {
                stageBands.push({
                    key: bandKey,
                    label: ps || '(no pipeline stage)',
                    items: [row],
                });
            }
        }

        let toolCardIdx = 0;
        const toolCardInner = (t) => {
            const idx = toolCardIdx;
            toolCardIdx += 1;
            const fullTool = String(t.tool || '').trim();
            const fullEsc = escapeHtml(fullTool);
            const shortEsc = escapeHtml(toolExecutionShortLabel(fullTool));
            const psEsc = escapeHtml(String(t.pipeline_stage || ''));
            return `
            <div class="llm-trace-card tool-card" data-pipeline-stage="${psEsc}" style="border-left: 3px solid ${t.success ? 'var(--success)' : 'var(--error)'};">
                <div class="trace-card-header" onclick="toggleToolDetail(${idx})">
                    <span class="trace-component" title="${fullEsc}">${shortEsc}</span>
                    <span class="trace-stats">
                        ${t.pipeline_stage ? `<span class="stat" style="opacity:0.85;">${escapeHtml(String(t.pipeline_stage))}</span>` : ''}
                        <span class="stat">${t.duration_ms != null ? `${Number(t.duration_ms).toFixed(0)}ms · ` : ''}${t.input_preview?.rows || '?'} → ${t.output_preview?.rows || '?'} rows</span>
                        ${t.snapshot_path ? `<span class="stat" style="cursor: pointer;" onclick="event.stopPropagation(); openDataPreview(${idx})" title="Preview Data">👁️</span>` : ''}
                        <span class="stat ${t.success ? 'success' : 'error'}">${t.success ? '✓' : '✗'}</span>
                    </span>
                </div>
                <div class="trace-card-body">
                    <div class="trace-field">
                        <span class="field-label">Result:</span>
                        <span class="field-value">${t.result || 'N/A'}</span>
                    </div>
                    <div class="tool-detail" id="tool-detail-${idx}" style="display: none; margin-top: 0.5rem; padding: 0.5rem; background: var(--bg-primary); border-radius: 4px;">
                        <div class="trace-field" style="margin-bottom: 0.35rem;">
                            <span class="field-label">Tool:</span>
                            <span class="field-value" style="font-family: monospace; font-size: 0.85em;">${fullEsc}</span>
                        </div>
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 1rem;">
                            <div>
                                <strong style="color: var(--warning);">📥 Input</strong>
                                <pre style="font-size: 0.75rem; margin: 0.25rem 0; overflow: auto; max-height: 150px;">${safeJson(t.input_preview)}</pre>
                            </div>
                            <div>
                                <strong style="color: var(--success);">📤 Output</strong>
                                <pre style="font-size: 0.75rem; margin: 0.25rem 0; overflow: auto; max-height: 150px;">${safeJson(normalizeOutputPreviewForDisplay(t.output_preview))}</pre>
                            </div>
                        </div>
                        <div style="margin-top: 0.5rem;">
                            <strong style="color: var(--text-secondary);">Params:</strong>
                            <pre style="font-size: 0.75rem; margin: 0.25rem 0;">${safeJson(t.params)}</pre>
                        </div>
                    </div>
                </div>
            </div>
        `;
        };

        html += stageBands
            .map((band) => {
                const headerLabelEsc = escapeHtml(band.label);
                const bodies = band.items.map((row) => toolCardInner(row)).join('');
                return `
            <div class="tool-stage-group" data-group-stage-filter="${band.key === '__none__' ? '__unknown__' : escapeHtml(String(band.label))}">
                <div class="tool-stage-band-hdr" style="font-size: 0.74rem; color: var(--text-secondary); margin: 0.45rem 0 0.2rem 0; letter-spacing: 0.03em;">
                    ${headerLabelEsc} · ${band.items.length}
                </div>
                <div class="tool-stage-group-body" style="display: flex; flex-direction: column; gap: 0.35rem;">
                    ${bodies}
                </div>
            </div>`;
            })
            .join('');

        // Store tool executions globally
        window.toolExecutionsData = toolExecutions;
    }

    if (!html) {
        tree.innerHTML = '<div class="empty-state">No LLM calls or tool executions recorded</div>';
        detail.innerHTML = '<div class="empty-state">Select a step to view details</div>';
        return;
    }

    tree.innerHTML = html;
    detail.innerHTML = '<div class="empty-state">Select a trace card to view details</div>';

    const stageSel = tree.querySelector('#toolStageFilter');
    if (stageSel) {
        stageSel.addEventListener('change', () => {
            const v = stageSel.value;
            if (!v) {
                tree.querySelectorAll('.tool-stage-group').forEach((g) => {
                    g.style.display = '';
                });
                tree.querySelectorAll('.tool-card').forEach((el) => {
                    el.style.display = '';
                });
                return;
            }
            tree.querySelectorAll('.tool-stage-group').forEach((group) => {
                let showGroup = false;
                group.querySelectorAll('.tool-card').forEach((el) => {
                    const ps = el.getAttribute('data-pipeline-stage') || '';
                    let show = false;
                    if (v === '__unknown__') show = !ps;
                    else show = ps === v;
                    el.style.display = show ? '' : 'none';
                    if (show) showGroup = true;
                });
                group.style.display = showGroup ? '' : 'none';
            });
        });
    }

    // Store traces globally for detail view
    window.llmTracesData = traces;
}

function renderLegacyLLMCalls(events) {
    const tree = document.getElementById('traceTree');
    if (!tree) return;

    const llmCalls = events.filter(e => e.process_type === 'llm_call');

    if (!llmCalls.length) {
        tree.innerHTML = '<div class="empty-state">No LLM calls recorded</div>';
        return;
    }

    tree.innerHTML = llmCalls.map(e => `
        <div class="llm-trace-card" onclick="selectNode('${e.event_id}')">
            <div class="trace-card-header">
                <span class="trace-component">${e.module}.${e.operation}</span>
                <span class="trace-stats">
                    ${e.duration_ms ? `<span class="stat">${e.duration_ms.toFixed(0)}ms</span>` : ''}
                </span>
            </div>
            <div class="trace-card-body">
                <div class="trace-field">
                    <span class="field-label">Input:</span>
                    <span class="field-value">${e.input_summary || 'N/A'}</span>
                </div>
                <div class="trace-field">
                    <span class="field-label">Output:</span>
                    <span class="field-value">${e.output_summary || 'N/A'}</span>
                </div>
            </div>
        </div>
    `).join('');
}



// ===== Tab 3: Run Summary =====

function aggregateLlmSummaryFromTraces(traces) {
    const rows = Array.isArray(traces) ? traces.filter((t) => t && typeof t === 'object') : [];
    if (!rows.length) return { total_calls: 0 };

    let totalLatencyMs = 0;
    let totalInputTokens = 0;
    let totalOutputTokens = 0;
    const confidences = [];
    const models = new Set();

    rows.forEach((row) => {
        const ms = row.latency_ms ?? row.duration_ms ?? 0;
        totalLatencyMs += Number(ms) || 0;
        totalInputTokens += Number(row.input_tokens) || 0;
        totalOutputTokens += Number(row.output_tokens) || 0;
        confidences.push(Number(row.confidence_score) || 0);
        const model = row.model_id || row.model;
        if (model) models.add(String(model));
    });

    const modelList = [...models];
    let modelId = '';
    if (modelList.length === 1) {
        modelId = modelList[0];
    } else if (modelList.length > 1) {
        modelId = `${modelList[0]} (+${modelList.length - 1} more)`;
    }

    return {
        model_id: modelId,
        total_calls: rows.length,
        total_latency_ms: totalLatencyMs,
        total_input_tokens: totalInputTokens,
        total_output_tokens: totalOutputTokens,
        avg_confidence: confidences.length
            ? confidences.reduce((a, b) => a + b, 0) / confidences.length
            : 0,
    };
}

function resolveRunSummary(llmSummary) {
    const fromTraces = aggregateLlmSummaryFromTraces(jobData?.llm_traces);
    if ((fromTraces.total_calls || 0) > 0) {
        return { ...(llmSummary || {}), ...fromTraces };
    }
    return llmSummary || {};
}

function renderRunSummary(llmSummary, trace) {
    llmSummary = resolveRunSummary(llmSummary || {});

    // Model
    const modelEl = document.getElementById('metaModel');
    if (modelEl) modelEl.textContent = llmSummary.model_id || trace.metadata?.model_id || 'N/A';

    // Total Calls
    const callsEl = document.getElementById('metaTotalCalls');
    if (callsEl) callsEl.textContent = llmSummary.total_calls || 0;

    // Total Latency
    const latencyEl = document.getElementById('metaLatency');
    if (latencyEl) {
        const latencyMs = llmSummary.total_latency_ms || 0;
        latencyEl.textContent = latencyMs > 1000 ? `${(latencyMs / 1000).toFixed(2)}s` : `${latencyMs.toFixed(0)}ms`;
    }

    // Tokens
    const tokensEl = document.getElementById('metaTokens');
    if (tokensEl) {
        const inTokens = llmSummary.total_input_tokens || 0;
        const outTokens = llmSummary.total_output_tokens || 0;
        tokensEl.textContent = `${inTokens} in / ${outTokens} out`;
    }

    // Confidence
    const confEl = document.getElementById('metaConfidence');
    if (confEl) {
        const avgConf = llmSummary.avg_confidence || 0;
        confEl.textContent = `${(avgConf * 100).toFixed(1)}%`;
    }

    // Debug
    const debugEl = document.getElementById('metaDebug');
    if (debugEl) debugEl.textContent = 'Yes';

    const finalPreviewEl = document.getElementById('finalOutputPreview');
    if (finalPreviewEl) {
        const previewRows = jobData?.data_preview || [];
        finalPreviewEl.innerHTML = renderFinalOutputPreview(
            previewRows,
            resolvePreviewColumnOrder(previewRows, jobData?.data_preview_column_order, jobData),
        );
    }
}

function renderFinalOutputPreview(rows, columnOrder) {
    if (!Array.isArray(rows) || rows.length === 0) {
        return '<div class="empty-state">No final output preview available yet.</div>';
    }
    const headers = resolvePreviewColumnOrder(rows, columnOrder, null);
    return `
        <table style="border-collapse: collapse; font-size: 0.75rem; white-space: nowrap;">
            <thead>
                <tr style="background: var(--bg-secondary);">
                    ${headers.map(h => `<th style="padding: 0.4rem 0.6rem; border: 1px solid var(--border); text-align: left; font-weight: 600;">${escapeHtml(String(h))}</th>`).join('')}
                </tr>
            </thead>
            <tbody>
                ${rows.slice(0, 20).map(row => `
                    <tr>
                        ${headers.map(h => `<td style="padding: 0.3rem 0.6rem; border: 1px solid var(--border);">${escapeHtml(String(row[h] ?? ''))}</td>`).join('')}
                    </tr>
                `).join('')}
            </tbody>
        </table>
        <p style="margin-top: 0.5rem; color: var(--text-muted); font-size: 0.75rem;">Showing ${Math.min(rows.length, 20)} preview row(s) from the latest finalized output.</p>
    `;
}

// Legacy functions kept for backward compatibility


function toggleToolDetail(index) {
    const detail = document.getElementById(`tool-detail-${index}`);
    if (detail) {
        detail.style.display = detail.style.display === 'none' ? 'block' : 'none';
    }
}

function openDataPreview(index) {
    const t = window.toolExecutionsData?.[index];
    if (!t) return;

    const preview = t.output_preview || {};
    const sample = preview.sample || [];
    const snapshotFile = t.snapshot_path ? t.snapshot_path.split(/[\\/]/).pop() : null;

    // Build table HTML from sample data
    let tableHtml = '';
    if (sample.length > 0) {
        // Use column_order if available (preserves original order), fallback to Object.keys
        const headers = preview.column_order && preview.column_order.length > 0
            ? preview.column_order
            : Object.keys(sample[0]);

        // Helper to format cell values (dates, numbers, etc.)
        const formatValue = (val) => {
            if (val === null || val === undefined) return '';
            // Format datetime strings (remove time portion)
            if (typeof val === 'string' && val.match(/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/)) {
                return val.split('T')[0].split(' ')[0];
            }
            return String(val);
        };

        tableHtml = `
            <table style="border-collapse: collapse; font-size: 0.75rem; white-space: nowrap;">
                <thead>
                    <tr style="background: var(--bg-secondary);">
                        ${headers.map(h => `<th style="padding: 0.4rem 0.6rem; border: 1px solid var(--border); text-align: left; font-weight: 600;">${String(h).substring(0, 20)}</th>`).join('')}
                    </tr>
                </thead>
                <tbody>
                    ${sample.map(row => `
                        <tr>
                            ${headers.map(h => `<td style="padding: 0.3rem 0.6rem; border: 1px solid var(--border);">${formatValue(row[h])}</td>`).join('')}
                        </tr>
                    `).join('')}
                </tbody>
            </table>
        `;
    } else {
        tableHtml = '<p style="color: var(--text-muted);">No sample data available</p>';
    }

    const content = `
        <div style="margin-bottom: 0.75rem; color: var(--text-muted); font-size: 0.8rem;">
            This is an intermediate snapshot captured immediately after <strong>${escapeHtml(t.tool || 'this step')}</strong>. The final output preview is shown in the Run Summary tab.
        </div>
        <div style="overflow: auto; max-height: 450px;">
            ${tableHtml}
        </div>
        <p style="margin-top: 0.5rem; color: var(--text-muted); font-size: 0.75rem;">Showing ${sample.length} of ${preview.rows || 0} rows</p>
    `;

    // Custom modal with stats in header
    document.getElementById('modalTitle').innerHTML = `${t.tool} <span style="font-weight: normal; font-size: 0.85rem; color: var(--text-muted);">(${preview.rows || 0} × ${preview.columns || 0})</span>`;
    const modalEl = document.getElementById('modalContent');
    modalEl.innerHTML = content;

    const modalHeader = document.querySelector('.modal-header');
    document.getElementById('modalSnapshotActions')?.remove();
    if (modalHeader && snapshotFile) {
        const wrap = document.createElement('div');
        wrap.id = 'modalSnapshotActions';
        wrap.style.cssText = 'display:inline-flex;gap:0.5rem;margin-right:0.5rem;align-items:center;';
        const previewBtn = document.createElement('button');
        previewBtn.type = 'button';
        previewBtn.className = 'btn btn-secondary btn-sm';
        previewBtn.textContent = '👁 Preview workbook';
        previewBtn.onclick = () => previewSnapshotWorkbook(snapshotFile);
        const dlBtn = document.createElement('button');
        dlBtn.type = 'button';
        dlBtn.className = 'btn btn-primary btn-sm';
        dlBtn.textContent = '📥 Download';
        dlBtn.onclick = () => downloadSnapshot(snapshotFile);
        wrap.appendChild(previewBtn);
        wrap.appendChild(dlBtn);
        const actions = modalHeader.querySelector('.modal-actions');
        const firstActionBtn = actions?.querySelector('button');
        if (firstActionBtn) firstActionBtn.parentNode.insertBefore(wrap, firstActionBtn);
        else modalHeader.appendChild(wrap);
    }

    document.getElementById('modalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}


function showTraceDetail(index) {
    const t = window.llmTracesData?.[index];
    if (!t) return;

    const detail = document.getElementById('nodeDetail');
    detail.innerHTML = `
        <div class="detail-section">
            <h4>${t.component}</h4>
            <p class="detail-type">${t.model_id} | ${t.prompt_version}</p>
        </div>
        <div class="detail-section">
            <strong>Latency:</strong> ${t.latency_ms.toFixed(0)}ms
        </div>
        <div class="detail-section">
            <strong>Tokens:</strong> ${t.input_tokens} in / ${t.output_tokens} out
        </div>
        <div class="detail-section">
            <strong>Confidence:</strong> ${formatConfidencePct(t.confidence_score)}
        </div>
        ${t.system_prompt ? `
            <div class="detail-section">
                <strong>System Prompt:</strong>
                <pre class="code-block">${escapeHtml(t.system_prompt.substring(0, 200))}...</pre>
            </div>
        ` : ''}
        ${t.raw_response ? `
            <div class="detail-section">
                <strong>Response:</strong>
                <pre class="code-block">${escapeHtml(t.raw_response.substring(0, 300))}...</pre>
            </div>
        ` : ''}
    `;
}

// ===== Modal Functions for Expanding Content =====

function openModal(title, content, isHtml = false) {
    document.getElementById('modalTitle').textContent = title;
    const modalEl = document.getElementById('modalContent');
    if (isHtml) {
        modalEl.classList.remove('modal-content-plain');
        modalEl.innerHTML = content;
    } else {
        modalEl.classList.add('modal-content-plain');
        modalEl.textContent = content;
    }
    document.getElementById('modalOverlay').classList.add('active');
    document.body.style.overflow = 'hidden';
}

function closeModal() {
    document.getElementById('modalSnapshotActions')?.remove();
    const modalEl = document.getElementById('modalContent');
    if (modalEl) modalEl.classList.remove('modal-content-plain');
    document.getElementById('modalOverlay').classList.remove('active');
    document.body.style.overflow = '';
}

function copyModalContent() {
    const content = document.getElementById('modalContent').textContent;
    navigator.clipboard.writeText(content).then(() => {
        showToast('Content copied to clipboard', 'success');
    }).catch(err => {
        showToast('Failed to copy', 'error');
    });
}

// Close modal on Escape key
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        closeModal();
    }
});

// ===== Enhanced Prompt Assembly with Expand Buttons =====

// Helper to open modal with trace data
function openModalFromTrace(traceId, field, title) {
    const trace = window[traceId];
    if (trace) {
        openModal(title + ' - ' + (trace.component || trace.module), trace[field] || 'N/A');
    }
}
