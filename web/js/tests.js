/**
 * Tests Page - Schema Agent
 */

let checkpointStagesCache = [];
const checkpointJsonStore = new Map();

document.addEventListener('DOMContentLoaded', () => {
    initTests();
    initCheckpointExport();
    fetchTests();
});

function initTests() {
    const runAllBtn = document.getElementById('runAllTests');
    if (runAllBtn) {
        runAllBtn.addEventListener('click', runAllTests);
    }
    const closeLogBtn = document.getElementById('closeTestLog');
    if (closeLogBtn) {
        closeLogBtn.addEventListener('click', () => {
            document.getElementById('testLogContainer').classList.add('hidden');
        });
    }
}

function initCheckpointExport() {
    const loadBtn = document.getElementById('checkpointLoadBtn');
    const downloadBtn = document.getElementById('checkpointDownloadBtn');
    const openApiBtn = document.getElementById('checkpointOpenApiBtn');
    const jobSelect = document.getElementById('checkpointJobSelect');
    const jobInput = document.getElementById('checkpointJobId');

    if (loadBtn) loadBtn.addEventListener('click', () => loadCheckpointExport(false));
    if (downloadBtn) downloadBtn.addEventListener('click', () => downloadCheckpointExport());
    if (openApiBtn) openApiBtn.addEventListener('click', () => openCheckpointApiUrl());

    if (jobSelect) {
        jobSelect.addEventListener('change', () => {
            const id = jobSelect.value || '';
            if (jobInput) jobInput.value = id;
            if (id) populateCheckpointSources(id);
        });
    }
    if (jobInput) {
        jobInput.addEventListener('change', () => {
            const id = (jobInput.value || '').trim();
            if (id) populateCheckpointSources(id);
        });
    }

    loadCheckpointStageOptions();
    populateCheckpointJobs();
}

function resolveCheckpointJobId() {
    const manual = (document.getElementById('checkpointJobId')?.value || '').trim();
    const selected = (document.getElementById('checkpointJobSelect')?.value || '').trim();
    return manual || selected || (typeof appState !== 'undefined' && appState.currentJobId) || '';
}

function buildCheckpointQueryParams() {
    const params = new URLSearchParams();
    const sourceId = (document.getElementById('checkpointSourceSelect')?.value || '').trim();
    const fullPacket = document.getElementById('checkpointFullPacket')?.checked;
    if (sourceId) params.set('source_id', sourceId);
    if (fullPacket) params.set('full_packet', 'true');
    return params;
}

function checkpointApiPath(jobId, stageId) {
    const base = stageId
        ? `/api/debug/${encodeURIComponent(jobId)}/checkpoints/${encodeURIComponent(stageId)}`
        : `/api/debug/${encodeURIComponent(jobId)}/checkpoints`;
    const qs = buildCheckpointQueryParams().toString();
    return qs ? `${base}?${qs}` : base;
}

async function loadCheckpointStageOptions() {
    try {
        const data = await fetchJson('/api/tests/checkpoints/stages');
        checkpointStagesCache = data.stages || [];
        const sel = document.getElementById('checkpointStageSelect');
        if (!sel) return;
        const opts = ['<option value="">All stages</option>'];
        checkpointStagesCache.forEach((st) => {
            opts.push(
                `<option value="${escapeHtml(st.stage_id)}">${st.order}. ${escapeHtml(st.title)}</option>`
            );
        });
        sel.innerHTML = opts.join('');
    } catch (err) {
        console.warn('Could not load checkpoint stages:', err);
    }
}

async function populateCheckpointJobs() {
    const sel = document.getElementById('checkpointJobSelect');
    const jobInput = document.getElementById('checkpointJobId');
    if (!sel) return;
    try {
        const data = await fetchJson('/api/jobs');
        const jobs = data.jobs || [];
        const current = typeof appState !== 'undefined' ? appState.currentJobId : '';
        const options = ['<option value="">— Select job —</option>'];
        jobs.forEach((j) => {
            const id = j.job_id || '';
            const label = `${id} · ${j.filename || 'file'} · ${j.status || 'unknown'}`;
            const selected = id === current ? ' selected' : '';
            options.push(`<option value="${escapeHtml(id)}"${selected}>${escapeHtml(label)}</option>`);
        });
        sel.innerHTML = options.join('');
        if (current && jobInput && !jobInput.value) {
            jobInput.value = current;
            await populateCheckpointSources(current);
        }
    } catch (err) {
        console.warn('Could not load jobs for checkpoint export:', err);
    }
}

