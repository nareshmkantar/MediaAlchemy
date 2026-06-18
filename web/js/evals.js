/**
 * Evals Page - Schema Agent
 * Manages the LLM Judge results display
 */

document.addEventListener('DOMContentLoaded', () => {
    initEvals();
});

async function initEvals() {
    const refreshBtn = document.getElementById('refreshEvals');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', loadEvals);
    }

    loadEvals();
}

async function loadEvals() {
    const grid = document.getElementById('evalsGrid');
    if (!grid) return;

    try {
        grid.innerHTML = '<div class="empty-state"><p>Fetching evaluation results...</p></div>';

        // 1. Fetch from optimized endpoint
        const response = await fetchJson('/api/evals');
        const evaluatedJobs = response.evals || [];

        if (evaluatedJobs.length === 0) {
            grid.innerHTML = '<div class="empty-state"><p>No jobs have evaluation results yet. Process a file using the granular tools.</p></div>';
            return;
        }

        // 2. Render
        renderEvalCards(evaluatedJobs);

    } catch (e) {
        console.error('Failed to load evals:', e);
        showToast('Failed to load evaluation results', 'error');
        grid.innerHTML = `<div class="empty-state"><p class="error">Error: ${e.message}</p></div>`;
    }
}

function renderEvalCards(jobs) {
    const grid = document.getElementById('evalsGrid');
    if (!grid) return;

    // Store all jobs globally for detail panel access
    window.evalsJobs = jobs;

    grid.innerHTML = jobs.map((job, index) => {
        const judge = job.judge_result;

        // Handle Missing or Errored Judge Result
        if (!judge) return '';
        if (judge.error) {
            return `
                <div class="eval-card eval-error" data-job-index="${index}" onclick="selectJob(${index})">
                    <div class="eval-header">
                        <h3 class="eval-title">${escapeHtml(job.filename)}</h3>
                        <span class="eval-status status-fail">ERROR</span>
                    </div>
                    <div class="eval-critique" style="border-left-color: var(--danger);">
                        <strong>Evaluation Component Error:</strong><br>
                        ${escapeHtml(judge.error)}
                    </div>
                    <div class="eval-footer">
                         <div class="eval-meta">Job ID: ${job.job_id.substring(0, 8)}</div>
                    </div>
                </div>
            `;
        }

        const verdictClass = `status-${(judge.verdict || 'review').toLowerCase()}`;
        const fidClass = getScoreClass(judge.fidelity);
        const flatClass = getScoreClass(judge.flatness);
        const intClass = getScoreClass(judge.integrity);
        const taskSuccessText = judge.task_success ? "✅ SUCCESS" : "❌ FAILED";
        const taskSuccessClass = judge.task_success ? "verdict-pass" : "verdict-fail";
        const stepCount = judge.trace_analysis?.step_scores?.length || 0;

        return `
            <div class="eval-card" data-job-index="${index}" onclick="selectJob(${index})" style="cursor: pointer;">
                <div class="eval-header">
                    <h3 class="eval-title">${escapeHtml(job.filename)}</h3>
                    <span class="eval-status ${verdictClass}">${judge.verdict}</span>
                </div>

                <div class="eval-metrics">
                    <div class="metric-item">
                        <span class="metric-label">Fidelity</span>
                        <span class="metric-value ${fidClass}">${Math.round(judge.fidelity * 100)}%</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Flatness</span>
                        <span class="metric-value ${flatClass}">${Math.round(judge.flatness * 100)}%</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Integrity</span>
                        <span class="metric-value ${intClass}">${Math.round(judge.integrity * 100)}%</span>
                    </div>
                </div>

                <div class="eval-critique">
                    <div style="margin-bottom: 5px;">
                        <strong>Task Status:</strong> <span class="${taskSuccessClass}" style="font-weight: bold; font-size: 0.9em;">${taskSuccessText}</span>
                        ${stepCount > 0 ? `<span style="float: right; color: var(--text-tertiary); font-size: 0.8em;">📊 ${stepCount} steps</span>` : ''}
                    </div>
                    <strong>Review:</strong> ${escapeHtml((judge.critique || 'No critique available.').substring(0, 150))}${judge.critique?.length > 150 ? '...' : ''}
                </div>

                <div class="eval-footer">
                    <div class="eval-meta">
                        Job ID: ${job.job_id.substring(0, 8)} • ${formatTime(job.created_at)}
                    </div>
                    <button class="btn-view-debug" onclick="event.stopPropagation(); viewJobDebug('${job.job_id}')">View Trace</button>
                </div>
            </div>
        `;
    }).join('');

    // Auto-select first job with step_scores
    const firstJobWithSteps = jobs.findIndex(j => j.judge_result?.trace_analysis?.step_scores?.length > 0);
    if (firstJobWithSteps >= 0) {
        setTimeout(() => selectJob(firstJobWithSteps), 100);
    }
}

