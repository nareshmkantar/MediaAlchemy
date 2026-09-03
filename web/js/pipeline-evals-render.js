/**
 * Shared Pipeline Evals presentation (Debug tab + Quality Evals page).
 */
(function (global) {
    const STAGE_LABELS = {
        structure: 'Structure',
        plan: 'Plan',
        plan_review: 'Plan review',
        execution: 'Execution',
        context_isolation: 'Context',
        collation: 'Collation',
    };

    const TYPE_LABELS = {
        zero_rows: 'Empty output',
        mass_row_loss: 'Row loss',
        metric_column_wiped: 'Metric wiped',
        aggregate_sum_mismatch: 'Sum mismatch',
        dimension_mismatch: 'Context bleed (dimensions)',
        metric_mismatch: 'Metric mismatch',
        memory_scope: 'Wrong source memory',
        context_grounding_critical: 'Plan context bleed',
        union_false_duplicate: 'False union duplicate',
        layout_alignment: 'Layout alignment',
        signal_consistency: 'Analyzer vs grid scan',
        tool_contract_sequence: 'Tool order',
        required_tools_missing: 'Missing tool',
        unnecessary_tools: 'Extra tool',
        context_completeness: 'Incomplete context',
        context_lineage_missing: 'Missing sheet lineage',
        low_confidence_context: 'Low-confidence context',
        context_hop_exceeded: 'Context hop exceeded',
        structure_plan_alignment: 'Structure/plan mismatch',
        plan_confidence_sanity: 'Plan confidence',
        review_miss: 'Missed review question',
        review_noise: 'Noisy review question',
        grain_tool_executed_pre_collation: 'Grain tool ran pre-collation',
        grain_tool_missing_deferral: 'Grain deferral missing',
        sparse_dimension_false_positive: 'Sparse dimension false positive',
        grouped_layout_false_positive: 'Grouped layout false positive',
    };

    const STAGE_ORDER = ['structure', 'plan', 'plan_review', 'execution', 'context_isolation'];

    function typeLabel(t) {
        return TYPE_LABELS[t] || String(t || 'Issue').replace(/_/g, ' ');
    }

    function stageLabel(s) {
        return STAGE_LABELS[s] || String(s || '');
    }

    function sheetFromSourceId(sid, labels) {
        const meta = (labels || {})[sid];
        if (meta?.sheet_name) return meta.sheet_name;
        const parts = String(sid || '').split(':');
        return parts[parts.length - 1] || sid || 'Unknown sheet';
    }

    function buildLabelsFromRegistry(registry) {
        const out = {};
        (registry || []).forEach((row) => {
            if (!row?.source_id) return;
            out[row.source_id] = {
                sheet_name: row.sheet_name || '',
                file_name: row.file_name || '',
            };
        });
        return out;
    }

    function violationSeverity(v) {
        if (String(v?.severity || '').toLowerCase() === 'critical') return 'critical';
        const criticalTypes = new Set([
            'zero_rows', 'mass_row_loss', 'metric_column_wiped', 'aggregate_sum_mismatch',
            'dimension_mismatch', 'metric_mismatch', 'memory_scope', 'context_grounding_critical',
            'context_lineage_missing', 'union_false_duplicate',
        ]);
        return criticalTypes.has(v?.type) ? 'critical' : 'advisory';
    }

    function flattenViolations(pe, sourceLabels) {
        const issues = [];
        const per = pe?.per_source || {};
        Object.entries(per).forEach(([sid, bucket]) => {
            if (!bucket || typeof bucket !== 'object') return;
            STAGE_ORDER.forEach((stage) => {
                const row = bucket[stage];
                (row?.violations || []).forEach((v) => {
                    issues.push({
                        ...v,
                        column: v.column || v.evidence?.column,
                        expected: v.expected || v.evidence?.expected,
                        actual: v.actual || v.evidence?.actual,
                        severity: violationSeverity(v),
                        stage,
                        stage_label: stageLabel(stage),
                        type_label: typeLabel(v.type),
                        source_id: sid,
                        sheet_name: sheetFromSourceId(sid, sourceLabels),
                        snapshot_hint: {
                            source_id: sid,
                            ...(stage === 'structure' ? { label: 'state.analyze_structure', phase: 'node_after' } : {}),
                            ...(stage === 'plan' || stage === 'plan_review' ? { label: 'state.generate_plan', phase: 'node_after' } : {}),
                            ...(stage === 'execution' ? { label: 'state.execute_tools.deferral', phase: 'deferral_decision' } : {}),
                            ...(stage === 'context_isolation' ? { label: 'job.multi_source.source_done', phase: 'after_process_file' } : {}),
                        },
                    });
                });
            });
        });
        (pe?.collation?.violations || []).forEach((v) => {
            issues.push({
                ...v,
                severity: violationSeverity(v),
                stage: 'collation',
                stage_label: stageLabel('collation'),
                type_label: typeLabel(v.type),
                source_id: '',
                sheet_name: '',
                snapshot_hint: {
                    source_id: '__collation__',
                    label: 'job.multi_source.collation',
                    phase: 'after_duplicate_check_before_deferred',
                },
            });
        });
        return issues;
    }

    function parseMetricFromMessage(message) {
        const m = String(message || '').match(
            /Metrics for ([^ ]+) do not match.*expected spends=([^,]+), impressions=([^;]+).*got spends=([^,]+), impressions=([^)]+)/
        );
        if (!m) return {};
        return {
            key: m[1],
            expected_spends: m[2],
            actual_spends: m[3],
            expected_impressions: m[4],
            actual_impressions: m[5],
        };
    }

    function parseMetricKey(message) {
        const m = String(message || '').match(/Metrics for ([^ ]+) do not match/);
        return m ? m[1] : '';
    }

    function metricRowFields(r) {
        const parsed = parseMetricFromMessage(r.message);
        return {
            key: r.key || parsed.key || parseMetricKey(r.message) || '—',
            expected_spends: r.expected_spends ?? parsed.expected_spends,
            actual_spends: r.actual_spends ?? parsed.actual_spends,
            expected_impressions: r.expected_impressions ?? parsed.expected_impressions,
            actual_impressions: r.actual_impressions ?? parsed.actual_impressions,
        };
    }

    function renderMetricMismatchTable(rows, maxShow) {
        const limit = maxShow == null ? 8 : maxShow;
        const shown = rows.slice(0, limit);
        const rest = rows.length - shown.length;
        if (!shown.length) return '';
        const head = `
            <table class="pe-mismatch-table">
                <thead><tr>
                    <th>Key</th><th>Sheet</th>
                    <th>Expected spends</th><th>Got</th>
                    <th>Expected impr.</th><th>Got</th>
                </tr></thead><tbody>`;
        const body = shown.map((r) => {
            const m = metricRowFields(r);
            const expS = m.expected_spends != null ? m.expected_spends : '—';
            const actS = m.actual_spends != null ? m.actual_spends : '—';
            const expI = m.expected_impressions != null ? m.expected_impressions : '—';
            const actI = m.actual_impressions != null ? m.actual_impressions : '—';
            return `<tr>
                <td><code>${escapeHtml(m.key)}</code></td>
                <td>${escapeHtml(r.sheet_name || '')}</td>
                <td class="pe-num">${escapeHtml(String(expS))}</td>
                <td class="pe-num pe-bad">${escapeHtml(String(actS))}</td>
                <td class="pe-num">${escapeHtml(String(expI))}</td>
                <td class="pe-num pe-bad">${escapeHtml(String(actI))}</td>
            </tr>`;
        }).join('');
        const more = rest > 0 ? `<p class="pe-more muted">${rest} more row(s) with metric mismatch — open Quality Evals for full list.</p>` : '';
        return `${head}${body}</tbody></table>${more}`;
    }

    function renderLocalContextBlock(localContext, scopedLocalContext) {
        const scoped = scopedLocalContext && typeof scopedLocalContext === 'object' ? scopedLocalContext : {};
        const flat = localContext && typeof localContext === 'object' ? localContext : {};
        const keys = [...new Set([...Object.keys(scoped), ...Object.keys(flat)])];
        if (!keys.length) return '';
        const rows = keys.map((k) => {
            const sf = scoped[k];
            const val = (sf && sf.value != null) ? sf.value : flat[k];
            const prov = sf && (sf.scope || sf.block_label || sf.evidence_line)
                ? [sf.scope, sf.block_label, sf.evidence_line].filter(Boolean).join(' · ')
                : '';
            return `<tr>
                <td><code>${escapeHtml(k)}</code></td>
                <td>${escapeHtml(String(val ?? ''))}</td>
                <td class="muted pe-prov">${escapeHtml(prov)}</td>
            </tr>`;
        }).join('');
        return `
            <div class="pe-local-context">
                <div class="pe-issue-block-title">Source-local context <span class="muted">(expected dimensions)</span></div>
                <table class="pe-mismatch-table">
                    <thead><tr><th>Field</th><th>Value</th><th>Provenance</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>`;
    }

    function renderDimensionMismatchTable(rows, maxShow, contextDrillFn) {
        const limit = maxShow == null ? 8 : maxShow;
        const shown = rows.slice(0, limit);
        const rest = rows.length - shown.length;
        if (!shown.length) return '';
        const hasDrill = typeof contextDrillFn === 'function';
        const head = `
            <table class="pe-mismatch-table">
                <thead><tr>
                    <th>Column</th><th>Sheet</th><th>Expected</th><th>Got</th>${hasDrill ? '<th>Debug</th>' : ''}
                </tr></thead><tbody>`;
        const body = shown.map((r) => {
            const col = r.column || r.evidence?.column || '—';
            const exp = r.expected ?? r.evidence?.expected ?? '—';
            const act = r.actual ?? r.evidence?.actual ?? '—';
            const drill = hasDrill
                ? `<td class="pe-drill-actions">${contextDrillFn(r, col)}</td>`
                : '';
            return `<tr>
                <td><code>${escapeHtml(String(col))}</code></td>
                <td>${escapeHtml(r.sheet_name || '')}</td>
                <td>${escapeHtml(String(exp))}</td>
                <td class="pe-bad">${escapeHtml(String(act))}</td>
                ${drill}
            </tr>`;
        }).join('');
        const more = rest > 0 ? `<p class="pe-more muted">${rest} more dimension mismatch(es).</p>` : '';
        return `${head}${body}</tbody></table>${more}`;
    }

    function renderContextInspectorHtml(data) {
        if (!data || typeof data !== 'object') {
            return '<p class="muted">No context data.</p>';
        }
        const sheet = data.sheet_name || data.lineage?.sheet_name || '';
        const sid = data.source_id || '';
        const fp = data.context_fingerprint || '';
        const cache = data.artifact_cache || {};
        const cacheBadge = cache.hit
            ? '<span class="pe-ctx-badge pe-ctx-badge--hit">artifact cache hit</span>'
            : '<span class="pe-ctx-badge pe-ctx-badge--miss">rebuilt</span>';

        const lineage = data.lineage && typeof data.lineage === 'object' ? data.lineage : {};
        const lineageRows = ['job_id', 'source_id', 'sheet_name', 'file_name']
            .filter((k) => lineage[k])
            .map((k) => `<tr><td><code>${escapeHtml(k)}</code></td><td>${escapeHtml(String(lineage[k]))}</td></tr>`)
            .join('');

        const scoped = Array.isArray(data.scoped_fields) ? data.scoped_fields : [];
        const local = data.local_context && typeof data.local_context === 'object' ? data.local_context : {};
        const fieldKeys = [...new Set([
            ...scoped.map((sf) => sf.field || sf.name),
            ...Object.keys(local),
        ].filter(Boolean))];
        const scopedRows = fieldKeys.map((k) => {
            const sf = scoped.find((r) => (r.field || r.name) === k) || {};
            const val = sf.value != null ? sf.value : local[k];
            const prov = sf.provenance_summary || [sf.scope, sf.block_label, sf.evidence_line].filter(Boolean).join(' · ');
            return `<tr>
                <td><code>${escapeHtml(String(k))}</code></td>
                <td>${escapeHtml(String(val ?? ''))}</td>
                <td class="muted pe-prov">${escapeHtml(String(prov || ''))}</td>
            </tr>`;
        }).join('');

        const evidence = (data.interpreted_context?.evidence || []).slice(0, 12);
        const evidenceHtml = evidence.length
            ? `<ul class="pe-ctx-evidence">${evidence.map((e) => {
                if (typeof e === 'string') return `<li>${escapeHtml(e)}</li>`;
                const line = e.line || e.text || e.summary || JSON.stringify(e);
                const blk = e.block_label ? ` <span class="muted">(${escapeHtml(e.block_label)})</span>` : '';
                return `<li>${escapeHtml(String(line))}${blk}</li>`;
            }).join('')}</ul>`
            : '<p class="muted">No evidence lines recorded.</p>';

        const snippets = Array.isArray(data.context_block_snippets) ? data.context_block_snippets : [];
        const snippetHtml = snippets.length
            ? `<ul class="pe-ctx-snippets">${snippets.map((s) => {
                const label = s.block_label || s.block_id || 'block';
                const sum = s.summary ? ` — ${escapeHtml(String(s.summary))}` : '';
                return `<li><code>${escapeHtml(String(label))}</code>${sum}</li>`;
            }).join('')}</ul>`
            : '';

        return `
            <div class="pe-context-inspector">
                <div class="pe-ctx-head">
                    <strong>${escapeHtml(sheet || sid || 'Context')}</strong>
                    ${fp ? `<span class="muted pe-ctx-fp" title="context fingerprint">${escapeHtml(String(fp).slice(0, 12))}…</span>` : ''}
                    ${cacheBadge}
                </div>
                ${lineageRows ? `<table class="pe-mismatch-table pe-ctx-lineage"><tbody>${lineageRows}</tbody></table>` : ''}
                ${scopedRows ? `
                    <div class="pe-ctx-section">
                        <div class="pe-issue-block-title">Scoped fields</div>
                        <table class="pe-mismatch-table">
                            <thead><tr><th>Field</th><th>Value</th><th>Provenance</th></tr></thead>
                            <tbody>${scopedRows}</tbody>
                        </table>
                    </div>` : ''}
                <div class="pe-ctx-section">
                    <div class="pe-issue-block-title">Evidence</div>
                    ${evidenceHtml}
                </div>
                ${snippetHtml ? `<div class="pe-ctx-section"><div class="pe-issue-block-title">Context blocks</div>${snippetHtml}</div>` : ''}
            </div>`;
    }

    function renderIssueGroup(title, issues, opts) {
        const { severity, snapshotLinkFn, contextDrillFn, metricTableMax } = opts || {};
        if (!issues.length) {
            return `<p class="muted pe-empty-group">No ${severity || ''} issues.</p>`;
        }
        const metricRows = issues.filter((i) => i.type === 'metric_mismatch');
        const dimensionRows = issues.filter((i) => i.type === 'dimension_mismatch');
        const other = issues.filter((i) => i.type !== 'metric_mismatch' && i.type !== 'dimension_mismatch');

        let html = '';
        if (dimensionRows.length) {
            html += `<div class="pe-issue-block">
                <div class="pe-issue-block-title">${escapeHtml(typeLabel('dimension_mismatch'))} <span class="muted">(${dimensionRows.length})</span></div>
                <p class="pe-issue-hint muted">Output channel/market values disagree with this sheet&apos;s local context — likely cross-sheet bleed.</p>
                ${renderDimensionMismatchTable(dimensionRows, metricTableMax, contextDrillFn)}
            </div>`;
        }
        if (metricRows.length) {
            html += `<div class="pe-issue-block">
                <div class="pe-issue-block-title">${escapeHtml(typeLabel('metric_mismatch'))} <span class="muted">(${metricRows.length})</span></div>
                <p class="pe-issue-hint muted">Processed spends/impressions do not match this sheet&apos;s clean template — likely context bleed or wrong allocation.</p>
                ${renderMetricMismatchTable(metricRows, metricTableMax)}
            </div>`;
        }
        other.forEach((v) => {
            const snap = snapshotLinkFn ? snapshotLinkFn(v) : '';
            html += `
                <div class="pe-issue-card pe-issue-card--${v.severity}">
                    <div class="pe-issue-card-head">
                        <span class="pe-issue-type">${escapeHtml(v.type_label)}</span>
                        <span class="pe-issue-meta">${escapeHtml(v.stage_label)}${v.sheet_name ? ` · ${escapeHtml(v.sheet_name)}` : ''}</span>
                    </div>
                    <p class="pe-issue-msg">${escapeHtml(v.message || '')}</p>
                    ${snap ? `<div class="pe-issue-actions">${snap}</div>` : ''}
                </div>`;
        });
        return html;
    }

    function renderStageChips(bucket) {
        return STAGE_ORDER.map((st) => {
            const row = bucket?.[st];
            const notRun = !row;
            const ok = notRun || row.pass !== false;
            const fail = row && row.pass === false;
            const cls = notRun ? 'pe-chip--skip' : fail ? 'pe-chip--fail' : 'pe-chip--pass';
            return `<span class="pe-stage-chip ${cls}" title="${escapeHtml(stageLabel(st))}">${escapeHtml(stageLabel(st))}</span>`;
        }).join('');
    }

    function renderSourceCard(sid, bucket, sourceLabels, issuesForSource, snapshotLinkFn, contextDrillFn) {
        const sheet = sheetFromSourceId(sid, sourceLabels);
        const file = sourceLabels[sid]?.file_name || '';
        const critical = issuesForSource.filter((i) => i.severity === 'critical');
        const advisory = issuesForSource.filter((i) => i.severity === 'advisory');
        const hasFail = critical.length > 0;
        const localContext = bucket?.context_isolation?.metrics?.local_context;
        const scopedLocalContext = bucket?.context_isolation?.metrics?.scoped_local_context;
        const localContextHtml = (localContext || scopedLocalContext)
            ? renderLocalContextBlock(localContext, scopedLocalContext)
            : '';

        return `
            <details class="pe-source-card" ${hasFail ? 'open' : ''}>
                <summary class="pe-source-summary">
                    <span class="pe-source-name">${escapeHtml(sheet)}</span>
                    ${file ? `<span class="pe-source-file muted">${escapeHtml(file)}</span>` : ''}
                    <span class="pe-source-counts">
                        ${critical.length ? `<span class="pe-count-critical">${critical.length} critical</span>` : ''}
                        ${advisory.length ? `<span class="pe-count-advisory">${advisory.length} advisory</span>` : ''}
                    </span>
                </summary>
                <div class="pe-source-chips">${renderStageChips(bucket)}</div>
                ${localContextHtml}
                <div class="pe-context-link-wrap">
                    <button type="button" class="btn btn-ghost btn-sm pe-context-btn"
                        data-source-id="${escapeHtml(sid)}">View context provenance</button>
                    <div class="pe-context-inspector-host" hidden></div>
                </div>
                ${critical.length ? `<div class="pe-source-section"><h5 class="pe-source-section-title">Critical</h5>${renderIssueGroup('Critical', critical, { severity: 'critical', snapshotLinkFn, contextDrillFn, metricTableMax: 12 })}</div>` : ''}
                ${advisory.length ? `<div class="pe-source-section"><h5 class="pe-source-section-title">Advisory</h5>${renderIssueGroup('Advisory', advisory, { severity: 'advisory', snapshotLinkFn, contextDrillFn, metricTableMax: 4 })}</div>` : ''}
                ${!critical.length && !advisory.length ? '<p class="muted">All checks passed for this source.</p>' : ''}
            </details>`;
    }

    function renderContextTrailShell() {
        return `
            <section class="pe-section context-trail-panel-wrap" id="contextTrailPanel">
                <header class="context-trail-header">
                    <h4 class="pe-section-title context-trail-title">Context trail</h4>
                    <p class="muted context-trail-help">Field changes across packet build, plan rebind, stamp, and assess.</p>
                    <div class="context-trail-filters">
                        <label>Source
                            <select id="contextTrailSourceFilter" class="context-trail-select"></select>
                        </label>
                        <label>Field
                            <input type="text" id="contextTrailFieldFilter" class="context-trail-input" placeholder="e.g. market" />
                        </label>
                        <button type="button" class="btn btn-ghost btn-sm" id="contextTrailClearBtn">Clear</button>
                    </div>
                </header>
                <div id="contextTrailContent" class="context-trail-timeline"></div>
            </section>`;
    }

    function renderPipelineEvalsPanel(pe, options) {
        const opts = options || {};
        const sourceLabels = { ...buildLabelsFromRegistry(opts.sourceRegistry), ...(opts.evalDisplay?.source_labels || {}) };
        const gate = pe?.critical_gate || {};
        const gatePass = gate.pass !== false;
        const overridden = !!gate.overridden;
        const evalDisplay = opts.evalDisplay || {};
        const headline = evalDisplay.headline || '';
        const issues = (evalDisplay.issues && evalDisplay.issues.length)
            ? evalDisplay.issues
            : flattenViolations(pe, sourceLabels);

        let bannerClass = 'pe-banner--pass';
        let bannerTitle = 'Export safe';
        if (overridden) {
            bannerClass = 'pe-banner--override';
            bannerTitle = 'Override logged — export allowed';
        } else if (!gatePass) {
            bannerClass = 'pe-banner--fail';
            bannerTitle = 'Export blocked';
        } else if (evalDisplay.verdict === 'advisory') {
            bannerClass = 'pe-banner--warn';
            bannerTitle = 'Advisory signals only';
        }

        const snapshotLinkFn = opts.snapshotLinkFn || null;
        const contextDrillFn = opts.contextDrillFn || null;
        const per = pe?.per_source || {};
        const sids = Object.keys(per);
        const bySource = {};
        issues.forEach((i) => {
            const sid = i.source_id || '__collation__';
            if (!bySource[sid]) bySource[sid] = [];
            bySource[sid].push(i);
        });

        const sourceCards = sids.map((sid) =>
            renderSourceCard(sid, per[sid], sourceLabels, bySource[sid] || [], snapshotLinkFn, contextDrillFn)
        ).join('');

        const coll = pe?.collation || {};
        const collViol = (coll.violations || []).length;
        const collHtml = collViol || coll.metrics
            ? `<section class="pe-section">
                <h4 class="pe-section-title">Collation</h4>
                <p class="muted">Union false duplicates: <strong>${Number(coll.metrics?.union_false_duplicates || 0)}</strong></p>
                ${collViol ? renderIssueGroup('Collation', issues.filter((i) => i.stage === 'collation'), { snapshotLinkFn }) : ''}
               </section>`
            : '';

        const blockedList = (gate.blocked_reasons || []).length
            ? `<ul class="pe-blocked-list">${gate.blocked_reasons.map((r) => `<li><code>${escapeHtml(r)}</code></li>`).join('')}</ul>`
            : '';

        const nextSteps = (evalDisplay.next_steps || []).length
            ? `<ul class="pe-next-steps">${evalDisplay.next_steps.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>`
            : '';

        return `
            <div class="pipeline-evals-panel" data-job-id="${escapeHtml(opts.jobId || '')}">
                <div class="pe-banner ${bannerClass}">
                    <div>
                        <h3 class="pe-banner-title">${bannerTitle}</h3>
                        ${headline ? `<p class="pe-banner-sub">${escapeHtml(headline)}</p>` : ''}
                    </div>
                    <div class="pe-banner-stats">
                        <span class="pe-stat-critical">${evalDisplay.counts?.critical ?? issues.filter((i) => i.severity === 'critical').length} critical</span>
                        <span class="pe-stat-advisory">${evalDisplay.counts?.advisory ?? issues.filter((i) => i.severity === 'advisory').length} advisory</span>
                    </div>
                </div>
                ${nextSteps}
                ${blockedList}
                ${overridden ? '<p class="pe-override-note">Analyst override is on record for this job.</p>' : ''}
                <section class="pe-section">
                    <h4 class="pe-section-title">Per source</h4>
                    <p class="pe-section-hint muted">Expand a sheet for local context, stage chips, and
                    <strong>View context provenance</strong>. Context trail (field changes) is below.</p>
                    <div class="pe-source-list">${sourceCards || '<p class="muted">No per-source evals yet.</p>'}</div>
                </section>
                ${renderContextTrailShell()}
                ${collHtml}
            </div>`;
    }

    function wireContextProvenanceButtons(root) {
        const panel = root && root.querySelector ? root : document;
        const jobId = panel.querySelector?.('.pipeline-evals-panel')?.dataset?.jobId;
        if (!jobId) return;
        panel.querySelectorAll('.pe-context-btn').forEach((btn) => {
            if (btn.dataset.wired === '1') return;
            btn.dataset.wired = '1';
            btn.addEventListener('click', async () => {
                const sid = btn.getAttribute('data-source-id');
                const host = btn.parentElement?.querySelector('.pe-context-inspector-host');
                if (!sid || !host) return;
                const open = !host.hidden;
                if (open && host.dataset.loaded === '1') {
                    host.hidden = true;
                    return;
                }
                host.hidden = false;
                host.innerHTML = '<p class="muted">Loading context…</p>';
                try {
                    const fetchFn = global.fetchJson;
                    const url = `/api/debug/${encodeURIComponent(jobId)}/context/${encodeURIComponent(sid)}`;
                    const data = fetchFn
                        ? await fetchFn(url)
                        : await (await fetch(url)).json();
                    host.innerHTML = renderContextInspectorHtml(data);
                    host.dataset.loaded = '1';
                } catch (err) {
                    host.innerHTML = `<p class="pe-bad">${escapeHtml(String(err?.message || err))}</p>`;
                }
            });
        });
    }

    global.PipelineEvalsRender = {
        STAGE_LABELS,
        TYPE_LABELS,
        typeLabel,
        stageLabel,
        sheetFromSourceId,
        buildLabelsFromRegistry,
        flattenViolations,
        renderMetricMismatchTable,
        renderContextInspectorHtml,
        renderIssueGroup,
        renderContextTrailShell,
        renderPipelineEvalsPanel,
        wireContextProvenanceButtons,
    };
})(typeof window !== 'undefined' ? window : globalThis);