async function populateCheckpointSources(jobId) {
    const sel = document.getElementById('checkpointSourceSelect');
    if (!sel || !jobId) return;
    sel.innerHTML = '<option value="">— Default first source —</option>';
    try {
        const status = await fetchJson(`/api/status/${encodeURIComponent(jobId)}`);
        const registry = status.source_registry || [];
        const currentSid = typeof appState !== 'undefined' ? appState.currentSourceId : '';
        registry.forEach((row) => {
            if (!row || !row.source_id) return;
            const sid = String(row.source_id);
            const sheet = row.sheet_name || sid.split(':').pop();
            const isRef = /validation|readme|rules/i.test(sheet);
            const tag = isRef ? ' · reference/metadata' : '';
            const selected = sid === currentSid ? ' selected' : '';
            sel.innerHTML += `<option value="${escapeHtml(sid)}"${selected}>${escapeHtml(sheet)}${escapeHtml(tag)}</option>`;
        });
    } catch (err) {
        console.warn('Could not load sources:', err);
    }
}

function _jsonValueClass(value) {
    if (value === null) return 'jv-null';
    if (typeof value === 'string') return 'jv-string';
    if (typeof value === 'number') return 'jv-number';
    if (typeof value === 'boolean') return 'jv-bool';
    return '';
}

function _jsonScalarHtml(value) {
    if (value === null) return '<span class="jv-null">null</span>';
    if (typeof value === 'string') return `<span class="jv-string">"${escapeHtml(value)}"</span>`;
    if (typeof value === 'number') return `<span class="jv-number">${escapeHtml(String(value))}</span>`;
    if (typeof value === 'boolean') return `<span class="jv-bool">${value ? 'true' : 'false'}</span>`;
    return escapeHtml(String(value));
}

function renderCollapsibleJson(value, keyLabel, openDepth) {
    const depth = openDepth == null ? 1 : openDepth;
    const keyHtml = keyLabel != null
        ? `<span class="jv-key">${escapeHtml(String(keyLabel))}</span>: `
        : '';

    if (value === null || typeof value !== 'object') {
        return `<div class="jv-leaf">${keyHtml}${_jsonScalarHtml(value)}</div>`;
    }

    const isArr = Array.isArray(value);
    const entries = isArr
        ? value.map((v, i) => [String(i), v])
        : Object.entries(value);
    const meta = isArr ? `Array(${entries.length})` : `Object{${entries.length}}`;
    const openAttr = depth > 0 ? ' open' : '';
    const children = entries
        .map(([k, v]) => renderCollapsibleJson(v, isArr ? `[${k}]` : k, depth - 1))
        .join('');

    return `
        <details${openAttr}>
            <summary>${keyHtml}<span class="jv-meta">${meta}</span></summary>
            <div>${children || '<div class="jv-leaf"><span class="jv-meta">empty</span></div>'}</div>
        </details>
    `;
}

function renderCheckpointJsonPanel(bundle, stageId) {
    const safeId = String(stageId || 'stage').replace(/[^a-zA-Z0-9_-]/g, '_');
    const panelId = `cp-json-${safeId}`;
    checkpointJsonStore.set(panelId, JSON.stringify(bundle, null, 2));
    const treeHtml = renderCollapsibleJson(bundle, null, 2);
    return `
        <div class="checkpoint-json-toolbar">
            <button type="button" class="btn-secondary btn-small cp-json-expand" data-panel="${panelId}">Expand all</button>
            <button type="button" class="btn-secondary btn-small cp-json-collapse" data-panel="${panelId}">Collapse all</button>
            <button type="button" class="btn-secondary btn-small cp-json-copy" data-panel="${panelId}">Copy JSON</button>
        </div>
        <div class="checkpoint-json-tree" id="${panelId}">
            ${treeHtml}
        </div>
    `;
}

function wireCheckpointJsonPanels(root) {
    if (!root) return;
    root.querySelectorAll('.cp-json-expand').forEach((btn) => {
        btn.addEventListener('click', () => {
            const panel = document.getElementById(btn.dataset.panel);
            if (!panel) return;
            panel.querySelectorAll('details').forEach((d) => {
                d.open = true;
            });
        });
    });
    root.querySelectorAll('.cp-json-collapse').forEach((btn) => {
        btn.addEventListener('click', () => {
            const panel = document.getElementById(btn.dataset.panel);
            if (!panel) return;
            panel.querySelectorAll('details').forEach((d) => {
                d.open = false;
            });
        });
    });
    root.querySelectorAll('.cp-json-copy').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const panelId = btn.dataset.panel;
            const text = checkpointJsonStore.get(panelId) || '';
            try {
                await navigator.clipboard.writeText(text);
                showToast('JSON copied', 'success');
            } catch (err) {
                console.error(err);
                showToast('Copy failed', 'error');
            }
        });
    });
}

