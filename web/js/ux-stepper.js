/**
 * Shared horizontal UX stepper (3 user-facing steps). Uses ux_stepper_summary from GET /api/status/<job_id>.
 * Backend current_ux_stage remains 0–4; stages 2+ are shown as one “Plan & run” step until the job completes.
 */
(function (global) {
    const STEP_LABELS = ['Upload', 'Layout & mapping', 'Plan & run'];

    const UI_LAST_INDEX = STEP_LABELS.length - 1;

    /**
     * Map backend current_ux_stage (0–4) to UI step index (0–2).
     * Any in-pipeline stage (2–4) maps to the last pill.
     */
    function mapBackendUxStageToUiHighlight(curUx) {
        const c = Number(curUx ?? 0);
        if (c <= 1) return 1;
        return UI_LAST_INDEX;
    }

    function isStepComplete(index, summary, options) {
        const s = summary || {};
        const opt = options || {};
        if (index === 0) return Boolean(s.stage0_complete);
        if (index === 1) return Boolean(s.stage1_complete);
        if (index === 2) return Boolean(opt.jobComplete);
        return false;
    }

    /**
     * @param {HTMLElement|null} container
     * @param {object} summary - ux_stepper_summary from job status
     * @param {{ highlightIndex?: number, pendingNextStepIndex?: number, jobComplete?: boolean }} [options]
     */
    function renderUxStepper(container, summary, options) {
        if (!container) return;
        const sum = summary || {};
        const opt = options || {};
        const forced = opt.highlightIndex != null ? Number(opt.highlightIndex) : null;
        let highlight =
            forced != null && !Number.isNaN(forced) ? Math.max(0, Math.min(UI_LAST_INDEX, forced)) : 0;
        if (forced == null) {
            if (sum.stage1_complete) highlight = Math.max(highlight, 2);
            else if (sum.stage0_complete) highlight = 1;
            else highlight = 0;
        }
        const pendingNext =
            opt.pendingNextStepIndex != null
                ? Math.max(0, Math.min(UI_LAST_INDEX, Number(opt.pendingNextStepIndex)))
                : null;

        const parts = ['<div class="ux-stepper ux-stepper-horizontal" role="list">'];
        for (let i = 0; i < STEP_LABELS.length; i++) {
            const complete = isStepComplete(i, sum, opt);
            const upcoming = Boolean(pendingNext === i && !complete);
            const current = !complete && !upcoming && highlight === i;
            const classes = ['ux-step'];
            if (complete) classes.push('ux-step--complete');
            if (current) classes.push('ux-step--current');
            if (upcoming) classes.push('ux-step--upcoming');
            const badge = complete ? '✓' : String(i + 1);
            parts.push(
                `<div class="${classes.join(' ')}" role="listitem" aria-current="${current ? 'step' : 'false'}">` +
                    `<span class="ux-step-badge" aria-hidden="true">${badge}</span>` +
                    `<span class="ux-step-label">${STEP_LABELS[i]}</span>` +
                    `</div>`
            );
        }
        parts.push('</div>');
        container.innerHTML = parts.join('');
    }

    global.renderUxStepper = renderUxStepper;
    global.mapBackendUxStageToUiHighlight = mapBackendUxStageToUiHighlight;
})(typeof window !== 'undefined' ? window : globalThis);