// Select a job and render its per-step evals in the right panel
function selectJob(jobIndex) {
    const jobs = window.evalsJobs || [];
    const job = jobs[jobIndex];
    if (!job) return;

    // Update card selection state
    document.querySelectorAll('.eval-card').forEach(c => c.classList.remove('selected'));
    document.querySelector(`.eval-card[data-job-index="${jobIndex}"]`)?.classList.add('selected');

    const detailPanel = document.getElementById('evalsDetailPanel');
    if (!detailPanel) return;

    const judge = job.judge_result;
    const stepScores = judge?.trace_analysis?.step_scores || [];

    if (stepScores.length === 0) {
        detailPanel.innerHTML = `
            <div class="detail-empty-state">
                <div class="empty-icon">📊</div>
                <h3>${escapeHtml(job.filename)}</h3>
                <p>No per-step evaluation data available for this job.</p>
            </div>
        `;
        return;
    }

    // Store step scores for this job
    window.currentJobStepScores = stepScores;
    window.currentJobId = job.job_id;

    // Render the per-step evaluations panel
    detailPanel.innerHTML = `
        <div class="detail-panel-header">
            <h3>⚖️ ${escapeHtml(job.filename)}</h3>
            <p style="color: var(--text-tertiary); margin: 4px 0 0; font-size: 0.85rem;">${stepScores.length} steps evaluated</p>
        </div>
        <div class="detail-step-container">
            <div class="detail-step-list">
                ${stepScores.map((step, idx) => `
                    <div class="detail-step-item" data-step-idx="${idx}" onclick="selectDetailStep(${idx})">
                        <span class="step-idx">#${idx + 1}</span>
                        <span class="step-name">${escapeHtml(step.step_name || 'Step')}</span>
                    </div>
                `).join('')}
            </div>
            <div class="detail-step-content" id="detailStepContent">
                <div class="empty-state" style="padding: 40px; text-align: center; color: var(--text-tertiary);">
                    <p>👈 Select a step to view details</p>
                </div>
            </div>
        </div>
    `;

    // Auto-select first step
    setTimeout(() => selectDetailStep(0), 50);
}

// Select a step in the detail panel
function selectDetailStep(stepIdx) {
    const stepScores = window.currentJobStepScores || [];
    const step = stepScores[stepIdx];
    if (!step) return;

    // Update step selection
    document.querySelectorAll('.detail-step-item').forEach(el => el.classList.remove('active'));
    document.querySelector(`.detail-step-item[data-step-idx="${stepIdx}"]`)?.classList.add('active');

    const logicScore = step.logic_score || 0;
    const fidelityScore = step.fidelity_score || 0;
    const logicClass = getScoreClass(logicScore);
    const fidClass = getScoreClass(fidelityScore);

    const content = document.getElementById('detailStepContent');
    if (content) {
        content.innerHTML = `
            <div class="detail-step-header">
                <h4>${escapeHtml(step.step_name || 'Step ' + (stepIdx + 1))}</h4>
            </div>
            <div class="detail-scores-grid">
                <div class="detail-score-card">
                    <div class="score-label">LOGIC SCORE</div>
                    <div class="score-value ${logicClass}">${Math.round(logicScore * 100)}%</div>
                </div>
                <div class="detail-score-card">
                    <div class="score-label">FIDELITY SCORE</div>
                    <div class="score-value ${fidClass}">${Math.round(fidelityScore * 100)}%</div>
                </div>
            </div>
            <div class="detail-critique-section">
                <div class="critique-label">CRITIQUE</div>
                <div class="critique-content">
                    ${escapeHtml(step.critique || 'No detailed critique available for this step.')}
                </div>
            </div>
        `;
    }
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

function selectStepEval(jobId, stepIdx) {
    console.log(`[Evals] selectStepEval called: job=${jobId}, step=${stepIdx}`);

    // Update selection state
    const splitPane = document.getElementById(`stepEvalsSplit-${jobId}`);
    if (splitPane) {
        splitPane.querySelectorAll('.step-eval-item').forEach(el => el.classList.remove('active'));
        const selectedItem = splitPane.querySelector(`.step-eval-item[data-step-idx="${stepIdx}"]`);
        if (selectedItem) {
            selectedItem.classList.add('active');
        }
    }

    // Get step data
    const stepScoresKey = `stepScores_${jobId}`;
    const stepScores = window[stepScoresKey] || [];
    console.log(`[Evals] stepScores for ${stepScoresKey}:`, stepScores.length, 'items');

    const step = stepScores[stepIdx];
    if (!step) {
        console.warn(`[Evals] No step found at index ${stepIdx}`);
        return;
    }

    const logicScore = step.logic_score || 0;
    const fidelityScore = step.fidelity_score || 0;
    const logicClass = getScoreClass(logicScore);
    const fidClass = getScoreClass(fidelityScore);

    const detailPane = document.getElementById(`stepDetail-${jobId}`);
    if (detailPane) {
        detailPane.innerHTML = `
            <div class="step-detail-header">
                <h4>${escapeHtml(step.step_name || 'Step ' + (stepIdx + 1))}</h4>
            </div>
            <div class="step-scores-grid">
                <div class="step-score-item">
                    <div class="step-score-label">Logic Score</div>
                    <div class="step-score-value ${logicClass}">${Math.round(logicScore * 100)}%</div>
                </div>
                <div class="step-score-item">
                    <div class="step-score-label">Fidelity Score</div>
                    <div class="step-score-value ${fidClass}">${Math.round(fidelityScore * 100)}%</div>
                </div>
            </div>
            <div>
                <div style="font-size: 0.75rem; color: var(--text-tertiary); margin-bottom: 6px; text-transform: uppercase;">Critique</div>
                <div class="step-critique-box">
                    ${escapeHtml(step.critique || 'No detailed critique available for this step.')}
                </div>
            </div>
        `;
    }
}
