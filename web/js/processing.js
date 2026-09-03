/**
 * Console page (live run status + log). Downloads live under Results (debug.html).
 */

let pollInterval = null;
let pipelineStartInFlight = false;
/** True after we trigger POST /api/process (sessionStorage autostart or first eligible poll). */
let pipelineAutoStartAttempted = false;

function escapeHtmlProcessing(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function hideTransformationPhaseBanner() {
    const el = document.getElementById('transformationPhaseBanner');
    if (!el) return;
    el.hidden = true;
    el.innerHTML = '';
}

/** Render macro phases + optional capped merge-region list from structure scan (same as Review structural card). */
function renderTransformationPhaseBanner(banner, structureHints) {
    const el = document.getElementById('transformationPhaseBanner');
    if (!el) return;
    const phases = banner && Array.isArray(banner.phases) ? banner.phases : [];
    const h = structureHints && typeof structureHints === 'object' ? structureHints : {};
    const prev = Array.isArray(h.merged_regions_preview) ? h.merged_regions_preview : [];
    const hasMergePreview = prev.length > 0;

    const total = Number(h.merged_regions_total || 0);
    const omitted = Number(h.merged_regions_omitted || 0);
    let mergedBlock = '';
    if (hasMergePreview) {
        const sheetLine = h.sheet_name
            ? `<p class="phase-struct-subline">${escapeHtmlProcessing(String(h.sheet_name))}${
                  h.merged_cell_signals ? ` · ${escapeHtmlProcessing(String(h.merged_cell_signals))} merge signal(s)` : ''
              }</p>`
            : '';
        const items = prev
            .map((m) => {
                if (!m || typeof m !== 'object') return '';
                const r = escapeHtmlProcessing(String(m.range ?? ''));
                const sp =
                    m.rows_spanned != null && m.cols_spanned != null
                        ? `${Number(m.rows_spanned)}×${Number(m.cols_spanned)}`
                        : '';
                const tv =
                    m.top_left_value != null && String(m.top_left_value).trim() !== ''
                        ? escapeHtmlProcessing(String(m.top_left_value).slice(0, 72))
                        : '';
                return `<li><code>${r}</code>${sp ? ` <span class="phase-struct-muted">(${sp})</span>` : ''}${
                    tv ? ` — <span class="phase-struct-muted">${tv}</span>` : ''
                }</li>`;
            })
            .filter(Boolean)
            .join('');
        const more =
            omitted > 0
                ? `<li class="phase-struct-muted">…and ${escapeHtmlProcessing(String(omitted))} more (full list in Review → Structural → Advanced JSON if paused)</li>`
                : '';
        mergedBlock = `
            <details class="phase-struct-merge-details">
                <summary>Excel merged ranges sampled (${total || prev.length})</summary>
                ${sheetLine}
                <ul class="phase-struct-merge-list">${items}${more}</ul>
            </details>`;
    }

    if (!phases.length && !hasMergePreview) {
        hideTransformationPhaseBanner();
        return;
    }
    el.hidden = false;

    if (!phases.length && hasMergePreview) {
        el.innerHTML = `
            <p class="phase-banner-heading">Structure scan (preview)</p>
            ${mergedBlock}
        `;
        return;
    }

    const rationaleBlock =
        banner.show_structure_phase && banner.rationale && banner.rationale.length
            ? `<div class="phase-banner-rationale"><strong>Structure phase:</strong> ${escapeHtmlProcessing(
                  banner.rationale.join('; ')
              )}</div>`
            : '';

    const chips = phases
        .map((p) => {
            const st = escapeHtmlProcessing(p.status || 'pending');
            const short = escapeHtmlProcessing(p.short || '');
            const title = escapeHtmlProcessing(p.title || '');
            const hint = escapeHtmlProcessing(p.hint || '');
            return `<div class="phase-chip" data-status="${st}" role="status">
                <div class="phase-chip-short">${short}</div>
                <div class="phase-chip-title">${title}</div>
                <div class="phase-chip-hint">${hint}</div>
            </div>`;
        })
        .join('');

    el.innerHTML = `
        <p class="phase-banner-heading">Transformation phases (overview)</p>
        <div class="phase-chip-row">${chips}</div>
        ${rationaleBlock}
        ${mergedBlock}
    `;
}

function hideUnionReadinessBanner() {
    const el = document.getElementById('unionReadinessBanner');
    if (!el) return;
    el.hidden = true;
    el.textContent = '';
}

function showUnionReadinessBanner(report) {
    const el = document.getElementById('unionReadinessBanner');
    if (!el) return;
    const warns = report.warnings || [];
    if (!warns.length) {
        hideUnionReadinessBanner();
        return;
    }
    el.hidden = false;
    el.className = report.blocking ? 'matrix-conflict-banner' : 'mapping-reuse-banner';
    const codes = warns.map((w) => w.code || 'NOTE').join(', ');
    el.innerHTML = `<strong>Union readiness</strong> — ${warns.length} note(s): ${escapeHtmlProcessing(codes)}`;
}

function buildProcessingLogSteps(data) {
    const steps = Array.isArray(data?.steps) ? data.steps : [];
    const debugEvents = Array.isArray(data?.job_debug_events) ? data.job_debug_events : [];
    const rawUploadEvents = debugEvents
        .filter((event) => {
            const phase = String(event?.phase || '');
            const label = String(event?.label || '');
            return (
                phase === 'upload' &&
                ['Data file uploaded', 'Target template uploaded', 'Upload inventory'].includes(label)
            );
        });
    const hasConcreteDataUpload = rawUploadEvents.some((event) => String(event?.label || '') === 'Data file uploaded');
    const uploadEvents = rawUploadEvents
        .filter((event) => !hasConcreteDataUpload || String(event?.label || '') !== 'Upload inventory')
        .map((event) => ({
            step: event.label || 'Upload',
            message: event.input_summary || event.output_summary || event.label || 'Upload recorded',
            timestamp: event.timestamp,
            _source: 'job_debug',
        }));

    const seen = new Set();
    return [...uploadEvents, ...steps]
        .filter((step) => {
            const key = [step.timestamp || '', step.step || '', step.message || ''].join('|');
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        })
        .sort((a, b) => {
            const at = a.timestamp ? Date.parse(a.timestamp) : Number.MAX_SAFE_INTEGER;
            const bt = b.timestamp ? Date.parse(b.timestamp) : Number.MAX_SAFE_INTEGER;
            return (Number.isFinite(at) ? at : Number.MAX_SAFE_INTEGER) - (Number.isFinite(bt) ? bt : Number.MAX_SAFE_INTEGER);
        });
}

async function maybeFetchUnionReadiness(jobId) {
    try {
        const vr = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/validate-cross-source-union`);
        if (!vr.ok) return null;
        return await vr.json();
    } catch {
        return null;
    }
}

function maybeAutoStartPipeline(jobId, data) {
    if (!jobId || pipelineAutoStartAttempted || pipelineStartInFlight) return;
    const s = String(data.status || '').toLowerCase();
    if (['processing', 'queued', 'completed', 'awaiting_review', 'awaiting_approval', 'error'].includes(s)) return;
    pipelineAutoStartAttempted = true;
    void runDataPreparationPipeline(jobId);
}

document.addEventListener('DOMContentLoaded', () => {
    const currentJob = getCurrentJob();
    const jobId = currentJob && currentJob.jobId;

    if (!jobId) {
        showToast('No job selected. Please upload a file first.', 'warning');
        setTimeout(() => window.location.href = '/pages/upload.html', 2000);
        return;
    }

    document.getElementById('jobId').textContent = jobId;
    document.getElementById('downloadProcessingLog')?.addEventListener('click', (event) => {
        event.stopPropagation();
    });

    try {
        if (sessionStorage.getItem('schemaAgentAutostartProcess') === '1') {
            sessionStorage.removeItem('schemaAgentAutostartProcess');
            pipelineAutoStartAttempted = true;
            void runDataPreparationPipeline(jobId);
        }
    } catch {
        /* ignore */
    }

    startPolling(jobId);
});

async function pollJobTick(jobId) {
    const data = await getJobStatus(jobId);
    updateUI(data, jobId);

    const path = (typeof window !== 'undefined' && window.location && window.location.pathname) || '';
    const onReviewPage = path.endsWith('review.html');

    if (data.status === 'awaiting_review' || data.status === 'awaiting_approval') {
        clearInterval(pollInterval);
        pollInterval = null;
        if (typeof updateNavStatus === 'function') updateNavStatus();
        if (!onReviewPage) navigateTo('review');
        return;
    }

    if (data.status === 'completed' || data.status === 'error') {
        clearInterval(pollInterval);
        pollInterval = null;
        if (typeof updateNavStatus === 'function') updateNavStatus();
    }
}

function startPolling(jobId) {
    pollInterval = setInterval(() => {
        pollJobTick(jobId).catch((e) => console.error('Polling error:', e));
    }, 1000);
    void pollJobTick(jobId).catch((e) => console.error('Polling error:', e));
}

async function runDataPreparationPipeline(jobId) {
    const { sheetName, sourceId } = getCurrentJob();
    pipelineStartInFlight = true;
    try {
        const report = await maybeFetchUnionReadiness(jobId);
        if (report) {
            const warns = report.warnings || [];
            if (warns.length > 0) {
                showUnionReadinessBanner(report);
                showToast(
                    report.blocking
                        ? `Union readiness: ${warns.length} issue(s). Review column overlap across sources before relying on a combined output.`
                        : `Union readiness: ${warns.length} note(s).`,
                    report.blocking ? 'warning' : 'info',
                );
            } else {
                hideUnionReadinessBanner();
            }
        }

        const body = { process_all_sources: true };
        if (sheetName) body.sheet_name = sheetName;
        if (sourceId) body.source_id = sourceId;
        const res = await fetch(`/api/process/${encodeURIComponent(jobId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const out = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(out.error || `Request failed (${res.status})`);
        if (out.error) throw new Error(out.error);

        if (out.async && out.status === 'processing') {
            showToast('Run started — log updates below.', 'info');
            await pollJobTick(jobId);
        } else if (out.status === 'awaiting_review' || out.status === 'awaiting_approval') {
            showToast('Paused for human review.', 'warning');
            routeJobByState(out);
        } else {
            showToast('Run finished.', 'success');
            routeJobByState(out);
        }
    } catch (e) {
        showToast(`Could not start run: ${e.message}`, 'error');
    } finally {
        pipelineStartInFlight = false;
        try {
            const data = await getJobStatus(jobId);
            updateUI(data, jobId);
        } catch (_) {
            /* ignore */
        }
    }
}

function updateActiveContextChip(data) {
    const chip = document.getElementById('activeContextChip');
    if (!chip) return;
    const scope = data?.active_context_scope;
    const text = scope?.chip || '';
    if (data?.status === 'processing' && text) {
        chip.hidden = false;
        chip.textContent = text;
        chip.title = scope?.source_id ? `Active source: ${scope.source_id}` : '';
    } else {
        chip.hidden = true;
        chip.textContent = '';
        chip.removeAttribute('title');
    }
}

function updateUI(data, jobIdOpt) {
    const jobId = jobIdOpt || (getCurrentJob() && getCurrentJob().jobId);
    const steps = buildProcessingLogSteps(data);
    const status = data.status;

    const panel = document.getElementById('statusPanel');
    panel.classList.remove('success', 'error', 'warning');
    updateActiveContextChip(data);
    const icon = panel.querySelector('.status-icon');
    const title = panel.querySelector('.status-title');
    const content = panel.querySelector('.status-content');

    const logDetails = document.getElementById('processingLogDetails');
    if (logDetails) {
        if (status === 'completed' || status === 'error') {
            logDetails.open = false;
            logDetails.removeAttribute('open');
        } else if (status === 'processing') {
            logDetails.open = true;
        }
    }
    const downloadLog = document.getElementById('downloadProcessingLog');
    if (downloadLog && jobId) {
        const hasLog = Boolean(data?._download_availability?.processing_log) || steps.length > 0;
        downloadLog.hidden = !hasLog;
        downloadLog.href = `/api/download/${encodeURIComponent(jobId)}/processing-log`;
    }

    if (status === 'completed') {
        panel.classList.add('success');
        icon.textContent = '✅';
        title.textContent = 'Run Complete';
        content.innerHTML = `<p>Processed ${data.total_rows || 0} rows with ${(data.schema?.fields || []).length} fields.</p>
            <p class="status-results-hint">Downloads are on <strong>4 · Results</strong>.</p>`;
    } else if (status === 'awaiting_review' || status === 'awaiting_approval') {
        panel.classList.add('warning');
        icon.textContent = '🧑';
        title.textContent = 'Waiting For Review';
        content.innerHTML = `
            <p>${data.review_reason || 'The workflow is paused for a human review step.'}</p>
            <div style="margin-top: 12px;">
                <button class="btn-primary" onclick="window.location.href='/pages/review.html'">Open Review Queue</button>
            </div>
        `;
    } else if (status === 'error') {
        panel.classList.add('error');
        icon.textContent = '❌';
        title.textContent = 'Run Failed';
        content.innerHTML = `<p>${data.error_details || 'Unknown error'}</p>`;
    } else if (status === 'processing') {
        icon.textContent = '⏳';
        title.textContent = 'Running…';
        const lastStep = steps[steps.length - 1];
        const hb = data.processing_heartbeat_at ? Date.parse(data.processing_heartbeat_at) : NaN;
        const ageMs =
            Number.isFinite(hb) ? Date.now() - hb : NaN;
        const staleNote =
            Number.isFinite(ageMs) && ageMs > 600000
                ? `<p class="processing-stale-hint" role="status">Limited updates for ~${Math.max(
                      1,
                      Math.round(ageMs / 60000)
                  )} min (replan/verify/Judge LLM runs can take several minutes). If this grows past ~20 minutes, refresh or check server logs.</p>`
                : '';
        content.innerHTML = `<p>${lastStep?.message || 'Working...'}</p>${staleNote}`;
    } else {
        maybeAutoStartPipeline(jobId, data);
        icon.textContent = '⏳';
        title.textContent = 'Starting…';
        content.innerHTML =
            '<p>A run is started automatically from this page when the job is idle. Partial jobs are allowed; finish schema mapping for every source when you want full coverage.</p>';
    }

    const logEl = document.getElementById('logTimeline');
    const esc = (s) =>
        String(s ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
    logEl.innerHTML =
        steps
            .map((step) => {
                const ts = step.timestamp ? formatTime(step.timestamp) : '';
                const lvl = step.step === 'Error' ? 'ERROR' : 'INFO';
                const line = [ts, lvl, step.step, step.message].filter(Boolean).join(' | ');
                return `<div class="log-entry log-entry--terminal ${step.step === 'Error' ? 'error' : ''}">${esc(line)}</div>`;
            })
            .join('') || '<div class="log-empty">No logs yet</div>';

    renderTransformationPhaseBanner(data.transformation_phase_banner || {}, data.structure_transform_hints || {});

    const scrollWrap = document.getElementById('logTimelineScroll');
    if (scrollWrap && (status === 'processing' || logDetails?.open)) {
        requestAnimationFrame(() => {
            scrollWrap.scrollTop = scrollWrap.scrollHeight;
        });
    }
}