function renderCheckpointResults(payload) {
    const wrap = document.getElementById('checkpointResults');
    const summary = document.getElementById('checkpointSummary');
    const list = document.getElementById('checkpointStageList');
    if (!wrap || !summary || !list) return;

    const checkpoints = payload.checkpoints || [];
    const ok = checkpoints.filter((c) => c.status === 'ok').length;
    summary.textContent =
        `Job ${payload.job_id} · source ${payload.source_id || '(default)'} · ` +
        `${checkpoints.length} stage(s) · ${ok} with data · ` +
        `${payload.llm_trace_count || 0} LLM trace(s) · ${payload.state_snapshot_count || 0} snapshot(s)`;

    checkpointJsonStore.clear();
    list.innerHTML = checkpoints
        .map((cp, idx) => {
            const status = cp.status || 'missing';
            const llm = cp.llm_trace?.component ? ` · LLM: ${cp.llm_trace.component}` : '';
            const snap = cp.state_snapshot?.label ? ` · snap: ${cp.state_snapshot.label}` : '';
            const ctxSrc = cp.context_packet?.context_source ? ` · ctx: ${cp.context_packet.context_source}` : '';
            const scopeBadge = cp.scope === 'job'
                ? '<span class="checkpoint-scope">workbook</span>'
                : '<span class="checkpoint-scope">per source</span>';
            const scopeNote = cp.scope_note
                ? `<p class="checkpoint-meta-line">${escapeHtml(cp.scope_note)}</p>`
                : '';
            const bundle = {
                stage_id: cp.stage_id,
                title: cp.title,
                description: cp.description,
                scope: cp.scope,
                filter_source_id: cp.filter_source_id,
                status: cp.status,
                context_packet: cp.context_packet,
                state_snapshot: cp.state_snapshot,
                llm_trace: cp.llm_trace,
                job_slice: cp.job_slice,
                context_diff_tail: cp.context_diff_tail,
                carried_forward: cp.carried_forward,
            };
            return `
                <details class="checkpoint-stage-card"${idx === 0 ? ' open' : ''}>
                    <summary>
                        <span>${cp.order || ''}. ${escapeHtml(cp.title || cp.stage_id)}${scopeBadge}</span>
                        <span class="checkpoint-status ${escapeHtml(status)}">${escapeHtml(status)}${escapeHtml(llm)}${escapeHtml(snap)}${escapeHtml(ctxSrc)}</span>
                    </summary>
                    <p style="margin:8px 0;font-size:0.82rem;color:var(--text-secondary);">${escapeHtml(cp.description || '')}</p>
                    ${scopeNote}
                    ${renderCheckpointJsonPanel(bundle, `${cp.stage_id || 'stage'}-${idx}`)}
                </details>
            `;
        })
        .join('');

    wireCheckpointJsonPanels(list);
    wrap.classList.remove('hidden');
}

async function loadCheckpointExport(silent) {
    const jobId = resolveCheckpointJobId();
    if (!jobId) {
        if (!silent) showToast('Enter or select a job ID', 'error');
        return null;
    }
    const stageId = (document.getElementById('checkpointStageSelect')?.value || '').trim();
    const loadBtn = document.getElementById('checkpointLoadBtn');
    if (loadBtn) {
        loadBtn.disabled = true;
        loadBtn.textContent = 'Loading…';
    }
    try {
        const url = checkpointApiPath(jobId, stageId || null);
        const payload = await fetchJson(url);
        if (payload.error && stageId) {
            showToast(payload.error, 'error');
            return null;
        }
        renderCheckpointResults(payload);
        if (!silent) showToast('Checkpoint export loaded', 'success');
        return payload;
    } catch (err) {
        console.error('Checkpoint export failed:', err);
        const hint =
            err.status === 404
                ? 'Endpoint not found — restart the web server to load new routes.'
                : err.message || 'Request failed';
        showToast(hint, 'error');
        return null;
    } finally {
        if (loadBtn) {
            loadBtn.disabled = false;
            loadBtn.textContent = 'Load checkpoints';
        }
    }
}

