/**
 * Quality Evals — Tier 1 pipeline scorecard + Tier 3 LLM judge
 */

function escapeJsString(value) {
    return String(value ?? '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

const EVALS_STAGE_ORDER = [
    'structure', 'plan', 'plan_review', 'execution', 'context_isolation', 'collation',
];

const EVALS_STAGE_LABELS = {
    structure: 'Structure analysis',
    plan: 'Plan contract',
    plan_review: 'Plan review (HITL)',
    execution: 'Tool execution',
    context_isolation: 'Context isolation',
    collation: 'Multi-source collation',
};

const EVALS_STAGE_METRIC_LABELS = {
    layout_scan_score: 'Layout scan score',
    sparse_dimension_precision: 'Sparse-dimension precision',
    analyzer_confidence: 'Analyzer confidence',
    sparse_dimension_false_positive_count: 'Sparse-dimension false positives',
    sparse_dimension_flagged_count: 'Sparse columns flagged',
    block_sparse_metric_count: 'Block-sparse metrics',
    tool_contract_score: 'Tool contract score',
    final_tool_contract_score: 'Final tool contract',
    raw_tool_contract_score: 'Raw tool contract',
    necessity_score: 'Review necessity',
    deferral_ok: 'Grain deferral OK',
    grain_tools_deferred: 'Deferred grain tools',
    grain_tools_executed: 'Grain tools ran early',
    verifier_issue_count: 'Verifier issues',
    argument_correctness_score: 'Argument correctness',
    argument_bad_ref_count: 'Bad column references',
    plan_adherence_score: 'Invocation adherence',
    plan_adherence_score_set: 'Tool-name adherence (set)',
    invocation_adherence_score: 'Invocation adherence',
    invocation_mismatch_count: 'Invocation mismatches',
    invocation_extra_count: 'Extra invocations',
    invocation_missing_count: 'Missing invocations',
    unplanned_executed_tools: 'Unplanned tools run',
    missing_planned_tools: 'Planned tools not run',
};

const EVALS_STAGE_METRIC_KEYS = {
    structure: ['layout_scan_score', 'sparse_dimension_precision', 'analyzer_confidence', 'sparse_dimension_false_positive_count'],
    plan: ['final_tool_contract_score', 'tool_contract_score'],
    plan_review: ['necessity_score'],
    execution: ['deferral_ok', 'grain_tools_deferred', 'grain_tools_executed', 'verifier_issue_count', 'invocation_adherence_score', 'plan_adherence_score'],
    context_isolation: [],
    collation: ['union_false_duplicates'],
};

/**
 * Canonical per-stage eval spec — the single source of truth for what each
 * pipeline stage measures, which DeepEval agentic metric it maps to, and what
 * is deliberately NOT applicable at that stage. Mirrors the stage/measure/skip
 * design so the UI shows "just the right" metrics per stage.
 */
const EVALS_STAGE_SPEC = {
    structure: {
        role: 'Reads the sheet and emits structure analysis (blocks, columns, sparse signals). No tools are chosen here.',
        measures: [
            { key: 'layout_scan_score', label: 'Layout scan', kind: 'score' },
            { key: 'sparse_dimension_precision', label: 'Sparse-dimension precision', kind: 'score' },
            { key: 'analyzer_confidence', label: 'Analyzer confidence', kind: 'score' },
            { key: 'sparse_dimension_false_positive_count', label: 'Sparse false positives', kind: 'count' },
        ],
        skip: ['Tool correctness — no tools chosen at this stage', 'Plan adherence — no plan exists yet'],
    },
    plan: {
        role: 'Chooses the ordered tool calls and their arguments.',
        measures: [
            { key: 'final_tool_contract_score', label: 'Plan contract (deterministic)', engine: 'Deterministic', kind: 'score' },
            { key: 'argument_correctness_score', label: 'Argument correctness (deterministic)', engine: 'Deterministic', kind: 'score' },
        ],
        skip: ['Plan adherence — measured at execution', 'Task completion — DeepEval GEval at finalize'],
    },
    plan_review: {
        role: 'Judges the analyst approval questions the agent raised — were they necessary, or did it interrupt you for nothing?',
        measures: [
            { key: 'necessity_score', label: 'Question necessity', kind: 'score' },
        ],
        skip: ['Tool / plan metrics — the plan is already fixed; this stage only judges the questions'],
    },
    execution: {
        role: 'Runs the approved plan deterministically.',
        measures: [
            { key: 'invocation_adherence_score', label: 'Invocation adherence (deterministic)', engine: 'Deterministic', kind: 'score' },
            { key: 'plan_adherence_score', label: 'Invocation adherence', engine: 'Deterministic', kind: 'score' },
            { key: 'deferral_ok', label: 'Deferral correctness', kind: 'bool' },
            { key: 'invocation_extra_count', label: 'Extra invocations', kind: 'count' },
            { key: 'invocation_mismatch_count', label: 'Invocation mismatches', kind: 'count' },
            { key: 'verifier_issue_count', label: 'Verifier issues', kind: 'count' },
        ],
        skip: ['Plan contract — validated at plan time', 'Task completion — DeepEval GEval at finalize'],
    },
    context_isolation: {
        role: 'Ensures each sheet stays scoped to its own context (no cross-sheet bleed). App-specific.',
        measures: [],
        skip: [],
        useContextHealth: true,
    },
    collation: {
        role: 'Combines sources and checks cross-source duplicates and isolation. Job-level only.',
        measures: [
            { key: 'union_false_duplicates', label: 'Union false duplicates', kind: 'count' },
        ],
        skip: ['Per-source metrics — this stage is cross-source by nature'],
    },
};

function formatEvalMetricValue(key, val) {
    if (val === null || val === undefined) return '—';
    if (typeof val === 'boolean') return val ? 'yes' : 'no';
    if (Array.isArray(val)) return val.length ? val.join(', ') : '—';
    if (typeof val === 'number' && (key.endsWith('_score') || key.endsWith('_precision') || key === 'analyzer_confidence')) {
        if (val >= 0 && val <= 1) return `${Math.round(val * 100)}%`;
    }
    return String(val);
}

function renderStageMeasureRow(measure, metrics) {
    const v = metrics ? metrics[measure.key] : undefined;
    const measured = v !== undefined && v !== null && v !== '';
    const sub = measure.deepeval
        ? `<span class="evals-measure-deepeval">DeepEval · ${escapeHtml(measure.deepeval)}</span>`
        : measure.engine
            ? `<span class="evals-measure-engine muted">${escapeHtml(measure.engine)}</span>`
            : '';
    let valueHtml;
    let warn = false;
    if (!measured) {
        valueHtml = '<span class="evals-measure-value evals-measure-value--na">not measured</span>';
    } else {
        const text = formatEvalMetricValue(measure.key, v);
        if (measure.kind === 'score' && typeof v === 'number') {
            warn = v < 0.7;
        } else if (measure.kind === 'count' && typeof v === 'number') {
            warn = v > 0;
        } else if (measure.kind === 'bool') {
            warn = v === false;
        }
        valueHtml = `<span class="evals-measure-value ${warn ? 'value-low' : 'value-high'}">${escapeHtml(text)}</span>`;
    }
    return `
        <div class="evals-measure-row${warn ? ' evals-measure-row--warn' : ''}">
            <div class="evals-measure-head">
                <span class="evals-measure-label">${escapeHtml(measure.label)}</span>
                ${sub}
            </div>
            ${valueHtml}
        </div>`;
}

function renderStageMetricsPanel(rollup, ctxHealth) {
    if (!rollup) return '';
    const spec = EVALS_STAGE_SPEC[rollup.stage];
    const metrics = rollup.metrics || {};
    if (!spec) {
        return '';
    }

    const roleLine = spec.role
        ? `<p class="evals-stage-role muted">${escapeHtml(spec.role)}</p>`
        : '';

    let verdictNote = '';
    if (rollup.stage === 'plan_review') {
        const items = Number(metrics.items || 0);
        const noiseCount = Array.isArray(metrics.noise) ? metrics.noise.length : 0;
        const missCount = Array.isArray(metrics.misses) ? metrics.misses.length : 0;
        let text;
        let tone = 'ok';
        if (!items) {
            text = 'No analyst review questions were raised — the agent proceeded without interrupting you.';
        } else {
            const necessary = Math.max(0, items - noiseCount);
            text = `${necessary} of ${items} review question${items === 1 ? '' : 's'} were necessary.`;
            if (noiseCount) {
                text += ` ${noiseCount} flagged as unnecessary (asked you to review something that didn't need it).`;
                tone = 'warn';
            }
            if (missCount) {
                text += ` ${missCount} expected question${missCount === 1 ? '' : 's'} missing.`;
                tone = 'warn';
            }
        }
        verdictNote = `<p class="evals-planreview-verdict evals-planreview-verdict--${tone}">${escapeHtml(text)}</p>`;
    }

    let measuresHtml = (spec.measures || [])
        .map((m) => renderStageMeasureRow(m, metrics))
        .join('');

    // Context isolation stage draws from the job-level context health rollup.
    if (spec.useContextHealth && ctxHealth && ctxHealth.sources_total) {
        const pct = ctxHealth.pass_pct != null ? `${ctxHealth.pass_pct}%` : '—';
        const warn = (ctxHealth.bleed_count || 0) > 0;
        measuresHtml += `
            <div class="evals-measure-row${warn ? ' evals-measure-row--warn' : ''}">
                <div class="evals-measure-head">
                    <span class="evals-measure-label">Isolation pass rate</span>
                </div>
                <span class="evals-measure-value ${warn ? 'value-low' : 'value-high'}">${escapeHtml(pct)}</span>
            </div>
            <div class="evals-measure-row">
                <div class="evals-measure-head"><span class="evals-measure-label">Bleed signals</span></div>
                <span class="evals-measure-value ${warn ? 'value-low' : 'value-high'}">${ctxHealth.bleed_count || 0}</span>
            </div>`;
    }

    const measuresBlock = measuresHtml
        ? `<div class="evals-stage-measures"><h5 class="evals-stage-metrics-title">Measures</h5>${measuresHtml}</div>`
        : '<p class="muted">No stage-level measures recorded.</p>';

    const skipBlock = (spec.skip && spec.skip.length)
        ? `<div class="evals-stage-skip">
            <h5 class="evals-stage-metrics-title">Not applicable here</h5>
            <ul class="evals-stage-skip-list">
                ${spec.skip.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}
            </ul>
           </div>`
        : '';

    return `<div class="evals-stage-detail">${roleLine}${verdictNote}${measuresBlock}${skipBlock}</div>`;
}

function stageCardScoreSnippet(rollup) {
    const m = rollup?.metrics || {};
    if (rollup?.stage === 'structure' && m.layout_scan_score != null) {
        return `<span class="evals-stage-score">${Math.round(Number(m.layout_scan_score) * 100)}%</span>`;
    }
    if (rollup?.stage === 'plan' && m.final_tool_contract_score != null) {
        return `<span class="evals-stage-score">${Math.round(Number(m.final_tool_contract_score) * 100)}%</span>`;
    }
    if (rollup?.stage === 'plan_review' && m.necessity_score != null) {
        return `<span class="evals-stage-score">${Math.round(Number(m.necessity_score) * 100)}%</span>`;
    }
    if (rollup?.stage === 'execution' && m.plan_adherence_score != null) {
        return `<span class="evals-stage-score">${Math.round(Number(m.plan_adherence_score) * 100)}%</span>`;
    }
    return '';
}

const EVALS_SUBSCORE_ORDER = ['task_completion', 'plan_quality', 'context_quality', 'step_efficiency'];

const EVALS_SUBSCORE_LABELS = {
    task_completion: 'Task completion',
    plan_quality: 'Plan quality',
    context_quality: 'Context',
    step_efficiency: 'Efficiency',
};

const EVALS_VERDICT_LABELS = {
    blocked: 'BLOCKED',
    advisory: 'ADVISORY',
    pass: 'PASS',
    pending: 'PENDING',
};

let evalsState = {
    jobs: [],
    filter: 'all',
    search: '',
    selectedIndex: -1,
    expandedStage: null,
    expandedSources: {},
};

function bootEvals() {
    initEvals();
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootEvals);
} else {
    bootEvals();
}

function initEvals() {
    document.getElementById('refreshEvals')?.addEventListener('click', loadEvals);
    document.getElementById('evalsSearch')?.addEventListener('input', (e) => {
        evalsState.search = String(e.target.value || '').trim().toLowerCase();
        renderEvalQueue();
    });
    document.querySelectorAll('.evals-filter-chip').forEach((chip) => {
        chip.addEventListener('click', () => {
            document.querySelectorAll('.evals-filter-chip').forEach((c) => c.classList.remove('is-active'));
            chip.classList.add('is-active');
            evalsState.filter = chip.dataset.filter || 'all';
            renderEvalQueue();
        });
    });
    loadEvals();
}

async function loadEvals() {
    const grid = document.getElementById('evalsGrid');
    if (!grid) return;
    try {
        grid.innerHTML = '<div class="empty-state compact"><p>Fetching evaluation results…</p></div>';
        const response = await fetchJson('/api/evals');
        evalsState.jobs = response.evals || [];
        window.evalsJobs = evalsState.jobs;

        if (evalsState.jobs.length === 0) {
            grid.innerHTML = '<div class="empty-state compact"><p>No jobs with eval data yet. Process a workbook through the pipeline first.</p></div>';
            return;
        }

        const prevId = evalsState.selectedIndex >= 0 ? evalsState.jobs[evalsState.selectedIndex]?.job_id : null;
        renderEvalQueue();
        if (prevId) {
            const idx = filteredEvalJobs().findIndex((j) => j.job_id === prevId);
            if (idx >= 0) selectJob(idx);
            else if (filteredEvalJobs().length) selectJob(0);
        } else if (filteredEvalJobs().length) {
            selectJob(0);
        }
    } catch (e) {
        console.error('Failed to load evals:', e);
        showToast('Failed to load evaluation results', 'error');
        grid.innerHTML = `<div class="empty-state compact"><p class="error">Error: ${escapeHtml(e.message)}</p></div>`;
    }
}

function getEvalDisplay(job) {
    if (job?.eval_display) return job.eval_display;
    return buildEvalDisplayFallback(job);
}

function buildEvalDisplayFallback(job) {
    const pe = job?.pipeline_evals || {};
    const gate = pe.critical_gate || {};
    const issues = [];
    const per = pe.per_source || {};
    Object.entries(per).forEach(([sid, row]) => {
        EVALS_STAGE_ORDER.forEach((stage) => {
            if (stage === 'collation') return;
            const st = row?.[stage];
            (st?.violations || []).forEach((v) => {
                const sev = v.severity === 'critical' ? 'critical' : 'advisory';
                issues.push({ severity: sev, stage, type: v.type, message: v.message, source_id: sid, sheet_name: sid });
            });
        });
    });
    (pe.collation?.violations || []).forEach((v) => {
        issues.push({ severity: 'critical', stage: 'collation', type: v.type, message: v.message, source_id: '', sheet_name: '' });
    });
    const critical = issues.filter((i) => i.severity === 'critical').length;
    const advisory = issues.filter((i) => i.severity === 'advisory').length;
    let verdict = 'pending';
    if (!pe || (!Object.keys(per).length && !pe.collation)) verdict = 'pending';
    else if (critical && !gate.overridden) verdict = 'blocked';
    else if (advisory) verdict = 'advisory';
    else verdict = 'pass';
    return {
        verdict,
        headline: verdict === 'pass' ? 'All checks passed' : `${critical} critical, ${advisory} advisory`,
        export_safe: gate.pass !== false,
        overridden: !!gate.overridden,
        counts: { critical, advisory, sources: Object.keys(per).length },
        stage_rollups: [],
        issues,
        source_labels: {},
        recommended_actions: ['open_debug'],
        next_steps: [],
    };
}

function filteredEvalJobs() {
    const { jobs, filter, search } = evalsState;
    return jobs.filter((job) => {
        const d = getEvalDisplay(job);
        const v = d.verdict || 'pending';
        if (filter !== 'all' && v !== filter) return false;
        if (search) {
            const hay = `${job.filename || ''} ${job.job_id || ''}`.toLowerCase();
            if (!hay.includes(search)) return false;
        }
        return true;
    });
}

function verdictChipClass(verdict) {
    switch (verdict) {
        case 'blocked': return 'evals-verdict-blocked';
        case 'advisory': return 'evals-verdict-advisory';
        case 'pass': return 'evals-verdict-pass';
        default: return 'evals-verdict-pending';
    }
}

function updateFilterCounts() {
    const bar = document.getElementById('evalsFilterBar');
    if (!bar) return;
    const counts = { all: 0, blocked: 0, advisory: 0, pass: 0, pending: 0 };
    (evalsState.jobs || []).forEach((job) => {
        const v = getEvalDisplay(job).verdict || 'pending';
        counts.all += 1;
        if (counts[v] != null) counts[v] += 1;
    });
    bar.querySelectorAll('.evals-filter-chip').forEach((chip) => {
        const f = chip.dataset.filter || 'all';
        const n = counts[f] ?? 0;
        let base = chip.dataset.label;
        if (!base) {
            base = chip.textContent.replace(/\s*\d+$/, '').trim();
            chip.dataset.label = base;
        }
        chip.innerHTML = `${escapeHtml(base)}<span class="evals-filter-count">${n}</span>`;
        chip.classList.toggle('is-empty', n === 0 && f !== 'all');
    });
}

function renderEvalQueue() {
    const grid = document.getElementById('evalsGrid');
    if (!grid) return;
    updateFilterCounts();
    const jobs = filteredEvalJobs();
    if (!jobs.length) {
        grid.innerHTML = '<div class="empty-state compact"><p>No runs match this filter.</p></div>';
        return;
    }
    grid.innerHTML = jobs.map((job, index) => {
        const d = getEvalDisplay(job);
        const verdict = d.verdict || 'pending';
        const selected = evalsState.selectedIndex === index ? ' is-selected' : '';
        const srcCount = d.counts?.sources || 0;
        const q = d.quality || {};
        const qOverall = q.overall != null ? `${q.overall}` : '—';
        const band = q.band || (verdict === 'pass' ? 'good' : 'fair');
        return `
            <button type="button" class="evals-strip-card ${verdictChipClass(verdict)}${selected}" data-job-index="${index}" onclick="selectJob(${index})" title="${escapeHtml(job.filename || 'Untitled')}">
                <span class="evals-strip-score ${qualityBandClass(band)}">${escapeHtml(qOverall)}</span>
                <span class="evals-strip-body">
                    <span class="evals-strip-title">${escapeHtml(job.filename || 'Untitled')}</span>
                    <span class="evals-strip-meta">
                        <span class="evals-strip-dot ${verdictChipClass(verdict)}"></span>
                        ${escapeHtml(EVALS_VERDICT_LABELS[verdict] || verdict.toUpperCase())}${srcCount ? ` · ${srcCount} src` : ''} · ${escapeHtml(formatTime(job.created_at))}
                    </span>
                </span>
            </button>
        `;
    }).join('');
}

function selectJob(jobIndex) {
    const jobs = filteredEvalJobs();
    const job = jobs[jobIndex];
    if (!job) return;
    evalsState.selectedIndex = jobIndex;
    evalsState.expandedStage = null;
    document.querySelectorAll('.evals-strip-card').forEach((c) => c.classList.remove('is-selected'));
    document.querySelector(`.evals-strip-card[data-job-index="${jobIndex}"]`)?.classList.add('is-selected');
    renderScorecard(job);
}

function renderScorecard(job) {
    const panel = document.getElementById('evalsDetailPanel');
    if (!panel) return;
    const d = getEvalDisplay(job);
    const judge = job.judge_result;

    panel.innerHTML = `
        <div class="evals-scorecard-top">
            ${renderQualityHeader(d, job, judge)}
        </div>
        ${renderNextSteps(d, job)}
        ${renderStageStepper(d, job.job_id)}
        ${renderIssuesPanel(d, job.job_id)}
        ${renderSourceAccordion(d, job)}
    `;
}

function qualityBandClass(band) {
    switch (band) {
        case 'excellent': return 'evals-quality--excellent';
        case 'good': return 'evals-quality--good';
        case 'fair': return 'evals-quality--fair';
        default: return 'evals-quality--poor';
    }
}

function renderQualityRing(overall, band) {
    const pct = Math.max(0, Math.min(100, Number(overall) || 0));
    const dash = Math.round((pct / 100) * 283);
    return `
        <div class="evals-quality-ring ${qualityBandClass(band)}" aria-label="Quality score ${pct}">
            <svg viewBox="0 0 100 100" class="evals-quality-ring-svg">
                <circle class="evals-quality-ring-bg" cx="50" cy="50" r="45"/>
                <circle class="evals-quality-ring-fg" cx="50" cy="50" r="45"
                    stroke-dasharray="${dash} 283" transform="rotate(-90 50 50)"/>
            </svg>
            <div class="evals-quality-ring-value">${pct}</div>
        </div>`;
}

const EVALS_ENGINE_LABELS = {
    deepeval: 'DeepEval GEval (task completion)',
    judge_fallback: 'LLM judge',
    'contract+judge': 'Contract + LLM judge',
    'contract+judge+args': 'Contract + args + judge',
    'contract+args': 'Contract + args',
    contract: 'Tool contract',
    judge: 'LLM judge',
    heuristic: 'Heuristic',
    deterministic: 'Deterministic',
    per_source_avg: 'Per-source rollup',
    trace: 'Trace heuristic',
    'trace+token': 'Trace + tokens',
    no_tools: 'No tools run',
    default: 'Default',
};

function engineLabel(engine) {
    if (!engine) return '';
    return EVALS_ENGINE_LABELS[engine] || String(engine).replace(/_/g, ' ');
}

function renderSubscoreDial(key, row) {
    const label = EVALS_SUBSCORE_LABELS[key] || key;
    const score = Math.round((Number(row?.score) || 0) * 100);
    const engine = row?.engine ? `<span class="evals-dial-engine muted">${escapeHtml(engineLabel(row.engine))}</span>` : '';
    const reason = row?.reason ? String(row.reason).trim() : '';
    const reasonHtml = reason && score < 85
        ? `<p class="evals-dial-reason muted" title="${escapeAttr(reason)}">${escapeHtml(reason.length > 220 ? `${reason.slice(0, 217)}…` : reason)}</p>`
        : '';
    return `
        <div class="evals-dial evals-dial--${escapeHtml(key)}" title="${escapeHtml(label)}">
            <div class="evals-dial-bar"><div class="evals-dial-fill ${getScoreClass(score / 100)}" style="width:${score}%"></div></div>
            <div class="evals-dial-meta">
                <span class="evals-dial-label">${escapeHtml(label)}</span>
                <strong class="evals-dial-value ${getScoreClass(score / 100)}">${score}%</strong>
            </div>
            ${engine}
            ${reasonHtml}
        </div>`;
}

function renderQualityStatusLine(d) {
    const verdict = d.verdict || 'pending';
    const counts = d.counts || {};
    const critical = counts.critical || 0;
    const advisory = counts.advisory || 0;
    let status = 'Export safe';
    let statusClass = 'evals-quality-status--pass';
    if (d.overridden) {
        status = 'Override logged — export allowed';
        statusClass = 'evals-quality-status--override';
    } else if (!d.export_safe || verdict === 'blocked') {
        status = 'Export blocked';
        statusClass = 'evals-quality-status--fail';
    } else if (verdict === 'advisory') {
        status = 'Export allowed — advisory signals';
        statusClass = 'evals-quality-status--warn';
    } else if (verdict === 'pending') {
        status = 'Evals pending';
        statusClass = 'evals-quality-status--pending';
    }
    const ch = d.context_health || {};
    const ctxBit = ch.sources_total
        ? `${ch.sources_passing}/${ch.sources_total} sources pass context isolation`
        : '';
    const parts = [status, d.headline || '', ctxBit].filter(Boolean);
    return `<p class="evals-quality-status ${statusClass}">${escapeHtml(parts.join(' · '))}${critical || advisory ? ` <span class="evals-quality-status-counts"><span class="evals-count-critical">${critical} critical</span> <span class="evals-count-advisory">${advisory} advisory</span></span>` : ''}</p>`;
}

function renderQualityHeader(d, job, judge) {
    const q = d.quality || {};
    const overall = q.overall;
    if (overall == null && !q.subscores) {
        return `<section class="evals-section evals-quality-header evals-quality-header--pending">
            <p class="muted">Quality score will appear after finalize runs the LLM judge.</p>
        </section>`;
    }
    const band = q.band || 'fair';
    const subscores = q.subscores || {};
    const dials = EVALS_SUBSCORE_ORDER.map((k) => renderSubscoreDial(k, subscores[k] || {})).join('');
    const strengths = (q.strengths || []).slice(0, 4);
    const weaknesses = (q.weaknesses || []).slice(0, 3);
    const insightParts = [];
    if (weaknesses.length) {
        insightParts.push(`<ul class="evals-quality-insights evals-quality-insights--warn">${weaknesses.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>`);
    }
    if (strengths.length) {
        insightParts.push(`<ul class="evals-quality-insights evals-quality-insights--ok">${strengths.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>`);
    }
    const insight = insightParts.length
        ? insightParts.join('')
        : '<p class="muted evals-quality-insights-empty">Balanced run — expand pipeline stages for detail.</p>';
    const deepevalNote = q.deepeval?.available
        ? (q.deepeval.used_for?.length ? '<span class="trace-chip trace-chip--muted">DeepEval</span>' : '<span class="trace-chip trace-chip--muted">DeepEval available</span>')
        : '';
    const gateNote = q.gate_blocked
        ? '<span class="evals-quality-gate-warn">Export gate blocked — score capped</span>'
        : '';
    return `
        <section class="evals-section evals-quality-header">
            <div class="evals-quality-header-grid">
                ${renderQualityRing(overall, band)}
                <div class="evals-quality-header-body">
                    <div class="evals-quality-header-top">
                        <h2 class="evals-quality-title">Quality score</h2>
                        <span class="evals-quality-band ${qualityBandClass(band)}">${escapeHtml(String(band))}</span>
                        ${deepevalNote}
                    </div>
                    ${gateNote}
                    ${renderQualityStatusLine(d)}
                    <div class="evals-dial-grid">${dials}</div>
                    ${insight}
                </div>
            </div>
        </section>`;
}

function renderVerdictBanner(job, d) {
    const verdict = d.verdict || 'pending';
    let bannerClass = 'evals-verdict-banner--pass';
    let title = 'Export safe';
    if (d.overridden) {
        bannerClass = 'evals-verdict-banner--override';
        title = 'Override logged — export allowed';
    } else if (!d.export_safe || verdict === 'blocked') {
        bannerClass = 'evals-verdict-banner--fail';
        title = 'Export blocked';
    } else if (verdict === 'advisory') {
        bannerClass = 'evals-verdict-banner--warn';
        title = 'Export allowed — advisory signals';
    } else if (verdict === 'pending') {
        bannerClass = 'evals-verdict-banner--pending';
        title = 'Evals pending';
    }
    const sub = escapeHtml(d.headline || '');
    const ch = d.context_health || {};
    const ctxLine = ch.sources_total
        ? `<p class="evals-context-health muted">${ch.sources_passing}/${ch.sources_total} sources pass context isolation${ch.bleed_count ? ` · ${ch.bleed_count} bleed signal(s)` : ''}${ch.max_hop ? ` · max hop ${ch.max_hop}` : ''}</p>`
        : '';
    return `
        <div class="evals-verdict-banner ${bannerClass}">
            <div class="evals-verdict-banner-main">
                <h2 class="evals-verdict-banner-title">${title}</h2>
                <p class="evals-verdict-banner-sub">${sub}</p>
                ${ctxLine}
            </div>
            <div class="evals-verdict-counts">
                <span class="evals-count-critical">${d.counts?.critical || 0} critical</span>
                <span class="evals-count-advisory">${d.counts?.advisory || 0} advisory</span>
            </div>
        </div>
    `;
}

function renderNextSteps(d, job) {
    const steps = d.next_steps || [];
    const actions = d.recommended_actions || ['open_debug'];
    const btns = [];
    if (actions.includes('open_debug')) {
        btns.push(`<button type="button" class="btn-secondary btn-sm" onclick="viewJobDebugPipelineEvals('${escapeJsString(job.job_id)}')">Open Debug</button>`);
    }
    if (actions.includes('open_review') || String(job.status || '').toLowerCase().includes('review')) {
        btns.push(`<button type="button" class="btn-secondary btn-sm" onclick="openJobReview('${escapeJsString(job.job_id)}')">Open Review</button>`);
    }
    if (actions.includes('open_processing') || job.status === 'processing') {
        btns.push(`<button type="button" class="btn-secondary btn-sm" onclick="openJobProcessing('${escapeJsString(job.job_id)}')">Open Processing</button>`);
    }
    const list = steps.length
        ? `<ul class="evals-next-steps-list">${steps.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>`
        : '<p class="evals-next-steps-empty muted">No action required.</p>';
    return `
        <section class="evals-section">
            <h4 class="evals-section-title">What to do next</h4>
            ${list}
            <div class="evals-cta-row">${btns.join('')}</div>
        </section>
    `;
}

function stageStatusIcon(status) {
    switch (status) {
        case 'pass': return '✓';
        case 'warn': return '!';
        case 'fail': return '✕';
        default: return '—';
    }
}

function stageStatusClass(status) {
    switch (status) {
        case 'pass': return 'evals-stage--pass';
        case 'warn': return 'evals-stage--warn';
        case 'fail': return 'evals-stage--fail';
        default: return 'evals-stage--pending';
    }
}

function renderStageStepper(d, jobId) {
    const rollups = d.stage_rollups?.length
        ? d.stage_rollups
        : EVALS_STAGE_ORDER.map((stage) => ({
            stage,
            stage_label: EVALS_STAGE_LABELS[stage],
            status: 'not_run',
            critical: 0,
            advisory: 0,
        }));
    const issues = d.issues || [];
    const cards = rollups.map((r) => `
        <button type="button" class="evals-stage-card ${stageStatusClass(r.status)}${evalsState.expandedStage === r.stage ? ' is-expanded' : ''}"
            onclick="toggleEvalStage('${escapeJsString(r.stage)}')"
            aria-expanded="${evalsState.expandedStage === r.stage}">
            <span class="evals-stage-icon">${stageStatusIcon(r.status)}</span>
            <span class="evals-stage-label">${escapeHtml(r.stage_label || EVALS_STAGE_LABELS[r.stage] || r.stage)}</span>
            ${stageCardScoreSnippet(r)}
            <span class="evals-stage-counts">${r.critical || 0} crit · ${r.advisory || 0} adv</span>
        </button>
    `).join('');
    const expanded = evalsState.expandedStage;
    const expandedRollup = expanded ? rollups.find((r) => r.stage === expanded) : null;
    const stageIssues = expanded
        ? issues.filter((i) => i.stage === expanded)
        : [];
    const metricsBlock = expandedRollup ? renderStageMetricsPanel(expandedRollup, d.context_health) : '';
    const issueBlock = expanded
        ? `<div class="evals-stage-issues-panel">
            ${metricsBlock}
            ${stageIssues.length
            ? stageIssues.map((i) => renderIssueRow(i, jobId)).join('')
            : '<p class="muted">No issues listed for this stage.</p>'}
           </div>`
        : '';
    return `
        <section class="evals-section">
            <h4 class="evals-section-title">Pipeline stages</h4>
            <div class="evals-stage-stepper">${cards}</div>
            ${issueBlock}
        </section>
    `;
}

function toggleEvalStage(stage) {
    evalsState.expandedStage = evalsState.expandedStage === stage ? null : stage;
    const jobs = filteredEvalJobs();
    const job = jobs[evalsState.selectedIndex];
    if (job) renderScorecard(job);
}

function renderIssueRow(issue, jobId) {
    const R = window.PipelineEvalsRender;
    if (issue.type === 'metric_mismatch') return '';
    const sheet = issue.sheet_name ? ` · ${issue.sheet_name}` : '';
    const typeLabel = issue.type_label || (R ? R.typeLabel(issue.type) : issue.type) || 'Issue';
    return `
        <div class="evals-issue-row evals-issue-row--${issue.severity || 'advisory'}">
            <div class="evals-issue-main">
                <strong class="evals-issue-type">${escapeHtml(typeLabel)}</strong>
                <span class="evals-issue-source">${escapeHtml(issue.stage_label || issue.stage || '')}${escapeHtml(sheet)}</span>
                <p class="evals-issue-message">${escapeHtml(issue.message || '')}</p>
            </div>
            <button type="button" class="btn-link evals-issue-link" onclick="viewJobDebugPipelineEvals('${escapeJsString(jobId)}')">Debug</button>
        </div>
    `;
}

function renderIssuesPanel(d, jobId) {
    const R = window.PipelineEvalsRender;
    const critical = (d.issues || []).filter((i) => i.severity === 'critical');
    const advisory = (d.issues || []).filter((i) => i.severity === 'advisory');
    const critOpen = critical.length > 0;

    const critMetric = critical.filter((i) => i.type === 'metric_mismatch');
    const critOther = critical.filter((i) => i.type !== 'metric_mismatch');
    const advMetric = advisory.filter((i) => i.type === 'metric_mismatch');
    const advOther = advisory.filter((i) => i.type !== 'metric_mismatch');

    const metricBlock = (rows, max) => (R && rows.length
        ? `<div class="pe-issue-block evals-metric-block">
            <div class="pe-issue-block-title">${escapeHtml(R.typeLabel('metric_mismatch'))} <span class="muted">(${rows.length})</span></div>
            <p class="pe-issue-hint muted">Expected vs processed spends/impressions per date+publisher (clean template reference).</p>
            ${R.renderMetricMismatchTable(rows, max)}
           </div>`
        : '');

    return `
        <section class="evals-section">
            <h4 class="evals-section-title">Issues</h4>
            <details class="evals-issues-group" ${critOpen ? 'open' : ''}>
                <summary class="evals-issues-summary evals-issues-summary--critical">Critical (${critical.length})</summary>
                <div class="evals-issues-body">
                    ${critical.length
                        ? metricBlock(critMetric, 10) + critOther.map((i) => renderIssueRow(i, jobId)).join('')
                        : '<p class="muted">No critical issues.</p>'}
                </div>
            </details>
            <details class="evals-issues-group">
                <summary class="evals-issues-summary">Advisory (${advisory.length})</summary>
                <div class="evals-issues-body">
                    ${advisory.length
                        ? metricBlock(advMetric, 5) + advOther.map((i) => renderIssueRow(i, jobId)).join('')
                        : '<p class="muted">No advisory issues.</p>'}
                </div>
            </details>
        </section>
    `;
}

function sourceQualityBandClass(overall) {
    const n = Number(overall) || 0;
    if (n >= 85) return 'evals-quality--excellent';
    if (n >= 70) return 'evals-quality--good';
    if (n >= 50) return 'evals-quality--fair';
    return 'evals-quality--poor';
}

function renderSourceQualityBadge(psq) {
    if (!psq || psq.overall == null) return '';
    const n = Math.round(Number(psq.overall) || 0);
    return `<span class="evals-source-quality ${sourceQualityBandClass(n)}" title="Per-source quality">${n}</span>`;
}

function renderSourceDials(psq) {
    if (!psq || !psq.subscores) return '';
    const subscores = psq.subscores;
    const labels = psq.labels || {};
    const dials = Object.keys(subscores).map((k) => {
        const row = subscores[k] || {};
        const label = labels[k] || EVALS_SUBSCORE_LABELS[k] || k;
        const score = Math.round((Number(row.score) || 0) * 100);
        return `
            <div class="evals-source-dial" title="${escapeHtml(label)}">
                <div class="evals-source-dial-bar"><div class="evals-source-dial-fill ${getScoreClass(score / 100)}" style="width:${score}%"></div></div>
                <div class="evals-source-dial-meta">
                    <span class="evals-source-dial-label">${escapeHtml(label)}</span>
                    <strong class="evals-source-dial-value ${getScoreClass(score / 100)}">${score}%</strong>
                </div>
            </div>`;
    }).join('');
    return `<div class="evals-source-dials">${dials}</div>`;
}

function renderSourceAccordion(d, job) {
    const pe = job.pipeline_evals || {};
    const per = pe.per_source || {};
    const labels = d.source_labels || {};
    const psQuality = d.per_source_quality || {};
    const sids = Object.keys(per);
    if (!sids.length) return '';

    const rows = sids.map((sid) => {
        const meta = labels[sid] || {};
        const title = meta.sheet_name || sid.split(':').pop() || sid;
        const file = meta.file_name || job.filename || '';
        const bucket = per[sid] || {};
        const psq = psQuality[sid] || null;
        const expanded = evalsState.expandedSources[sid];
        const chips = EVALS_STAGE_ORDER.filter((s) => s !== 'collation').map((stage) => {
            const row = bucket[stage];
            const ok = !row || row.pass !== false;
            const label = (window.PipelineEvalsRender && PipelineEvalsRender.stageLabel(stage)) || stage;
            return `<span class="evals-source-chip ${ok ? 'pass' : 'fail'}">${escapeHtml(label)}</span>`;
        }).join('');
        const viols = EVALS_STAGE_ORDER.flatMap((stage) => {
            const row = bucket[stage];
            return (row?.violations || []).map((v) => ({
                ...v,
                stage,
                severity: v.severity || (v.type === 'metric_mismatch' || v.type === 'dimension_mismatch' ? 'critical' : 'advisory'),
                type_label: (window.PipelineEvalsRender && PipelineEvalsRender.typeLabel(v.type)) || v.type,
                stage_label: EVALS_STAGE_LABELS[stage] || stage,
                sheet_name: title,
            }));
        });
        const crit = viols.filter((v) => v.severity === 'critical' || ['metric_mismatch', 'dimension_mismatch'].includes(v.type));
        const adv = viols.filter((v) => !crit.includes(v));
        const R = window.PipelineEvalsRender;
        const body = expanded
            ? `<div class="evals-source-body">
                ${renderSourceDials(psq)}
                ${crit.length && R ? `<div class="pe-source-section"><h5 class="pe-source-section-title">Critical</h5>${R.renderIssueGroup('', crit, { metricTableMax: 8 })}</div>` : ''}
                ${adv.length ? adv.map((v) => renderIssueRow(v, job.job_id)).join('') : (!crit.length ? '<p class="muted">No issues for this source.</p>' : '')}
               </div>`
            : '';
        return `
            <div class="evals-source-row">
                <button type="button" class="evals-source-header" onclick="toggleEvalSource('${escapeJsString(sid)}')">
                    ${renderSourceQualityBadge(psq)}
                    <span class="evals-source-title">${escapeHtml(title)}</span>
                    <span class="evals-source-file muted">${escapeHtml(file)}</span>
                </button>
                <div class="evals-source-chips">${chips}</div>
                ${body}
            </div>
        `;
    }).join('');

    return `
        <section class="evals-section">
            <h4 class="evals-section-title">Per source</h4>
            <p class="evals-source-note muted">Plan quality, context quality &amp; step efficiency are scored per sheet. Task completion (DeepEval) and cross-source collation are job-level only — see the Quality score above.</p>
            <div class="evals-source-accordion">${rows}</div>
        </section>
    `;
}

function toggleEvalSource(sid) {
    evalsState.expandedSources[sid] = !evalsState.expandedSources[sid];
    const jobs = filteredEvalJobs();
    const job = jobs[evalsState.selectedIndex];
    if (job) renderScorecard(job);
}

function renderJudgePanel(job, judge) {
    const stepScores = judge?.trace_analysis?.step_scores || [];
    const pending = !judge;
    const err = judge?.error;

    let inner = '';
    if (err) {
        inner = `<p class="evals-judge-error">${escapeHtml(err)}</p>`;
    } else if (pending) {
        inner = '<p class="muted">LLM judge has not run yet or is disabled in Settings. This does not affect pipeline pass/fail.</p>';
    } else {
        const fid = Math.round((judge.fidelity ?? 0) * 100);
        const flat = Math.round((judge.flatness ?? 0) * 100);
        const integ = Math.round((judge.integrity ?? 0) * 100);
        inner = `
            <div class="evals-judge-metrics">
                <div class="evals-judge-metric"><span>Fidelity</span><strong class="${getScoreClass(judge.fidelity ?? 0)}">${fid}%</strong></div>
                <div class="evals-judge-metric"><span>Flatness</span><strong class="${getScoreClass(judge.flatness ?? 0)}">${flat}%</strong></div>
                <div class="evals-judge-metric"><span>Integrity</span><strong class="${getScoreClass(judge.integrity ?? 0)}">${integ}%</strong></div>
                <div class="evals-judge-metric"><span>Verdict</span><strong>${escapeHtml(judge.verdict || '—')}</strong></div>
            </div>
            <p class="evals-judge-critique">${escapeHtml((judge.critique || '').substring(0, 400))}</p>
            ${stepScores.length ? renderJudgeSteps(stepScores, job.job_id) : ''}
        `;
    }

    return `
        <details class="evals-section evals-judge-tier">
            <summary class="evals-section-title evals-judge-summary">LLM judge <span class="muted">(Tier 3 — secondary audit)</span></summary>
            <div class="evals-judge-body">${inner}</div>
        </details>
    `;
}

function renderJudgeSteps(stepScores, jobId) {
    window.currentJobStepScores = stepScores;
    window.currentJobId = jobId;
    return `
        <div class="evals-judge-steps">
            <div class="evals-judge-step-list">
                ${stepScores.map((step, idx) => `
                    <button type="button" class="evals-judge-step-item" data-step-idx="${idx}" onclick="selectDetailStep(${idx})">
                        <span>#${idx + 1}</span> ${escapeHtml(step.step_name || 'Step')}
                    </button>
                `).join('')}
            </div>
            <div class="evals-judge-step-detail" id="detailStepContent">
                <p class="muted">Select a step to view judge critique.</p>
            </div>
        </div>
    `;
}

function selectDetailStep(stepIdx) {
    const stepScores = window.currentJobStepScores || [];
    const step = stepScores[stepIdx];
    if (!step) return;
    document.querySelectorAll('.evals-judge-step-item').forEach((el) => el.classList.remove('active'));
    document.querySelector(`.evals-judge-step-item[data-step-idx="${stepIdx}"]`)?.classList.add('active');
    const logicScore = step.logic_score || 0;
    const fidelityScore = step.fidelity_score || 0;
    const content = document.getElementById('detailStepContent');
    if (!content) return;
    content.innerHTML = `
        <h5>${escapeHtml(step.step_name || 'Step')}</h5>
        <div class="evals-judge-step-scores">
            <span>Logic <strong class="${getScoreClass(logicScore)}">${Math.round(logicScore * 100)}%</strong></span>
            <span>Fidelity <strong class="${getScoreClass(fidelityScore)}">${Math.round(fidelityScore * 100)}%</strong></span>
        </div>
        <p class="evals-judge-step-critique">${escapeHtml(step.critique || 'No critique.')}</p>
    `;
}

function getScoreClass(score) {
    if (score >= 0.85) return 'value-high';
    if (score >= 0.6) return 'value-mid';
    return 'value-low';
}

function viewJobDebug(jobId) {
    setCurrentJob(jobId);
    navigateTo('debug');
}

function viewJobDebugPipelineEvals(jobId) {
    setCurrentJob(jobId);
    window.location.href = `/pages/debug.html?tab=pipeline-evals&job_id=${encodeURIComponent(jobId)}`;
}

function openJobReview(jobId) {
    setCurrentJob(jobId);
    navigateTo('review');
}

function openJobProcessing(jobId) {
    setCurrentJob(jobId);
    navigateTo('processing');
}

window.selectJob = selectJob;
window.toggleEvalStage = toggleEvalStage;
window.toggleEvalSource = toggleEvalSource;
window.selectDetailStep = selectDetailStep;
window.viewJobDebug = viewJobDebug;
window.viewJobDebugPipelineEvals = viewJobDebugPipelineEvals;
window.openJobReview = openJobReview;
window.openJobProcessing = openJobProcessing;