function downloadCheckpointExport() {
    const jobId = resolveCheckpointJobId();
    if (!jobId) {
        showToast('Enter or select a job ID', 'error');
        return;
    }
    const stageId = (document.getElementById('checkpointStageSelect')?.value || '').trim();
    const params = buildCheckpointQueryParams();
    if (stageId) params.set('stage', stageId);
    const qs = params.toString();
    const url = `/api/download/${encodeURIComponent(jobId)}/checkpoints-json${qs ? `?${qs}` : ''}`;
    window.location.href = url;
}

function openCheckpointApiUrl() {
    const jobId = resolveCheckpointJobId();
    if (!jobId) {
        showToast('Enter or select a job ID', 'error');
        return;
    }
    const stageId = (document.getElementById('checkpointStageSelect')?.value || '').trim();
    window.open(checkpointApiPath(jobId, stageId || null), '_blank', 'noopener');
}

async function fetchTests() {
    try {
        const data = await fetchJson('/api/tests/list');
        if (data.success) {
            renderTestCards(data.tests);
        }
    } catch (error) {
        console.error('Error fetching tests:', error);
        showToast('Failed to load tests', 'error');
    }
}

function renderTestCards(tests) {
    const grid = document.getElementById('testGrid');
    if (!grid) return;

    grid.innerHTML = tests.map(test => `
        <div class="test-card" id="test-card-${test.id}">
            <div class="test-header">
                <h3 class="test-title">${test.name}</h3>
                <span class="test-badge pending">Pending</span>
            </div>
            <p class="test-description">${test.description}</p>
            <div class="test-meta">
                <div class="test-meta-item">📁 ${test.file_path.split('/').pop()}</div>
                ${test.expected_tools.length ? `<div class="test-meta-item">🛠️ ${test.expected_tools.join(', ')}</div>` : ''}
            </div>
            <div class="test-footer">
                <div class="test-result-summary">-</div>
                <button class="btn-secondary btn-small run-single-test" data-id="${test.id}">Run Test</button>
            </div>
        </div>
    `).join('');

    document.querySelectorAll('.run-single-test').forEach(btn => {
        btn.addEventListener('click', () => runSingleTest(btn.dataset.id));
    });
}

async function runSingleTest(testId) {
    const card = document.getElementById(`test-card-${testId}`);
    const badge = card.querySelector('.test-badge');
    const resultSummary = card.querySelector('.test-result-summary');
    const btn = card.querySelector('.run-single-test');
    const logContainer = document.getElementById('testLogContainer');
    const log = document.getElementById('testLog');

    badge.className = 'test-badge running';
    badge.textContent = 'Running';
    btn.disabled = true;

    log.textContent = `[${new Date().toLocaleTimeString()}] Starting test: ${testId}...\n`;
    logContainer.classList.remove('hidden');

    try {
        const data = await fetchJson(`/api/tests/run/${testId}`, { method: 'POST' });

        if (data.success) {
            badge.className = `test-badge ${data.status}`;
            badge.textContent = data.status;
            resultSummary.textContent = `${data.rows_extracted || 0} rows | ${data.duration || 0}s`;

            log.textContent += `[${new Date().toLocaleTimeString()}] Result: ${data.status.toUpperCase()}\n`;
            log.textContent += `[${new Date().toLocaleTimeString()}] Message: ${data.message}\n`;
            log.textContent += `[${new Date().toLocaleTimeString()}] Confidence: ${(data.confidence * 100).toFixed(1)}%\n`;
            log.textContent += `[${new Date().toLocaleTimeString()}] Tools: ${(data.tools_used || []).join(', ')}\n`;
        } else {
            badge.className = 'test-badge error';
            badge.textContent = 'Error';
            resultSummary.textContent = 'Failed';
            log.textContent += `[${new Date().toLocaleTimeString()}] ERROR: ${data.error || 'Unknown error'}\n`;
        }
    } catch (error) {
        console.error('Error running test:', error);
        badge.className = 'test-badge error';
        badge.textContent = 'Error';
        log.textContent += `[${new Date().toLocaleTimeString()}] FATAL ERROR: ${error.message}\n`;
    } finally {
        btn.disabled = false;
        log.scrollTop = log.scrollHeight;
    }
}

async function runAllTests() {
    const btns = document.querySelectorAll('.run-single-test');
    const runAllBtn = document.getElementById('runAllTests');
    runAllBtn.disabled = true;
    runAllBtn.textContent = 'Running All...';

    for (const btn of btns) {
        await runSingleTest(btn.dataset.id);
    }

    runAllBtn.disabled = false;
    runAllBtn.textContent = 'Run All Tests';
}
