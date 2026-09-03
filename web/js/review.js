/**
 * Review Page - HITL Review Queue
 */

const reviewState = {
    approved: 0,
    rejected: 0,
    items: [],
    selectedCheckpointId: null,
    selectedJobId: null,
};

/** Escape a string so it is safe inside a double-quoted HTML attribute. */
function escapeAttr(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function escapeJsString(value) {
    return String(value ?? '')
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'");
}

/** Plan/checkpoint confidence for queue badges (plan_confidence beats stale trajectory min). */
function reviewItemConfidence(item) {
    const planConf = item?.trigger_data?.plan_confidence;
    if (planConf != null && Number.isFinite(Number(planConf))) {
        return Number(planConf);
    }
    const raw = Number(item?.confidence);
    return Number.isFinite(raw) ? raw : 0.5;
}

function formatConfidencePct(confidence) {
    return `${(confidence * 100).toFixed(0)}%`;
}

/** After review completes, open Results (downloads live on debug.html). */
window.goToResultsForJob = function (jobId) {
    const j = String(jobId || '').trim();
    if (j) setCurrentJob(j);
    navigateTo('debug');
};

/** While a run is still in flight, send analysts to Console. */
window.goToProcessingForJob = function (jobId) {
    const j = String(jobId || '').trim();
    if (j) setCurrentJob(j);
    navigateTo('processing');
};

function truncateReviewSnippet(text, maxLen = 160) {
    const t = String(text || '').replace(/\s+/g, ' ').trim();
    if (t.length <= maxLen) return t;
    return `${t.slice(0, maxLen - 1)}…`;
}

/** Canonical relationship kinds stored on checkpoints / graph (values must stay stable for the API). */
const RELATIONSHIP_KIND_OPTIONS = [
    {
        value: 'union',
        label: 'Union — stack rows (append sheets that describe the same measures)',
    },
    {
        value: 'lookup/reference',
        label: 'Lookup / reference — link without stacking the full fact table',
    },
    {
        value: 'join',
        label: 'Join — merge side-by-side on shared keys (you must list join keys)',
    },
    {
        value: 'independent',
        label: 'Independent — keep sources separate in this relationship',
    },
];

function _joinKeysDisplayFromProposal(proposal) {
    const jk = proposal && proposal.join_keys;
    if (Array.isArray(jk)) {
        const parts = jk
            .map((entry) => {
                if (entry && typeof entry === 'object' && (entry.from || entry.to)) {
                    const f = entry.from || entry.source || '';
                    const t = entry.to || entry.target || '';
                    if (f && t && f !== t) return `${f}→${t}`;
                    return f || t || '';
                }
                return String(entry ?? '').trim();
            })
            .filter(Boolean);
        return parts.join(', ');
    }
    return String((proposal && proposal.join_keys_csv) || '').trim();
}

/** Primary proposal to headline (prefer union across main sources). */
function _pickHeroProposal(proposals) {
    const list = Array.isArray(proposals) ? proposals : [];
    const union = list.find((p) => String(p.relationship_kind || '').toLowerCase().includes('union'));
    return union || list[0] || null;
}

/** Short analyst-facing operation title. */
function _combineOperationLabel(kindRaw) {
    const k = String(kindRaw || '').trim().toLowerCase();
    if (k.includes('union')) return { headline: 'UNION', sub: 'Stack rows' };
    if (k === 'join' || k.startsWith('join')) return { headline: 'JOIN', sub: 'Merge on keys' };
    if (k.includes('lookup') || k.includes('reference')) return { headline: 'LOOKUP', sub: 'Reference link' };
    if (k.includes('independent')) return { headline: 'INDEPENDENT', sub: 'Keep separate' };
    return { headline: 'COMBINE', sub: 'Confirm link' };
}

function _sourcesFileTreeHtml(sources) {
    const list = Array.isArray(sources) ? sources : [];
    const byFile = new Map();
    for (const s of list) {
        const fn = String(s.file_name || 'Workbook').trim() || 'Workbook';
        const sh = String(s.sheet_name || '—').trim() || '—';
        if (!byFile.has(fn)) byFile.set(fn, []);
        byFile.get(fn).push(sh);
    }
    const parts = [];
    for (const [file, sheets] of byFile) {
        const lines = sheets.map((sh) => `<li>${escapeHtml(sh)}</li>`).join('');
        parts.push(
            `<div class="combine-source-file"><div class="combine-source-file-name">${escapeHtml(file)}</div><ul class="combine-source-sheet-list">${lines}</ul></div>`,
        );
    }
    return parts.length ? parts.join('') : '<p class="muted">No source list attached.</p>';
}

function _alignmentColumnsLine(hero, sources) {
    if (!hero) return '—';
    const kind = String(hero.relationship_kind || '').toLowerCase();
    const keys = _joinKeysDisplayFromProposal(hero);
    if (kind.includes('union') || kind.includes('independent')) {
        if (keys) return keys;
    }
    const sids = Array.isArray(hero.source_ids) ? hero.source_ids.map(String) : [];
    const cols = new Set();
    const list = Array.isArray(sources) ? sources : [];
    for (const sid of sids) {
        const hit = list.find((s) => String(s.source_id || '') === sid);
        const mt = hit && Array.isArray(hit.mapped_targets) ? hit.mapped_targets : [];
        mt.forEach((c) => {
            if (c) cols.add(String(c));
        });
    }
    if (kind.includes('union') || kind.includes('independent')) {
        const line = Array.from(cols).slice(0, 16).join(' | ');
        return line || keys || '—';
    }
    const line = Array.from(cols).slice(0, 16).join(' | ');
    if (keys) return `Join on: ${keys}${line ? ` | ${line}` : ''}`;
    return line || keys || '—';
}

/** Table for duplicate-key preview with Outcome column styling (relationship review). */
function _renderDuplicateVisualSample(sample) {
    if (!sample || typeof sample !== 'object') return '';
    const cols = Array.isArray(sample.columns) ? sample.columns.map(String) : [];
    const raw = Array.isArray(sample.rows) ? sample.rows : [];
    if (!cols.length || !raw.length) return '';
    const outcomeIdx = cols.findIndex((c) => String(c) === 'Outcome');
    const title = sample.title ? escapeHtml(sample.title) : 'Duplicate key rows';
    const subtitle = sample.subtitle
        ? `<p class="combine-dedupe-visual__subtitle muted">${escapeHtml(sample.subtitle)}</p>`
        : '';
    const tbody = raw
        .map((row) => {
            const tds = cols.map((c, ci) => {
                const v = row && row[c] != null ? row[c] : '';
                const s = escapeHtml(String(v));
                if (ci === outcomeIdx) {
                    const t = String(v);
                    let cls = 'combine-dedupe-outcome';
                    if (t.includes('Removed')) cls += ' combine-dedupe-outcome--removed';
                    else if (t.includes('Would remove')) cls += ' combine-dedupe-outcome--removed';
                    else if (t.includes('Kept')) cls += ' combine-dedupe-outcome--kept';
                    else if (t.includes('Combined')) cls += ' combine-dedupe-outcome--merged';
                    return `<td class="${cls}">${s}</td>`;
                }
                return `<td>${s}</td>`;
            });
            return `<tr>${tds.join('')}</tr>`;
        })
        .join('');
    const thead = `<thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>`;
    return `
        <div class="combine-dedupe-visual">
            <div class="combine-dedupe-visual__head">
                <h4 class="combine-dedupe-visual__title">${title}</h4>
                ${subtitle}
            </div>
            <div class="combine-mini-table-wrap combine-mini-table-wrap--scroll-output combine-dedupe-visual__table">
                <table class="combine-mini-table combine-mini-table--dedupe-outcome">${thead}<tbody>${tbody}</tbody></table>
            </div>
        </div>`;
}

/** Build duplicate-check-style table payload from a deletion preview dict. */
function _buildRemovalVisualSampleFromPreview(p) {
    if (!p || typeof p !== 'object') return null;
    if (p.visual_sample && typeof p.visual_sample === 'object') return p.visual_sample;
    const sample = Array.isArray(p.sample_deleted_data) ? p.sample_deleted_data : [];
    if (!sample.length) return null;
    const removedRows = sample.map((row) => ({ ...row, Outcome: 'Would remove if you continue' }));
    const cols = [...Object.keys(removedRows[0] || {})];
    if (!cols.includes('Outcome')) cols.push('Outcome');
    const title = p.step_title || p.tool_name || 'Destructive step';
    return {
        title: `${title} — rows that would be removed`,
        subtitle: String(p.impact_summary || p.reason || '').trim(),
        columns: cols,
        rows: removedRows,
    };
}

function _renderMiniPreviewTable(columns, rows, maxRows = 3) {
    const cols = Array.isArray(columns) && columns.length ? columns.map(String) : [];
    const body = Array.isArray(rows) ? rows.slice(0, maxRows) : [];
    if (!cols.length && body.length && typeof body[0] === 'object') {
        cols.push(...Object.keys(body[0]));
    }
    if (!cols.length) return '<p class="muted">No columns</p>';
    const thead = `<thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>`;
    const tbody = body
        .map(
            (row) =>
                `<tr>${cols.map((c) => `<td>${escapeHtml(String(row && row[c] != null ? row[c] : ''))}</td>`).join('')}</tr>`,
        )
        .join('');
    return `<div class="combine-mini-table-wrap"><table class="combine-mini-table">${thead}<tbody>${tbody}</tbody></table></div>`;
}

/** Sticky-header scroll table for combined union preview; optional break rows between stacked sources. */
function _renderCombineOutputSampleTable(columns, rows, opts = {}) {
    const maxRows = opts.maxRows != null ? opts.maxRows : 160;
    const unionBreaks = Array.isArray(opts.unionBreakBeforeRow) ? opts.unionBreakBeforeRow : [];
    const breakSet = new Set(
        unionBreaks.map((n) => Number(n)).filter((n) => Number.isFinite(n) && n > 0),
    );
    let cols = Array.isArray(columns) && columns.length ? columns.map(String) : [];
    const raw = Array.isArray(rows) ? rows.slice(0, maxRows) : [];
    if (!cols.length && raw.length && typeof raw[0] === 'object') {
        cols.push(...Object.keys(raw[0]));
    }
    if (!cols.length) return '<p class="muted">No preview rows</p>';
    const ncol = cols.length;
    const sepLabel = '… next source …';
    const midEllipsisAt =
        raw.length > 4 && breakSet.size === 0 ? Math.max(1, Math.floor(raw.length / 2)) : -1;
    const parts = [];
    raw.forEach((row, idx) => {
        if (breakSet.has(idx)) {
            parts.push(
                `<tr class="combine-mini-separator-row"><td colspan="${ncol}" class="combine-mini-separator-cell">${escapeHtml(sepLabel)}</td></tr>`,
            );
        }
        if (midEllipsisAt === idx && idx > 0) {
            parts.push(
                `<tr class="combine-mini-ellipsis-row"><td colspan="${ncol}" class="combine-mini-separator-cell combine-mini-ellipsis-cell">⋯</td></tr>`,
            );
        }
        parts.push(
            `<tr>${cols.map((c) => `<td>${escapeHtml(String(row && row[c] != null ? row[c] : ''))}</td>`).join('')}</tr>`,
        );
    });
    const thead = `<thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join('')}</tr></thead>`;
    const tbody = `<tbody>${parts.join('')}</tbody>`;
    const total = opts.rowCountTotal != null ? Number(opts.rowCountTotal) : raw.length;
    const shown = raw.length;
    const capNote =
        Number.isFinite(total) && total > shown
            ? `<p class="combine-output-sample-meta muted">Showing first ${shown} of ${total} combined rows (stack order). Scroll to see rows from each file.</p>`
            : raw.length > 3
              ? `<p class="combine-output-sample-meta muted">Combined stack order — scroll to see rows from each source.</p>`
              : '';
    return `${capNote}<div class="combine-mini-table-wrap combine-mini-table-wrap--scroll-output"><table class="combine-mini-table combine-mini-table--output">${thead}${tbody}</table></div>`;
}

function _renderHiddenRelationshipFormCards(proposals, sources, heroIndex) {
    const list = Array.isArray(proposals) ? proposals : [];
    const hi = Number.isFinite(Number(heroIndex)) ? Number(heroIndex) : 0;
    return list
        .map((proposal, index) => {
            const sourceIds = Array.isArray(proposal.source_ids) ? proposal.source_ids : [];
            const currentKind = String(proposal.relationship_kind || '').trim();
            const knownKind = RELATIONSHIP_KIND_OPTIONS.some((o) => o.value === currentKind);
            const unknownKindOption =
                currentKind && !knownKind
                    ? `<option value="${escapeAttr(currentKind)}" selected>${escapeHtml(currentKind)} (model)</option>`
                    : '';
            const kindOptions =
                unknownKindOption
                + RELATIONSHIP_KIND_OPTIONS.map(
                    (o) =>
                        `<option value="${escapeAttr(o.value)}" ${
                            o.value === currentKind ? 'selected' : ''
                        }>${escapeHtml(o.label)}</option>`,
                ).join('');
            const joinKeysRaw = Array.isArray(proposal.join_keys)
                ? proposal.join_keys
                    .map((entry) => {
                        if (entry && typeof entry === 'object' && (entry.from || entry.to)) {
                            const f = entry.from || entry.source || '';
                            const t = entry.to || entry.target || '';
                            if (f && t && f !== t) return `${f}→${t}`;
                            return f || t || '';
                        }
                        return String(entry ?? '').trim();
                    })
                    .filter(Boolean)
                : [];
            const joinKeysStr = joinKeysRaw.length ? joinKeysRaw.join(', ') : '';
            const joinRowClass =
                currentKind.includes('join') || currentKind.includes('lookup') || currentKind.includes('reference')
                    ? ''
                    : ' combine-join-row--muted';
            return `
        <div class="combine-form-card mapping-review-card" data-relationship-card="${index}" data-relationship-id="${escapeAttr(proposal.relationship_id || `relationship_${index}`)}" data-hero-card="${index === hi ? '1' : '0'}">
            <div class="mapping-review-meta relationship-review-kind-row">
                <span class="muted">Link ${index + 1}</span>
                <select class="planner-rule-input" data-relationship-field="relationship_kind" aria-label="Relationship kind">
                    ${kindOptions}
                </select>
            </div>
            <div class="mapping-review-meta relationship-review-join-row${joinRowClass}" data-relationship-join-row>
                <span>Join keys</span>
                <input class="planner-rule-input" type="text" data-relationship-field="join_keys" value="${escapeHtml(joinKeysStr)}" placeholder="e.g. date, channel" aria-label="Join keys">
            </div>
            <p class="muted planner-review-help planner-review-help--tight" style="margin:4px 0 0">${escapeHtml(sourceIds.join(' · ') || 'sources')}</p>
        </div>`;
        })
        .join('');
}

function _wireCombineChangeDrawer(root) {
    const rootEl = root && root.classList && root.classList.contains('combine-confirm') ? root : root && root.querySelector && root.querySelector('.combine-confirm');
    if (!rootEl) return;
    const toggle = rootEl.querySelector('[data-toggle-combine-edit]');
    const drawer = rootEl.querySelector('#combineChangeDrawer');
    const syncJoinRows = () => {
        rootEl.querySelectorAll('[data-relationship-card]').forEach((card) => {
            const sel = card.querySelector('[data-relationship-field="relationship_kind"]');
            const row = card.querySelector('[data-relationship-join-row]');
            if (!sel || !row) return;
            const v = String(sel.value || '').toLowerCase();
            const show = v.includes('join') || v.includes('lookup') || v.includes('reference');
            row.classList.toggle('combine-join-row--muted', !show);
            const inp = row.querySelector('input');
            if (inp) inp.disabled = !show;
        });
    };
    rootEl.querySelectorAll('[data-relationship-field="relationship_kind"]').forEach((el) => {
        el.addEventListener('change', () => {
            const card = el.closest('[data-relationship-card]');
            const opEl = rootEl.querySelector('[data-combine-op-display]');
            if (opEl && card && card.getAttribute('data-hero-card') === '1') {
                const lab = _combineOperationLabel(el.value);
                opEl.innerHTML = `<strong>${escapeHtml(lab.headline)}</strong> <span class="muted">(${escapeHtml(lab.sub)})</span>`;
            }
            syncJoinRows();
        });
    });
    syncJoinRows();
    if (toggle && drawer) {
        toggle.addEventListener('click', () => {
            const open = drawer.hasAttribute('hidden');
            if (open) {
                drawer.removeAttribute('hidden');
                toggle.setAttribute('aria-expanded', 'true');
            } else {
                drawer.setAttribute('hidden', '');
                toggle.setAttribute('aria-expanded', 'false');
            }
        });
    }
}

function _dupDecisionFromPayload(decisions, gid, category, fallbackAction) {
    const d = decisions && typeof decisions === 'object' ? decisions[gid] : null;
    if (d && d.category === category && d.action) return String(d.action);
    return fallbackAction;
}

const DUP_SOURCE_COLUMNS = ['file_name', 'sheet_name'];
const DUP_SOURCE_COLUMN_LABELS = { file_name: 'File', sheet_name: 'Sheet' };

function _renderDupGroupTable(rows) {
    const raw = Array.isArray(rows) ? rows : [];
    if (!raw.length) return '<p class="muted">No rows</p>';
    const dataCols = Object.keys(raw[0] || {}).filter(
        (c) => !String(c).startsWith('_') && !DUP_SOURCE_COLUMNS.includes(c),
    );
    const cols = [
        ...DUP_SOURCE_COLUMNS.filter((c) => raw.some((r) => String(r[c] || '').trim())),
        ...dataCols.sort(),
    ];
    if (!cols.length) return '<p class="muted">No columns</p>';
    const thead = `<thead><tr>${cols
        .map((c) => `<th class="${DUP_SOURCE_COLUMNS.includes(c) ? 'dup-source-col' : ''}">${escapeHtml(DUP_SOURCE_COLUMN_LABELS[c] || c)}</th>`)
        .join('')}</tr></thead>`;
    const tbody = raw
        .map(
            (row) =>
                `<tr>${cols
                    .map(
                        (c) =>
                            `<td class="${DUP_SOURCE_COLUMNS.includes(c) ? 'dup-source-col' : ''}">${escapeHtml(String(row && row[c] != null ? row[c] : ''))}</td>`,
                    )
                    .join('')}</tr>`,
        )
        .join('');
    return `<div class="combine-mini-table-wrap"><table class="combine-mini-table combine-mini-table--dup-sources">${thead}<tbody>${tbody}</tbody></table></div>`;
}

function _dupChoiceBtn(value, label, variant, selected) {
    const sel = selected ? ' is-selected' : '';
    return `<button type="button" class="dup-choice-btn dup-choice-btn--${variant}${sel}" data-dup-value="${escapeAttr(String(value))}" aria-pressed="${selected ? 'true' : 'false'}">${label}</button>`;
}

function _dupGroupLikelyContextBleed(rows) {
    const raw = Array.isArray(rows) ? rows : [];
    if (raw.length < 2) return false;
    const sheets = [...new Set(raw.map((r) => String(r?.sheet_name || '').trim()).filter(Boolean))];
    if (sheets.length < 2) return false;
    const dimCols = ['market', 'channel', 'Market', 'Channel'];
    for (const col of dimCols) {
        const vals = raw
            .map((r) => String(r?.[col] ?? '').trim())
            .filter(Boolean);
        if (vals.length >= 2 && new Set(vals).size === 1) return true;
    }
    return false;
}

function _renderDuplicateKeyGroupSections(dup) {
    const exact = Array.isArray(dup.exact_duplicate_groups) ? dup.exact_duplicate_groups : [];
    const partial = Array.isArray(dup.partial_duplicate_groups) ? dup.partial_duplicate_groups : [];
    const decisions = dup.duplicate_key_group_decisions && typeof dup.duplicate_key_group_decisions === 'object' ? dup.duplicate_key_group_decisions : {};
    const s1 = escapeHtml(String(dup.source_1_label || 'Source 1'));
    const s2 = escapeHtml(String(dup.source_2_label || 'Source 2'));
    if (!exact.length && !partial.length) return '';
    const stackHint = Array.isArray(dup.union_source_stack) && dup.union_source_stack.length
        ? `<p class="dup-section__hint muted">Union stack order: ${dup.union_source_stack
              .map((s) => escapeHtml([s.file_name, s.sheet_name].filter(Boolean).join(' · ') || s.source_id || ''))
              .join(' → ')}. Each duplicate row shows its <strong>File</strong> and <strong>Sheet</strong> — same file + sheet means both rows came from that source&apos;s processed output; different sheets are separate regional tabs stacked together.</p>`
        : `<p class="dup-section__hint muted">Each duplicate row shows which <strong>File</strong> and <strong>Sheet</strong> it came from after per-source processing.</p>`;
    const allGroups = [...exact, ...partial];
    const bleedGroups = allGroups.filter((g) => _dupGroupLikelyContextBleed(g.rows));
    const bleedBanner = bleedGroups.length
        ? `<div class="dup-bleed-banner" role="note">
            <strong>Likely context bleed</strong> — ${bleedGroups.length} duplicate group(s) span different sheets but share the same market/channel.
            Keys may match because context was copied across sources; check Debug → Pipeline Evals → context trail before treating as true duplicates.
           </div>`
        : '';
    let html = stackHint + bleedBanner;
    if (exact.length) {
        html += `<section class="dup-section" aria-label="Exact duplicate key rows"><h4 class="dup-section__title">Exact duplicates <span class="muted">(same keys and same measures)</span></h4>
            <div class="dup-section__actions dup-section__actions--prominent">
                <button type="button" class="dup-apply-all-btn dup-apply-all-btn--exact-dup" data-dup-apply-all="exact" data-dup-value="treat_duplicate">Apply to all: treat as duplicate</button>
                <button type="button" class="dup-apply-all-btn dup-apply-all-btn--exact-sep" data-dup-apply-all="exact" data-dup-value="treat_separate">Apply to all: treat as separate</button>
            </div>`;
        for (const g of exact) {
            const gid = escapeAttr(String(g.group_id || ''));
            const glab = escapeHtml(String(g.key_display || gid));
            const cur = _dupDecisionFromPayload(decisions, String(g.group_id), 'exact', 'treat_duplicate');
            const bleedNote = _dupGroupLikelyContextBleed(g.rows)
                ? '<p class="dup-bleed-hint muted">Different sheets, same market/channel — may be context bleed, not a true duplicate.</p>'
                : '';
            html += `<div class="dup-group-card" data-dup-group-card="1" data-dup-gid="${gid}" data-dup-category="exact">
                <div class="dup-group-card__head">
                    <code class="dup-group-card__keys">${glab}</code>
                    <div class="dup-group-card__toolbar" role="toolbar" aria-label="Choice for this duplicate group">
                        ${_dupChoiceBtn('treat_duplicate', 'Treat as duplicate', 'exact-dup', cur === 'treat_duplicate')}
                        ${_dupChoiceBtn('treat_separate', 'Treat as separate', 'exact-sep', cur === 'treat_separate')}
                    </div>
                </div>
                ${bleedNote}
                <div class="dup-group-card__body">${_renderDupGroupTable(g.rows)}</div>
            </div>`;
        }
        html += '</section>';
    }
    if (partial.length) {
        html += `<section class="dup-section" aria-label="Partial duplicate key rows"><h4 class="dup-section__title">Partial duplicates <span class="muted">(same keys, different measures)</span></h4>
            <div class="dup-section__actions dup-section__actions--prominent">
                <button type="button" class="dup-apply-all-btn dup-apply-all-btn--partial-s1" data-dup-apply-all="partial" data-dup-value="keep_source_1">Apply to all: keep ${s1}</button>
                <button type="button" class="dup-apply-all-btn dup-apply-all-btn--partial-s2" data-dup-apply-all="partial" data-dup-value="keep_source_2">Apply to all: keep ${s2}</button>
                <button type="button" class="dup-apply-all-btn dup-apply-all-btn--partial-sum" data-dup-apply-all="partial" data-dup-value="keep_both">Apply to all: keep both (sum)</button>
            </div>`;
        for (const g of partial) {
            const gid = escapeAttr(String(g.group_id || ''));
            const glab = escapeHtml(String(g.key_display || gid));
            const cur = _dupDecisionFromPayload(decisions, String(g.group_id), 'partial', 'keep_both');
            const bleedNote = _dupGroupLikelyContextBleed(g.rows)
                ? '<p class="dup-bleed-hint muted">Different sheets, same market/channel — may be context bleed, not a true duplicate.</p>'
                : '';
            html += `<div class="dup-group-card" data-dup-group-card="1" data-dup-gid="${gid}" data-dup-category="partial">
                <div class="dup-group-card__head">
                    <code class="dup-group-card__keys">${glab}</code>
                    <div class="dup-group-card__toolbar" role="toolbar" aria-label="Choice for this partial duplicate group">
                        ${_dupChoiceBtn('keep_source_1', `Keep ${s1}`, 'partial-s1', cur === 'keep_source_1')}
                        ${_dupChoiceBtn('keep_source_2', `Keep ${s2}`, 'partial-s2', cur === 'keep_source_2')}
                        ${_dupChoiceBtn('keep_both', 'Keep both (sum)', 'partial-sum', cur === 'keep_both')}
                    </div>
                </div>
                ${bleedNote}
                <div class="dup-group-card__body">${_renderDupGroupTable(g.rows)}</div>
            </div>`;
        }
        html += '</section>';
    }
    return html;
}

function collectDuplicateKeyGroupDecisionsFromMount(mount) {
    const map = {};
    if (!mount) return map;
    mount.querySelectorAll('.dup-group-card[data-dup-gid]').forEach((card) => {
        const gid = card.getAttribute('data-dup-gid');
        const cat = card.getAttribute('data-dup-category');
        if (!gid || !cat) return;
        const sel = card.querySelector('.dup-choice-btn.is-selected');
        if (!sel) return;
        const act = sel.getAttribute('data-dup-value');
        if (!act) return;
        map[gid] = { category: cat, action: act };
    });
    return map;
}

async function hydrateRelationshipSamples(checkpointId, duplicateMergeMode, duplicateKeyGroupDecisions) {
    const mount = document.getElementById('combinePreviewMount');
    if (!mount) return;
    mount.innerHTML = '<div class="loading">Loading samples…</div>';
    try {
        const mode = String(duplicateMergeMode || 'auto').trim() || 'auto';
        const dec = duplicateKeyGroupDecisions && typeof duplicateKeyGroupDecisions === 'object' ? duplicateKeyGroupDecisions : {};
        const url = `/api/review/checkpoint/${encodeURIComponent(checkpointId)}/relationship-samples`;
        const data = await fetchJson(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                duplicate_merge_mode: mode,
                duplicate_key_group_decisions: dec,
            }),
        });
        const inputs = Array.isArray(data.inputs) ? data.inputs : [];
        let html = '<div class="combine-preview-row">';
        for (const inp of inputs) {
            const title = `${inp.file_name || ''} · ${inp.sheet_name || ''}`.trim() || 'Source';
            html += `<div class="combine-preview-block"><div class="combine-preview-block-title">${escapeHtml(title)}</div>${_renderMiniPreviewTable(inp.columns, inp.rows, 3)}</div>`;
        }
        html += '</div>';
        const dup = data.union_duplicate_preview;
        if (dup && typeof dup === 'object') {
            const rb = dup.row_count_before_stack != null ? Number(dup.row_count_before_stack) : null;
            const rafter = dup.row_count_after_merge_tools != null ? Number(dup.row_count_after_merge_tools) : null;
            const rd = dup.rows_removed_duplicate != null ? Number(dup.rows_removed_duplicate) : 0;
            const rag = dup.rows_collapsed_by_aggregate != null ? Number(dup.rows_collapsed_by_aggregate) : 0;
            const net = dup.rows_net_reduced != null ? Number(dup.rows_net_reduced) : null;
            const statsBits = [];
            if (rb != null && Number.isFinite(rb)) {
                statsBits.push(`Before stack: <strong>${escapeHtml(String(rb))}</strong> rows`);
            }
            if (rd > 0) statsBits.push(`Removed (dedupe): <strong>${escapeHtml(String(rd))}</strong>`);
            if (rag > 0) statsBits.push(`Collapsed (sum per key): <strong>${escapeHtml(String(rag))}</strong>`);
            if (net != null && Number.isFinite(net) && net > 0 && !rd && !rag) {
                statsBits.push(`Net rows reduced: <strong>${escapeHtml(String(net))}</strong>`);
            }
            if (rafter != null && Number.isFinite(rafter)) {
                statsBits.push(`After merge: <strong>${escapeHtml(String(rafter))}</strong>`);
            }
            const statsRow =
                statsBits.length > 0
                    ? `<p class="combine-dedupe-stats muted" style="margin:0 0 8px;font-size:12px">${statsBits.join(' · ')}</p>`
                    : '';
            const sections = _renderDuplicateKeyGroupSections(dup);
            const legacyVisual = sections ? '' : _renderDuplicateVisualSample(dup.visual_sample);
            html += `
                <aside class="combine-dedupe-callout" aria-label="Duplicate handling for union">
                    <div class="combine-dedupe-callout__title">Duplicates and merge tools</div>
                    ${statsRow}
                    ${sections || legacyVisual}
                </aside>
            `;
        }
        if (data.output_sample && Array.isArray(data.output_sample.rows) && data.output_sample.rows.length) {
            const br = data.output_sample.union_break_before_row;
            const meta = {
                unionBreakBeforeRow: Array.isArray(br) ? br : [],
                rowCountTotal: data.output_sample.row_count_total,
                maxRows: 160,
            };
            html += `<div class="combine-preview-out" aria-label="Output preview"><div class="combine-preview-arrow">→</div><div class="combine-preview-block combine-preview-block--out"><div class="combine-preview-block-title">Output (sample)</div>${_renderCombineOutputSampleTable(data.output_sample.columns, data.output_sample.rows, meta)}</div></div>`;
        } else {
            html += `<p class="muted combine-preview-fallback">Output shape updates after you confirm; preview uses current mappings.</p>`;
        }
        mount.innerHTML = html;
        mount.dataset.relationshipCheckpointId = String(checkpointId || '');
        mount.dataset.duplicateMergeMode = String(duplicateMergeMode || 'auto');

        const refresh = () => {
            const ck = mount.dataset.relationshipCheckpointId;
            const m = mount.dataset.duplicateMergeMode || 'auto';
            const next = collectDuplicateKeyGroupDecisionsFromMount(mount);
            void hydrateRelationshipSamples(ck, m, next);
        };

        mount.querySelectorAll('[data-dup-apply-all]').forEach((btn) => {
            btn.addEventListener('click', () => {
                const cat = btn.getAttribute('data-dup-apply-all');
                const val = btn.getAttribute('data-dup-value');
                if (!cat || !val) return;
                mount.querySelectorAll(`.dup-group-card[data-dup-category="${cat}"]`).forEach((card) => {
                    card.querySelectorAll('.dup-choice-btn').forEach((b) => {
                        const on = b.getAttribute('data-dup-value') === val;
                        b.classList.toggle('is-selected', on);
                        b.setAttribute('aria-pressed', on ? 'true' : 'false');
                    });
                });
                refresh();
            });
        });
        mount.querySelectorAll('.dup-group-card .dup-choice-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                const card = btn.closest('.dup-group-card');
                if (!card) return;
                card.querySelectorAll('.dup-choice-btn').forEach((b) => {
                    b.classList.remove('is-selected');
                    b.setAttribute('aria-pressed', 'false');
                });
                btn.classList.add('is-selected');
                btn.setAttribute('aria-pressed', 'true');
                refresh();
            });
        });
    } catch (e) {
        console.error('[Review] relationship samples', e);
        mount.innerHTML = `<p class="error-text">${escapeHtml(e.message || String(e))}</p>`;
    }
}

function reviewQueueTypeLabel(item) {
    if (item.type === 'destructive_approval') return 'Safety pause';
    if (item.type === 'structural_review') return 'Sheet layout';
    if (item.type === 'checksum_failure') return 'Checksum';
    if (item.type === 'schema_mismatch') return 'Constraints';
    if (item.type === 'file_relationship_review') return 'File links';
    const td = item.trigger_data || {};
    const hasPlanArtifacts = Boolean(td.approval_items?.length || td.planned_rule_actions?.length);
    if (item.type === 'plan_review' || hasPlanArtifacts) return 'Planner review';
    if (item.type === 'verification_stall') return 'Stall';
    if (item.checkpoint_id) return 'Checkpoint';
    return 'Confidence';
}

function pickLatestPendingCheckpointId(items, jobId) {
    if (!jobId || !Array.isArray(items)) return null;
    const candidates = items.filter((it) => it.job_id === jobId && it.checkpoint_id);
    if (!candidates.length) return null;
    candidates.sort((a, b) => {
        const ta = new Date(a.created_at || 0).getTime();
        const tb = new Date(b.created_at || 0).getTime();
        return tb - ta;
    });
    return candidates[0].checkpoint_id;
}

function pickLatestPlanReviewCheckpointId(items, jobId) {
    if (!jobId || !Array.isArray(items)) return null;
    const candidates = items.filter((it) => {
        if (it.job_id !== jobId || !it.checkpoint_id) return false;
        if (it.type === 'plan_review') return true;
        const td = it.trigger_data || {};
        return Boolean((td.approval_items || []).length || (td.planned_rule_actions || []).length);
    });
    if (!candidates.length) return null;
    candidates.sort((a, b) => {
        const ta = new Date(a.created_at || 0).getTime();
        const tb = new Date(b.created_at || 0).getTime();
        return tb - ta;
    });
    return candidates[0].checkpoint_id;
}

// ===== Init =====
document.addEventListener('DOMContentLoaded', () => {
    fetchPendingReviews();
});

// ===== API Calls =====
async function fetchPendingReviews(options = {}) {
    try {
        const data = await fetchJson('/api/review/pending');
        await renderReviewList(data.items || [], options);
    } catch (e) {
        console.error('[Review] Error fetching pending reviews:', e);
        showToast('Failed to fetch review queue', 'error');
    }
}

async function fetchReviewDetails(jobId) {
    try {
        return await fetchJson(`/api/review/${jobId}`);
    } catch (e) {
        console.error('[Review] Error fetching review details:', e);
        throw e;
    }
}

async function fetchCheckpointDetails(checkpointId) {
    try {
        return await fetchJson(`/api/review/checkpoint/${checkpointId}`);
    } catch (e) {
        console.error('[Review] Error fetching checkpoint details:', e);
        throw e;
    }
}

async function approveReview(jobId) {
    try {
        const result = await fetchJson(`/api/review/${jobId}/approve`, { method: 'POST' });
        if (result.success) {
            reviewState.approved++;
            showToast('Item approved successfully', 'success');
            fetchPendingReviews();
        }
    } catch (e) {
        console.error('[Review] Error approving:', e);
        showToast('Failed to approve item', 'error');
    }
}

async function rejectReview(jobId) {
    try {
        const result = await fetchJson(`/api/review/${jobId}/reject`, { method: 'POST' });
        if (result.success) {
            reviewState.rejected++;
            showToast('Item rejected', 'success');
            fetchPendingReviews();
        }
    } catch (e) {
        console.error('[Review] Error rejecting:', e);
        showToast('Failed to reject item', 'error');
    }
}


async function renderReviewList(items, options = {}) {
    const listEl = document.getElementById('reviewQueueList');
    if (!listEl) return;
    reviewState.items = items;

    if (items.length === 0) {
        listEl.innerHTML = `
            <div class="empty-state compact" style="padding: 16px;">
                <div class="empty-icon">✅</div>
                <h3>All clear</h3>
                <p>No pending reviews.</p>
            </div>
        `;
        const details = document.getElementById('reviewDetails');
        if (details) {
            details.innerHTML = `
                <div class="empty-state compact">
                    <div class="empty-icon">✅</div>
                    <h3>Nothing to show</h3>
                    <p>When new items arrive, pick one from the queue.</p>
                </div>
            `;
        }
        reviewState.selectedCheckpointId = null;
        reviewState.selectedJobId = null;
        return;
    }

    listEl.innerHTML = items.map((item) => {
        const confNum = reviewItemConfidence(item);
        const confValue = formatConfidencePct(confNum);
        const confClass = confNum > 0.9 ? 'confidence-high'
            : (confNum > 0.6 ? 'confidence-medium' : 'confidence-low');
        const hasPlanArtifacts = Boolean(item.trigger_data?.approval_items?.length || item.trigger_data?.planned_rule_actions?.length);
        const isPlanReview = item.type === 'plan_review' || hasPlanArtifacts;
        const isCheckpoint = item.type === 'structural_review' || item.type === 'checksum_failure'
            || item.type === 'schema_mismatch'
            || item.type === 'file_relationship_review' || item.checkpoint_id;
        const rowId = item.checkpoint_id || item.job_id;
        const rowClick = isCheckpoint
            ? `event.stopPropagation(); showCheckpointDetail('${item.checkpoint_id}')`
            : `event.stopPropagation(); showReviewDetail('${item.job_id}')`;
        const typeLabel = item.type_label ? escapeHtml(item.type_label) : reviewQueueTypeLabel(item);
        const snippetSource = item.reason || (isPlanReview ? 'Planner paused this run until you continue or replan.' : 'Needs a decision.');
        const time = new Date(item.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

        return `
            <button type="button" class="review-item review-queue-card" id="review-item-${rowId}" onclick="${rowClick}">
                <div class="review-queue-card-top">
                    <span class="review-queue-type">${typeLabel}</span>
                    <span class="confidence-tag ${confClass}">${confValue}</span>
                </div>
                <div class="review-queue-title">${escapeHtml(item.filename || 'Untitled')}</div>
                <div class="review-queue-snippet">${escapeHtml(truncateReviewSnippet(snippetSource))}</div>
                <div class="review-queue-meta">Job ${escapeHtml(item.job_id.substring(0, 8))} · ${time}</div>
            </button>
        `;
    }).join('');

    if (options.skipAutoSelect) {
        if (reviewState.selectedCheckpointId) {
            setActiveReviewRow(reviewState.selectedCheckpointId);
        } else if (reviewState.selectedJobId) {
            setActiveReviewRow(reviewState.selectedJobId);
        }
        return;
    }

    const stillSelectedCheckpoint = items.find(
        (item) => item.checkpoint_id && item.checkpoint_id === reviewState.selectedCheckpointId
    );
    const stillSelectedJob = items.find(
        (item) => !item.checkpoint_id && item.job_id === reviewState.selectedJobId
    );
    if (stillSelectedCheckpoint) {
        await showCheckpointDetail(stillSelectedCheckpoint.checkpoint_id);
        return;
    }
    if (stillSelectedJob) {
        await showReviewDetail(stillSelectedJob.job_id);
        return;
    }

    const nextSelection = items[0];
    if (nextSelection.checkpoint_id) {
        await showCheckpointDetail(nextSelection.checkpoint_id);
    } else {
        await showReviewDetail(nextSelection.job_id);
    }
}

async function showReviewDetail(jobId) {
    reviewState.selectedJobId = jobId;
    reviewState.selectedCheckpointId = null;
    setActiveReviewRow(jobId);

    const detailsEl = document.getElementById('reviewDetails');
    detailsEl.innerHTML = '<div class="loading">Loading details...</div>';

    try {
        const data = await fetchReviewDetails(jobId);
        if (data.type === 'destructive_approval') {
            renderDestructiveApprovalContent(data);
        } else {
            renderReviewDetailContent(data);
        }
    } catch (e) {
        detailsEl.innerHTML = '<div class="error">Failed to load details</div>';
    }
}

async function showCheckpointDetail(checkpointId, options = {}) {
    reviewState.selectedCheckpointId = checkpointId;
    reviewState.selectedJobId = null;
    setActiveReviewRow(checkpointId);

    const detailsEl = document.getElementById('reviewDetails');
    detailsEl.innerHTML = '<div class="loading">Loading review details...</div>';

    try {
        const data = await fetchCheckpointDetails(checkpointId);
        if (data.type === 'file_relationship_review') {
            renderRelationshipReviewContent(data);
        } else if (data.type === 'checksum_failure') {
            renderIntegrityReviewContent(data);
        } else if (data.type === 'schema_mismatch') {
            renderSchemaConstraintReviewContent(data);
        } else if (data.type === 'plan_review' || data.trigger_data?.approval_items?.length || data.trigger_data?.planned_rule_actions?.length) {
            renderPlanReviewContent(data);
        } else {
            renderCheckpointDetailContent(data);
        }
        if (options.scrollIntoView) {
            detailsEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    } catch (e) {
        const msg = e?.message || String(e);
        if (msg.includes('404')) {
            reviewState.selectedCheckpointId = null;
            reviewState.selectedJobId = null;
            await fetchPendingReviews();
            detailsEl.innerHTML = `
                <div class="empty-state compact">
                    <div class="empty-icon">ℹ️</div>
                    <h3>Review item no longer available</h3>
                    <p class="planner-review-help">It may have been cancelled, completed, or cleared when the job was removed. The queue was refreshed.</p>
                </div>`;
            showToast('That review item is no longer on the server (refreshed queue).', 'warning');
        } else {
            detailsEl.innerHTML = '<div class="error">Failed to load checkpoint details</div>';
        }
    }
}

function openPlanReviewWorkspace(checkpointId) {
    showCheckpointDetail(checkpointId, { scrollIntoView: true });
}

function setActiveReviewRow(id) {
    document.querySelectorAll('.review-item').forEach(el => el.classList.remove('active'));
    const row = document.getElementById(`review-item-${id}`);
    if (row) row.classList.add('active');
}

function renderDestructiveApprovalContent(data) {
    const previews = data.previews || [];
    const detailsEl = document.getElementById('reviewDetails');
    const withRemovals = previews.filter((p) => {
        const rowCount = Number(p.deleted_count ?? p.total_rows_to_delete ?? 0);
        const columnCount = Number(p.total_columns_to_delete ?? 0);
        return rowCount > 0 || columnCount > 0;
    });

    const statsHtml = withRemovals.length
        ? `<ul class="combine-dedupe-callout__facts combine-dedupe-callout__facts-row">${withRemovals.map((p) => {
            const label = escapeHtml(p.step_title || p.tool_name || 'Step');
            const before = Number(p.rows_before || 0);
            const after = Number(p.rows_after || 0);
            const removed = Number(p.deleted_count ?? p.total_rows_to_delete ?? 0);
            const cols = Number(p.total_columns_to_delete ?? 0);
            const colPart = cols > 0 ? `, ${cols} column${cols === 1 ? '' : 's'}` : '';
            const rowPart = before && after
                ? ` (${before} → ${after} rows)`
                : '';
            return `<li><code>${label}</code> — would remove <strong>${removed}</strong> row${removed === 1 ? '' : 's'}${colPart}${rowPart}.</li>`;
        }).join('')}</ul>`
        : '';

    const visualBlocks = previews
        .map((p) => _renderDuplicateVisualSample(_buildRemovalVisualSampleFromPreview(p)))
        .filter(Boolean)
        .join('');

    const emptyNote = !previews.length
        ? `
            <div class="empty-state compact">
                <h3>No destructive changes detected</h3>
                <p>This pause was raised without any rows or columns marked for removal.</p>
            </div>`
        : !withRemovals.length
            ? `<p class="combine-dedupe-callout__note">Destructive steps were planned, but the preview found <strong>no rows or columns</strong> to remove on this sheet sample.</p>`
            : '';

    detailsEl.innerHTML = `
        <div class="review-detail-header">
            <div>
                <h2>${escapeHtml(data.filename)}</h2>
                <p class="review-detail-subtitle">Safety pause — data removal</p>
            </div>
            <div class="review-detail-meta">
                <span class="review-status-chip warning">Approval required</span>
                <span class="review-detail-job-id">Job ${escapeHtml(data.job_id)}</span>
            </div>
        </div>

        <section class="planner-review-card combine-dedupe-callout" aria-label="What would be removed">
            <div class="combine-dedupe-callout__title">What needs your review</div>
            <p class="combine-dedupe-callout__lead">${escapeHtml(data.reason || 'The next steps can delete rows or columns.')}</p>
            <ul class="combine-dedupe-callout__help">
                <li><strong>If you continue:</strong> the rows and columns shown below will be removed.</li>
                <li><strong>If you stop:</strong> processing halts and your data stays unchanged.</li>
            </ul>
            <div class="combine-dedupe-callout__status" role="status">
                <strong>Removal preview</strong> — approve to apply; stop to keep current data unchanged.
            </div>
            ${statsHtml}
            ${emptyNote}
            ${visualBlocks}
        </section>

        <div class="review-actions">
            <button class="btn-primary destructive-approve-btn" onclick="handleApproveDeletions('${data.job_id}')">
                Continue with deletions
            </button>
            <button class="btn-secondary btn-reject" onclick="handleRejectDeletions('${data.job_id}')">
                Stop job
            </button>
        </div>
    `;
}

/** Map planner / template keys into the fields the rule-action editor expects. */
function normalizePlannerRuleAction(item) {
    if (!item || typeof item !== 'object') return null;
    const target_column = item.target_column || item.column || item.destination_column || '';
    const action = item.action || item.rule_type || item.operator || item.type || '';
    const val = item.value ?? item.rule_expression ?? item.expression ?? '';
    const description = item.description || item.summary || item.reason || item.title || '';
    if (!target_column && !action && (val === '' || val == null) && !description) return null;
    return {
        ...item,
        target_column: String(target_column),
        action: String(action),
        value: val === '' || val == null ? '' : String(val),
        description: String(description),
    };
}

function buildGuidedSetupSnapshotHtml(data) {
    const ps = data.planning_summary || {};
    const src = ps.source_summary || {};
    const lay = ps.layout_summary || {};
    const ms = ps.mapping_summary || {};
    const rules = ps.rules_summary || [];
    const gran = ps.date_granularity_alignment || {};
    const sheet = src.sheet_name || '—';
    const hr = lay.header_row != null && lay.header_row !== '' ? String(lay.header_row) : '—';
    const mappedList = ms.mapped_columns || [];
    const mappedCount = typeof ms.mapped_count === 'number' ? ms.mapped_count : mappedList.length;
    const excluded = ms.excluded_columns || [];
    const unresolved = ms.unresolved_target_columns || [];
    const jobId = data.job_id || '';
    const sheetQ = src.sheet_name ? `&sheet_name=${encodeURIComponent(src.sheet_name)}` : '';
    const srcId = (data.lineage && data.lineage.source_id) ? String(data.lineage.source_id) : '';
    const srcQ = srcId ? `&source_id=${encodeURIComponent(srcId)}` : '';
    const setupHref = `/pages/setup.html?job_id=${encodeURIComponent(jobId)}${sheetQ}${srcQ}`;
    const srcGrain = gran.source_date_granularity || src.date_granularity || '';
    const tgtGrain = gran.target_date_granularity || '';
    const granLi = (srcGrain || tgtGrain || gran.planner_obligation)
        ? `<li><strong>Date granularity</strong>: source <code>${escapeHtml(srcGrain || 'not set')}</code> → target <code>${escapeHtml(tgtGrain || 'not set')}</code>${gran.requires_weekly_rollup_step ? ' <span class="planner-guided-setup-warn">(rollup expected)</span>' : ''}</li>`
        : `<li><strong>Date granularity</strong>: <span class="muted">not summarized — set source grain in Guided Setup and template in target schema.</span></li>`;

    return `
        <section class="planner-review-card planner-guided-setup-card">
            <h4>Inputs from Guided Setup</h4>
            <p class="planner-review-help planner-review-help--tight">Saved layout and mapping for this job. Fix in Guided Setup if anything looks wrong.</p>
            <ul class="planner-guided-setup-list">
                <li><strong>Sheet</strong>: ${escapeHtml(sheet)}</li>
                <li><strong>Header row (1-based)</strong>: ${escapeHtml(hr)}</li>
                ${granLi}
                <li><strong>Column mapping</strong>: ${mappedCount} mapped pair${mappedCount === 1 ? '' : 's'}${excluded.length ? `; ${excluded.length} source column${excluded.length === 1 ? '' : 's'} excluded` : ''}</li>
                ${rules.length ? `<li><strong>Business rules on job</strong>: ${rules.length}</li>` : ''}
                ${unresolved.length ? `<li class="planner-guided-setup-warn"><strong>Unresolved template fields</strong>: ${unresolved.map(escapeHtml).join(', ')}</li>` : ''}
            </ul>
            ${gran.planner_obligation ? `<p class="planner-review-help planner-guided-setup-warn" style="margin-top:8px;"><strong>Passed to planner:</strong> ${escapeHtml(gran.planner_obligation)}</p>` : ''}
            ${mappedList.length
                ? `<div class="planner-guided-mapping-preview"><span class="planner-summary-label">Sample mappings</span><code>${escapeHtml(mappedList.slice(0, 8).join(' · '))}${mappedList.length > 8 ? ' …' : ''}</code></div>`
                : '<p class="planner-review-help muted">No mapping pairs were summarized — open Guided Setup to confirm mappings.</p>'}
            <div class="planner-inline-actions"><a class="btn-secondary" href="${setupHref}">Edit in Guided Setup</a></div>
        </section>`;
}

function approvalItemChoiceOptions(item) {
    const raw = item.options || item.choices;
    if (!Array.isArray(raw) || raw.length < 2) return [];
    return raw
        .map((o, i) => {
            if (!o || typeof o !== 'object') return null;
            const id = String(o.id ?? o.value ?? `option_${i}`).trim();
            const label = String(o.label ?? o.text ?? o.title ?? id).trim();
            if (!id || !label) return null;
            return { id, label };
        })
        .filter(Boolean);
}

function buildPlannerApprovalAnswerHtml(item, index) {
    const choices = approvalItemChoiceOptions(item);
    if (choices.length >= 2) {
        const opts = choices
            .map((o) => {
                const enc = encodeURIComponent(o.id);
                return `<label class="planner-approval-choice-line"><input type="radio" name="planner-approval-choice-${index}" value="${enc}"> ${escapeHtml(o.label)}</label>`;
            })
            .join('');
        const otherEnc = encodeURIComponent('other_notes');
        const otherLine = `<label class="planner-approval-choice-line"><input type="radio" name="planner-approval-choice-${index}" value="${otherEnc}"> None of the above — use my notes below instead</label>`;
        return `
                            <div class="planner-approval-choices planner-approval-yn--stack" role="radiogroup" aria-label="Your choice for item ${index + 1}">
                                ${opts}
                                ${otherLine}
                            </div>`;
    }
    return `
                            <div class="planner-approval-yn planner-approval-yn--stack" role="radiogroup" aria-label="Your answer for item ${index + 1}">
                                <label class="planner-approval-yn-line"><input type="radio" name="planner-approval-yn-${index}" value="yes"> Yes — statement is true</label>
                                <label class="planner-approval-yn-line"><input type="radio" name="planner-approval-yn-${index}" value="no"> No — false or unsure</label>
                            </div>`;
}

function formatApprovalPreviewHtml(item) {
    const vals = item.preview_values;
    if (vals && vals.length) {
        const sub = item.preview_column_label ? escapeHtml(item.preview_column_label) : '';
        const label = sub
            ? `<div class="planner-approval-preview-label">Sample values <span class="planner-approval-preview-meta">(${sub})</span></div>`
            : '<div class="planner-approval-preview-label">Sample values</div>';
        const chips = vals.map((v) => {
            const s = v === null || v === undefined ? '∅' : String(v);
            const short = s.length > 120 ? `${s.slice(0, 117)}…` : s;
            return `<span class="planner-approval-value-chip">${escapeHtml(short)}</span>`;
        }).join('');
        return `<div class="planner-approval-preview">${label}<div class="planner-approval-value-row">${chips}</div></div>`;
    }
    if (item.preview_note) {
        return `<p class="planner-approval-preview-note">${escapeHtml(item.preview_note)}</p>`;
    }
    return '';
}

function buildPlannerWhatToDoHintHtml(approvalCount, stepCount) {
    const ap = approvalCount > 0
        ? `Complete <strong>${approvalCount}</strong> decision${approvalCount === 1 ? '' : 's'}: pick the option that matches your data (or Yes/No where that pattern is shown), or choose <strong>None of the above — use my notes</strong> / write at least 15 characters in notes if no listed choice fits.`
        : 'No separate checklist — review steps, then continue or replan.';
    const st = stepCount > 0
        ? `This plan lists <strong>${stepCount}</strong> step${stepCount === 1 ? '' : 's'} in run order below.`
        : '';
    return `
        <details class="planner-review-card planner-what-to-do planner-what-to-do--collapsible">
            <summary class="planner-what-to-do-summary">How this screen works</summary>
            <ol class="planner-what-to-do-list">
                <li>Read <strong>What will run</strong> (run order may differ from numeric “step” fields in the JSON).</li>
                <li>${ap}</li>
                <li><strong>Continue with plan</strong> approves and resumes; <strong>Replan with notes</strong> sends your edits back to the planner. ${st}</li>
            </ol>
        </details>`;
}

function buildPlannerPlanReadoutHtml(toolCalls) {
    if (!toolCalls.length) {
        return `
        <section class="planner-review-card planner-plan-readout">
            <h4>What will run</h4>
            <p class="planner-review-help planner-review-help--tight">No executable steps were attached to this plan snapshot.</p>
        </section>`;
    }
    const items = toolCalls.map((tool, index) => {
        const runOrder = index + 1;
        const plannerStep = Number(tool.step);
        const marked = Number.isFinite(plannerStep) ? plannerStep : runOrder;
        const stepMismatch = marked !== runOrder;
        const title = planReadoutStepTitle(tool);
        const toolId = escapeHtml(String(tool.tool || tool.name || '').trim());
        const scope = formatPlanParamsForHumans(tool.params || {});
        const desc = (tool.description || tool.reason || '').trim();
        const descShort = desc.length > 200 ? `${desc.slice(0, 197)}…` : desc;
        const stepNote = stepMismatch
            ? `<span class="planner-plan-readout-stepnote" title="Order in this list is execution order; planner JSON used a different step number.">Run #${runOrder} · planner field step=${marked}</span>`
            : `<span class="planner-plan-readout-stepnote">Run #${runOrder}</span>`;
        return `
            <li class="planner-plan-readout-item planner-plan-readout-item--compact">
                <div class="planner-plan-readout-top">
                    ${stepNote}
                    ${toolId ? `<code class="planner-plan-readout-toolid">${toolId}</code>` : ''}
                </div>
                <strong class="planner-plan-readout-title">${escapeHtml(title)}</strong>
                ${scope ? `<div class="planner-plan-readout-scope">${escapeHtml(scope)}</div>` : ''}
                ${descShort ? `<p class="planner-plan-readout-desc">${escapeHtml(descShort)}</p>` : ''}
            </li>`;
    }).join('');
    return `
        <section class="planner-review-card planner-plan-readout">
            <h4>What will run</h4>
            <p class="planner-review-help planner-review-help--tight">${toolCalls.length} step${toolCalls.length === 1 ? '' : 's'} in execution order.</p>
            <ol class="planner-plan-readout-list">${items}</ol>
        </section>`;
}

function buildPlannerNeedsYourOkHtml(approvalItems) {
    if (!approvalItems.length) return '';
    return `
        <section class="planner-review-card planner-decisions-card planner-decisions-card--dense">
            <h4 class="planner-decisions-heading">Decisions needed (${approvalItems.length})</h4>
            <p class="planner-review-help planner-review-help--dense">Pick the option that matches your file, or add notes (15+ characters) if none fit.</p>
            <div class="planner-review-list planner-review-list--dense">
                ${approvalItems.map((item, index) => {
        const q = item.question || item.summary || item.description || item.reason || item.title || 'Planner question';
        const body = escapeHtml(q);
        const answerHtml = buildPlannerApprovalAnswerHtml(item, index);
        return `
                    <fieldset class="planner-approval-fieldset" data-approval-card data-approval-index="${index}">
                        <legend class="planner-approval-legend">
                            <span class="planner-item-index">Item ${index + 1}</span>
                            ${item.target_column || item.target ? `<span class="planner-item-target">${escapeHtml(item.target_column || item.target || '')}</span>` : ''}
                        </legend>
                        <div class="planner-approval-cursor">
                            <p class="planner-approval-cursor-label">Q</p>
                            <p class="planner-approval-question">${body}</p>
                            <div class="planner-approval-cursor-preview">${formatApprovalPreviewHtml(item)}</div>
                            <p class="planner-approval-cursor-label">A</p>
                            ${answerHtml}
                            <label class="planner-approval-extra-label">Optional notes</label>
                            <textarea class="planner-note-input planner-approval-extra planner-approval-cursor-input" data-approval-index="${index}" rows="2" placeholder="Optional — corrections or context."></textarea>
                        </div>
                    </fieldset>`;
    }).join('')}
            </div>
        </section>`;
}

function isExclusionTool(toolName) {
    const normalized = String(toolName || '').toLowerCase();
    return ['drop', 'filter', 'exclude', 'remove', 'discard'].some(token => normalized.includes(token));
}

function renderPlanImpactPreviewPayload(payload) {
    const mount = document.getElementById('planImpactPreviewMount');
    if (!mount) return;

    const impacts = Array.isArray(payload?.impacts) ? payload.impacts : [];
    const destructiveTools = Array.isArray(payload?.destructive_tools) ? payload.destructive_tools : [];
    const basis = String(payload?.preview_basis || '').trim();
    const available = Boolean(payload?.preview_available);

    if (!destructiveTools.length) {
        mount.innerHTML = '';
        return;
    }

    const withRemovals = impacts.filter((p) => p.has_removals);
    const noRemoval = impacts.filter((p) => !p.has_removals);

    let statsHtml = '';
    if (withRemovals.length) {
        const statItems = withRemovals.map((p) => {
            const label = escapeHtml(p.step_title || p.tool_name || 'Step');
            const before = Number(p.rows_before || 0);
            const after = Number(p.rows_after || 0);
            const removed = Math.max(0, before - after);
            const cols = Number(p.total_columns_to_delete || 0);
            const colPart = cols > 0 ? `, ${cols} column${cols === 1 ? '' : 's'}` : '';
            return `<li><code>${label}</code> — would remove <strong>${removed}</strong> row${removed === 1 ? '' : 's'}${colPart} (${before} → ${after} rows).</li>`;
        }).join('');
        statsHtml = `<ul class="combine-dedupe-callout__facts combine-dedupe-callout__facts-row">${statItems}</ul>`;
    }

    const visualBlocks = withRemovals
        .map((p) => _renderDuplicateVisualSample(p.visual_sample))
        .filter(Boolean)
        .join('');

    let emptyNote = '';
    if (noRemoval.length && !withRemovals.length) {
        emptyNote = `
            <p class="combine-dedupe-callout__note">
                Destructive steps are in the plan, but the preview found <strong>no rows or columns</strong> to remove on this sheet sample.
                You can continue safely, or replan if that seems wrong.
            </p>`;
    } else if (noRemoval.length) {
        emptyNote = `
            <p class="combine-dedupe-callout__note muted">
                Other destructive steps in the plan did not match any rows/columns in the preview.
            </p>`;
    }

    const unavailable = !available
        ? `<p class="combine-dedupe-callout__note">Preview data unavailable: ${escapeHtml(basis || 'could not load sheet sample')}.</p>`
        : basis
            ? `<p class="combine-dedupe-callout__verify muted">Preview basis: ${escapeHtml(basis)}</p>`
            : '';

    mount.innerHTML = `
        <div class="combine-dedupe-callout__status" role="status">
            <strong>Removal preview</strong> — approve to apply; replan or cancel to keep current data unchanged.
        </div>
        ${unavailable}
        ${statsHtml}
        ${emptyNote}
        ${visualBlocks}
    `;
}

async function hydratePlanImpactPreview(checkpointId) {
    const mount = document.getElementById('planImpactPreviewMount');
    if (!mount || !checkpointId) return;
    try {
        const payload = await fetchJson(`/api/review/checkpoint/${encodeURIComponent(checkpointId)}/plan-impact-preview`);
        renderPlanImpactPreviewPayload(payload);
    } catch (e) {
        mount.innerHTML = `<p class="combine-dedupe-callout__note">Could not load removal preview: ${escapeHtml(e.message || String(e))}</p>`;
    }
}

function buildPlannerReviewFocusHtml(pauseReason, toolCalls, approvalItems, checkpointId) {
    const destructiveSteps = (toolCalls || []).filter((t) => isExclusionTool(t.tool || t.name));
    const decisionCount = Array.isArray(approvalItems) ? approvalItems.length : 0;
    const bullets = [];

    if (destructiveSteps.length) {
        const labels = destructiveSteps.map((t) => planReadoutStepTitle(t)).join(', ');
        bullets.push(
            `<li><strong>Data removal:</strong> ${destructiveSteps.length} step${destructiveSteps.length === 1 ? '' : 's'} may delete rows or columns (${escapeHtml(labels)}). Review the impact preview below before continuing.</li>`,
        );
    }
    if (decisionCount > 0) {
        bullets.push(
            `<li><strong>Planner decisions:</strong> ${decisionCount} question${decisionCount === 1 ? '' : 's'} need your answer in <em>Decisions needed</em>.</li>`,
        );
    }
    if (!bullets.length) {
        bullets.push(
            `<li><strong>Plan sign-off:</strong> Confirm the steps below match your intent before execution starts.</li>`,
        );
    }
    bullets.push(
        `<li><strong>If you reject:</strong> use <em>Replan with notes</em> or <em>Cancel job</em> — nothing is deleted until you continue.</li>`,
    );

    const cpAttr = checkpointId ? ` data-checkpoint-id="${escapeAttr(String(checkpointId))}"` : '';

    return `
        <section class="planner-review-card planner-review-focus combine-dedupe-callout" aria-label="What needs your review">
            <div class="combine-dedupe-callout__title">What needs your review</div>
            <p class="combine-dedupe-callout__lead">${escapeHtml(pauseReason)}</p>
            <ul class="combine-dedupe-callout__help">${bullets.join('')}</ul>
            <div id="planImpactPreviewMount" class="plan-impact-preview-mount"${cpAttr}>
                <p class="muted plan-impact-preview-loading">Loading removal preview…</p>
            </div>
        </section>`;
}

function renderPlanReviewContent(data) {
    const plan = data.plan || {};
    const approvalItems = plan.approval_items || data.trigger_data?.approval_items || [];
    const rawRuleSource = plan.business_rule_actions || data.trigger_data?.planned_rule_actions || [];
    const plannedRuleActions = rawRuleSource.map(normalizePlannerRuleAction).filter(Boolean);
    const toolCalls = plan.tool_calls || [];
    const expectedColumns = plan.expected_columns || [];
    const mappingSummary = data.planning_summary?.mapping_summary || {};
    const excludedColumns = mappingSummary.excluded_columns || [];
    const reasoning = (plan.reasoning || data.trigger_data?.reasoning || '').trim();
    const reasoningText = reasoning || buildFallbackPlanReasoning(data, toolCalls, expectedColumns, excludedColumns);
    const detailsEl = document.getElementById('reviewDetails');
    const pauseReason = (data.reason || data.description || 'Planner review required before execution continues.').trim();

    const needsOkHtml = buildPlannerNeedsYourOkHtml(approvalItems);
    const planReadoutHtml = buildPlannerPlanReadoutHtml(toolCalls);
    const focusHtml = buildPlannerReviewFocusHtml(pauseReason, toolCalls, approvalItems, data.checkpoint_id);

    const planConf = reviewItemConfidence(data);
    detailsEl.innerHTML = `
        <div class="planner-workspace-shell">
        <div class="planner-sticky-actions review-actions">
            <button type="button" class="btn-primary btn-approve plan-action-btn" data-plan-action="approve" title="Approve this plan and resume processing." onclick="submitPlanReview('${data.checkpoint_id}', 'approve')">
                Continue with plan
            </button>
            <button type="button" class="btn-secondary plan-action-btn" data-plan-action="modify" title="Keep your notes below and ask the planner to try again." onclick="submitPlanReview('${data.checkpoint_id}', 'modify')">
                Replan with notes
            </button>
            <button type="button" class="btn-danger plan-action-btn" data-plan-action="cancel" title="Stop this job." onclick="submitPlanReview('${data.checkpoint_id}', 'cancel')" style="margin-left: auto;">
                Cancel job
            </button>
        </div>

        <div class="planner-inline-titlebar">
            <h2 class="planner-inline-title">${escapeHtml(data.filename)}</h2>
            <div class="planner-inline-meta">
                <span class="confidence-tag confidence-medium">${formatConfidencePct(planConf)} confidence</span>
                <span class="review-detail-job-id">Job ${escapeHtml(data.job_id || '')}</span>
                <span class="planner-inline-badge">Planner review</span>
            </div>
            <p class="planner-pause-line"><span class="planner-pause-line-label">Paused:</span> ${escapeHtml(pauseReason)}</p>
        </div>

        ${focusHtml}
        ${planReadoutHtml}
        ${needsOkHtml}

        <div class="planner-body-stack">
            <details class="planner-review-card" open>
                <summary>Guidance, expected columns &amp; reasoning</summary>
                <div class="planner-review-layout">
                    <section class="planner-review-card" style="margin:0; border:none; padding:0; background:transparent;">
                        <h4 style="margin:0 0 6px; font-size:13px;">Expected output columns</h4>
                        <p class="planner-review-help planner-review-help--tight">One per line or comma-separated.</p>
                        <textarea id="plannerExpectedColumns" class="planner-guidance-input" placeholder="campaign_name&#10;spend&#10;impressions">${escapeHtml(expectedColumns.join('\n'))}</textarea>
                    </section>
                    <section class="planner-review-card" style="margin:0; border:none; padding:0; background:transparent;">
                        <h4 style="margin:0 0 6px; font-size:13px;">Business guidance</h4>
                        <p class="planner-review-help planner-review-help--tight">Intent, exclusions, or corrections for replanning.</p>
                        <textarea id="plannerAnalystNotes" class="planner-guidance-input" placeholder="e.g. Drop diagnostic columns; keep spend and impressions."></textarea>
                    </section>
                </div>
                <section class="planner-review-card" style="margin:12px 0 0; border:none; padding:0; background:transparent;">
                    <h4 style="margin:0 0 6px; font-size:13px;">Planner reasoning (editable)</h4>
                    <textarea id="plannerReasoning" class="planner-guidance-input" placeholder="Planner reasoning">${escapeHtml(reasoningText)}</textarea>
                </section>
            </details>

            <details class="planner-review-card">
                <summary>Business rules (${plannedRuleActions.length})</summary>
                <p class="planner-review-help planner-review-help--tight">Optional rows persisted for the next plan.</p>
                <div class="planner-rule-list" id="plannerRuleList">
                    ${plannedRuleActions.map((item, index) => `
                        ${renderPlanRuleActionEditor(item, index)}
                    `).join('')}
                </div>
                <div class="planner-inline-actions">
                    <button class="btn-secondary" type="button" onclick="addPlanRuleActionRow()">Add rule row</button>
                </div>
            </details>
        </div>
        </div>
    `;

    if (data.checkpoint_id && (toolCalls || []).some((t) => isExclusionTool(t.tool || t.name))) {
        void hydratePlanImpactPreview(data.checkpoint_id);
    } else {
        const mount = document.getElementById('planImpactPreviewMount');
        if (mount) mount.innerHTML = '';
    }
}

function renderIntegrityReviewContent(data) {
    const detailsEl = document.getElementById('reviewDetails');
    const triggerData = data.trigger_data || {};
    const violation = triggerData.integrity_violation || {};
    const subtype = violation.subtype || triggerData.subtype || 'integrity';
    const toolName = violation.tool || triggerData.tool || 'unknown tool';
    const cols = Array.isArray(violation.columns_affected) ? violation.columns_affected.join(', ') : '';
    const previewRows = Array.isArray(data.data_preview) ? data.data_preview : [];
    const previewHtml = renderCheckpointDataPreview(previewRows);

    detailsEl.innerHTML = `
        <div class="review-detail-header">
            <div>
                <h2>${escapeHtml(data.filename || 'Data integrity review')}</h2>
                <p class="review-detail-subtitle">${escapeHtml(data.title || 'Processing paused for data integrity')}</p>
            </div>
            <div class="review-detail-meta">
                <span class="review-status-chip warning">${escapeHtml(String(data.severity || 'high'))}</span>
                <span class="review-detail-job-id">Job ${escapeHtml(data.job_id || '')}</span>
            </div>
        </div>
        <div class="reason-box reason-box-warning">
            <p><strong>What happened:</strong> ${escapeHtml(data.description || data.reason || 'A tool step changed the data in a way that failed integrity checks.')}</p>
            <p class="reason-box-followup" style="margin-top: 8px;">
                Tool: <strong>${escapeHtml(toolName)}</strong>
                ${subtype ? ` · Issue: <strong>${escapeHtml(String(subtype).replace(/_/g, ' '))}</strong>` : ''}
                ${cols ? ` · Columns: ${escapeHtml(cols)}` : ''}
            </p>
        </div>
        <section class="planner-review-card">
            <h4 style="margin: 0 0 8px;">Current data preview</h4>
            <div class="mini-data-preview">${previewHtml}</div>
        </section>
        <p class="planner-review-help" style="margin-top: 12px;">
            <strong>Approve</strong> to continue with the remaining plan steps (integrity checks are suppressed for this resume).
            <strong>Cancel</strong> stops the job. Open <a href="debug.html">Results</a> for full execution trace.
        </p>
        <div class="review-actions">
            ${renderCheckpointActions(data.checkpoint_id, data.available_actions || ['approve', 'investigate', 'cancel'])}
        </div>
    `;
}

function _constraintDecisionFromPayload(decisions, column, fallback = 'keep_as_is') {
    if (!decisions || typeof decisions !== 'object') return fallback;
    const key = String(column || '');
    if (decisions[key]) return String(decisions[key]);
    const lower = key.toLowerCase();
    for (const [k, v] of Object.entries(decisions)) {
        if (String(k).toLowerCase() === lower) return String(v);
    }
    return fallback;
}

function _constraintChoiceBtn(value, label, selected) {
    return `<button type="button" class="dup-choice-btn${selected ? ' is-selected' : ''}"
        data-constraint-action="${escapeAttr(value)}" aria-pressed="${selected ? 'true' : 'false'}">${escapeHtml(label)}</button>`;
}

function renderSchemaConstraintReviewContent(data) {
    const detailsEl = document.getElementById('reviewDetails');
    const triggerData = data.trigger_data || {};
    const violations = Array.isArray(triggerData.constraint_violations)
        ? triggerData.constraint_violations
        : [];
    const decisions = (data.resolution_data && data.resolution_data.constraint_decisions)
        || triggerData.constraint_decisions
        || {};
    const previewRows = Array.isArray(data.data_preview) ? data.data_preview : [];
    const previewHtml = renderCheckpointDataPreview(previewRows);

    const cards = violations.map((v, idx) => {
        const col = String(v.column || `column_${idx}`);
        const issue = String(v.issue || 'CONSTRAINT').replace(/_/g, ' ');
        const bound = v.bound ?? v.minimum ?? v.maximum ?? '?';
        const count = Number(v.violation_count || v.count || 0);
        const samples = Array.isArray(v.sample_values) ? v.sample_values.slice(0, 5) : [];
        const cur = _constraintDecisionFromPayload(decisions, col, 'keep_as_is');
        return `
            <section class="dup-group-card" data-constraint-column="${escapeAttr(col)}">
                <div class="dup-group-card__header">
                    <strong>${escapeHtml(col)}</strong>
                    <span class="muted">${escapeHtml(issue)} · bound ${escapeHtml(String(bound))} · ${count} value(s)</span>
                </div>
                ${samples.length ? `<p class="dup-section__hint muted">Samples: ${escapeHtml(samples.map(String).join(', '))}</p>` : ''}
                <div class="dup-group-card__toolbar" role="toolbar" aria-label="Decision for ${escapeAttr(col)}">
                    ${_constraintChoiceBtn('keep_as_is', 'Keep as-is', cur === 'keep_as_is')}
                    ${_constraintChoiceBtn('clip_to_bound', 'Clip to bound', cur === 'clip_to_bound')}
                    ${_constraintChoiceBtn('drop_rows', 'Drop rows', cur === 'drop_rows')}
                </div>
            </section>
        `;
    }).join('');

    detailsEl.innerHTML = `
        <div class="review-detail-header">
            <div>
                <h2>${escapeHtml(data.filename || 'Template constraint review')}</h2>
                <p class="review-detail-subtitle">${escapeHtml(data.title || 'Values outside template min/max')}</p>
            </div>
            <div class="review-detail-meta">
                <span class="review-status-chip warning">${escapeHtml(String(data.severity || 'high'))}</span>
                <span class="review-detail-job-id">Job ${escapeHtml(data.job_id || '')}</span>
            </div>
        </div>
        <div class="reason-box reason-box-warning">
            <p><strong>What happened:</strong> ${escapeHtml(data.description || data.reason || 'Output values violate template minimum/maximum constraints.')}</p>
            <p class="reason-box-followup" style="margin-top: 8px;">
                Choose per column: keep negatives/outliers, clip them to the bound, or drop those rows — same style as duplicate review.
            </p>
        </div>
        <section class="dup-section" aria-label="Constraint violations">
            <h4 class="dup-section__title">Constraint breaches</h4>
            ${cards || '<p class="muted">No detailed constraint rows were attached.</p>'}
        </section>
        <section class="planner-review-card" style="margin-top: 12px;">
            <h4 style="margin: 0 0 8px;">Current data preview</h4>
            <div class="mini-data-preview">${previewHtml}</div>
        </section>
        <p class="planner-review-help" style="margin-top: 12px;">
            <strong>Approve</strong> applies your choices and continues.
            Unspecified columns default to <em>Keep as-is</em>.
            <strong>Cancel</strong> stops the job.
        </p>
        <div class="review-actions">
            <div class="action-buttons">
                <button class="btn-action approve" type="button" id="schemaConstraintApproveBtn">Approve</button>
                <button class="btn-action reject" type="button" id="schemaConstraintCancelBtn">Cancel Job</button>
            </div>
        </div>
    `;

    detailsEl.querySelectorAll('.dup-group-card').forEach((card) => {
        card.querySelectorAll('[data-constraint-action]').forEach((btn) => {
            btn.addEventListener('click', () => {
                card.querySelectorAll('[data-constraint-action]').forEach((b) => {
                    b.classList.remove('is-selected');
                    b.setAttribute('aria-pressed', 'false');
                });
                btn.classList.add('is-selected');
                btn.setAttribute('aria-pressed', 'true');
            });
        });
    });

    const approveBtn = document.getElementById('schemaConstraintApproveBtn');
    const cancelBtn = document.getElementById('schemaConstraintCancelBtn');
    if (approveBtn) {
        approveBtn.addEventListener('click', () => submitSchemaConstraintReview(data.checkpoint_id, 'approve'));
    }
    if (cancelBtn) {
        cancelBtn.addEventListener('click', () => submitSchemaConstraintReview(data.checkpoint_id, 'cancel'));
    }
}

function collectSchemaConstraintDecisions() {
    const decisions = {};
    document.querySelectorAll('.dup-group-card[data-constraint-column]').forEach((card) => {
        const col = card.getAttribute('data-constraint-column');
        const selected = card.querySelector('[data-constraint-action].is-selected');
        decisions[col] = selected
            ? (selected.getAttribute('data-constraint-action') || 'keep_as_is')
            : 'keep_as_is';
    });
    return decisions;
}

async function submitSchemaConstraintReview(checkpointId, action) {
    const resolutionData = action === 'cancel'
        ? { reason: 'Cancelled from template constraint review' }
        : { constraint_decisions: collectSchemaConstraintDecisions() };
    await resolveCheckpoint(checkpointId, action, resolutionData);
}

function renderVerificationStallContent(data) {
    const detailsEl = document.getElementById('reviewDetails');
    const triggerData = data.trigger_data || {};
    const jobId = data.job_id || '';
    const sheetName = triggerData.sheet_name || data.sheet_name || '';
    const sourceId = triggerData.source_id || data.source_id || '';
    const issues = Array.isArray(triggerData.verifier_issues) ? triggerData.verifier_issues : [];
    const actions = Array.isArray(data.available_actions) && data.available_actions.length
        ? data.available_actions
        : ['accept_as_is', 'retry', 'cancel'];
    const setupQs = new URLSearchParams();
    if (jobId) setupQs.set('job_id', jobId);
    if (sheetName) setupQs.set('sheet_name', sheetName);
    if (sourceId) setupQs.set('source_id', sourceId);
    const setupHref = `setup.html${setupQs.toString() ? `?${setupQs.toString()}` : ''}`;
    const issueList = issues.length
        ? `<ul class="planner-guided-setup-list">${issues.slice(0, 8).map((issue) => {
            const text = issue?.description || issue?.issue_type || issue?.summary || JSON.stringify(issue);
            return `<li>${escapeHtml(String(text))}</li>`;
        }).join('')}${issues.length > 8 ? '<li>…and more</li>' : ''}</ul>`
        : '<p class="planner-review-help">No verifier issue list was attached. Use Accept as-is to keep the current output, or Retry to run the latest plan again.</p>';

    detailsEl.innerHTML = `
        <div class="planner-sticky-actions review-actions">
            ${renderCheckpointActions(data.checkpoint_id, actions)}
        </div>
        <div class="review-detail-header">
            <div>
                <h2>${escapeHtml(data.filename || 'Checkpoint')}</h2>
                <p class="review-detail-subtitle">${escapeHtml(data.title || 'Replanner Escalation')}</p>
            </div>
            <div class="review-detail-meta">
                <span class="confidence-tag confidence-medium">${(Number(data.confidence || 0.5) * 100).toFixed(0)}% confidence</span>
                <span class="review-detail-job-id">Job ${escapeHtml(jobId)}</span>
            </div>
        </div>
        <div class="reason-box">
            <p><strong>Why paused:</strong> ${escapeHtml(data.reason || 'Automatic retry stopped.')}</p>
            ${data.description ? `<p style="margin-top: 8px;">${escapeHtml(data.description)}</p>` : ''}
        </div>
        <section class="planner-review-card">
            <h4 style="margin: 0 0 6px;">Latest verifier issues</h4>
            ${issueList}
        </section>
        <section class="planner-review-card">
            <div style="display:flex; align-items:center; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
                <div>
                    <h4 style="margin: 0;">Mapped output preview</h4>
                    <p class="planner-review-help" style="margin: 4px 0 0;">Selected / mapped columns from the current processed frame, not the full source sheet.</p>
                </div>
                <div style="display:flex; gap:8px; flex-wrap: wrap;">
                    <button type="button" id="checkpointLoadPreviewBtn" class="btn-secondary btn-sm"
                        data-checkpoint-id="${escapeAttr(data.checkpoint_id || '')}">
                        Refresh preview
                    </button>
                    <a class="btn-secondary btn-sm" href="${escapeAttr(setupHref)}">Open source in Guided Setup</a>
                </div>
            </div>
            <div id="checkpointSheetPreview" class="mini-data-preview" style="margin-top: 12px;">
                ${renderCheckpointDataPreview(
                    Array.isArray(data.data_preview) ? data.data_preview : [],
                    Array.isArray(data.data_preview_column_order) ? data.data_preview_column_order : [],
                )}
            </div>
        </section>
    `;

    const loadBtn = document.getElementById('checkpointLoadPreviewBtn');
    if (loadBtn) {
        loadBtn.addEventListener('click', () => {
            loadStallOutputPreview(loadBtn.dataset.checkpointId || data.checkpoint_id || '');
        });
    }
}

function renderCheckpointDetailContent(data) {
    if (data.type === 'column_decision') {
        renderColumnDecisionContent(data);
        return;
    }
    if (data.type === 'file_relationship_review') {
        renderRelationshipReviewContent(data);
        return;
    }
    if (data.type === 'checksum_failure') {
        renderIntegrityReviewContent(data);
        return;
    }
    if (data.type === 'schema_mismatch') {
        renderSchemaConstraintReviewContent(data);
        return;
    }
    if (data.type === 'verification_stall') {
        renderVerificationStallContent(data);
        return;
    }

    const detailsEl = document.getElementById('reviewDetails');
    const triggerData = data.trigger_data || {};
    const jobId = data.job_id || '';
    const sheetName = triggerData.sheet_name || data.sheet_name || '';
    const sourceId = triggerData.source_id || data.source_id || '';

    const findingsHtml = renderStructuralFindings(triggerData);
    const previewRows = Array.isArray(data.data_preview) ? data.data_preview : [];
    const previewHtml = renderCheckpointDataPreview(previewRows);
    const setupQs = new URLSearchParams();
    if (jobId) setupQs.set('job_id', jobId);
    if (sheetName) setupQs.set('sheet_name', sheetName);
    if (sourceId) setupQs.set('source_id', sourceId);
    const setupHref = `setup.html${setupQs.toString() ? `?${setupQs.toString()}` : ''}`;

    detailsEl.innerHTML = `
        <div class="review-detail-header">
            <div>
                <h2>${escapeHtml(data.filename || 'Checkpoint')}</h2>
                <p class="review-detail-subtitle">${escapeHtml(data.title || 'Checkpoint')}</p>
            </div>
            <div class="review-detail-meta">
                <span class="confidence-tag confidence-medium">${(Number(data.confidence || 0.5) * 100).toFixed(0)}% confidence</span>
                <span class="review-detail-job-id">Job ${escapeHtml(jobId)}</span>
            </div>
        </div>
        <div class="reason-box">
            <p><strong>Why paused:</strong> ${escapeHtml(data.reason || 'Manual review required')}</p>
            ${data.description ? `<p style="margin-top: 8px;">${escapeHtml(data.description)}</p>` : ''}
        </div>
        ${findingsHtml}
        <section class="planner-review-card">
            <div style="display:flex; align-items:center; justify-content: space-between; gap: 12px; flex-wrap: wrap;">
                <div>
                    <h4 style="margin: 0;">Sheet Data</h4>
                    <p class="planner-review-help" style="margin: 4px 0 0;">Preview the raw cells so you can decide whether to approve, pick a different header, or skip.</p>
                </div>
                <div style="display:flex; gap:8px; flex-wrap: wrap;">
                    <button type="button" id="checkpointLoadPreviewBtn" class="btn-secondary btn-sm"
                        data-job-id="${escapeAttr(jobId)}"
                        data-sheet-name="${escapeAttr(sheetName)}"
                        data-source-id="${escapeAttr(sourceId)}">
                        Load sheet preview
                    </button>
                    <a class="btn-secondary btn-sm" href="${escapeAttr(setupHref)}">Open in Guided Setup</a>
                </div>
            </div>
            <div id="checkpointSheetPreview" class="mini-data-preview" style="margin-top: 12px;">
                ${previewHtml}
            </div>
        </section>
        <details class="planner-review-card" style="margin-top: 12px;">
            <summary style="cursor:pointer; font-weight: 600;">Advanced: raw checkpoint payload</summary>
            <pre class="review-json-preview" style="margin-top: 8px;">${escapeHtml(JSON.stringify(triggerData, null, 2))}</pre>
        </details>
        <div class="review-actions">
            ${renderCheckpointActions(data.checkpoint_id, data.available_actions || ['approve'])}
        </div>
    `;

    const loadBtn = document.getElementById('checkpointLoadPreviewBtn');
    if (loadBtn) {
        loadBtn.addEventListener('click', () => {
            loadCheckpointSheetPreview(
                loadBtn.dataset.jobId || '',
                loadBtn.dataset.sheetName || '',
                loadBtn.dataset.sourceId || '',
            );
        });
    }
}

function renderStructuralFindings(td) {
    if (!td || typeof td !== 'object') return '';
    const hasStructuralBits =
        td.noise_score !== undefined ||
        (td.merged_cells && td.merged_cells.length) ||
        (td.header_candidates && td.header_candidates.length) ||
        (td.empty_rows && td.empty_rows.length) ||
        ((td.empty_columns && td.empty_columns.length) || (td.empty_cols && td.empty_cols.length));
    if (!hasStructuralBits) return '';

    const sourceBits = [td.source_id && `Source ${escapeHtml(String(td.source_id))}`, td.sheet_name && `Sheet ${escapeHtml(String(td.sheet_name))}`]
        .filter(Boolean)
        .join(' · ');
    const sourceLine = sourceBits ? `<p class="planner-review-help" style="margin: 0 0 10px;">${sourceBits}</p>` : '';

    const noisePercent = td.noise_score !== undefined ? `${(Number(td.noise_score) * 100).toFixed(0)}%` : 'n/a';
    const merged = Array.isArray(td.merged_cells) ? td.merged_cells.length : 0;
    const headerCandidates = Array.isArray(td.header_candidates) ? td.header_candidates : [];
    const emptyRows = Array.isArray(td.empty_rows) ? td.empty_rows.length : 0;
    const emptyCols = Array.isArray(td.empty_columns)
        ? td.empty_columns.length
        : (Array.isArray(td.empty_cols) ? td.empty_cols.length : 0);
    const headerList = headerCandidates.length
        ? `<ul class="planner-guided-setup-list" style="margin: 6px 0 0;">${headerCandidates.slice(0, 5).map(h => {
            const rowIdx = typeof h === 'object' ? (h.row ?? h.index ?? '?') : h;
            const sampleValues = typeof h === 'object' && Array.isArray(h.sample_values)
                ? h.sample_values.filter(Boolean).slice(0, 4).join(' | ')
                : '';
            const sample = sampleValues ? ` — ${escapeHtml(String(sampleValues).slice(0, 120))}` : '';
            return `<li>Row ${escapeHtml(String(Number(rowIdx) + 1))} <span class="item-id">(0-based: ${escapeHtml(String(rowIdx))})</span>${sample}</li>`;
        }).join('')}${headerCandidates.length > 5 ? '<li>…and more</li>' : ''}</ul>`
        : '';
    const headerPicker = headerCandidates.length
        ? `
            <div style="margin-top: 12px;">
                <label for="structuralHeaderRowSelect" class="planner-summary-label">Header row override</label>
                <select id="structuralHeaderRowSelect" class="planner-guidance-input" style="margin-top: 6px;">
                    ${headerCandidates.map(h => {
                        const rowIdx = typeof h === 'object' ? (h.row ?? h.index ?? '') : h;
                        const sampleValues = typeof h === 'object' && Array.isArray(h.sample_values)
                            ? h.sample_values.filter(Boolean).slice(0, 4).join(' | ')
                            : '';
                        const label = `Row ${Number(rowIdx) + 1}${sampleValues ? ` — ${sampleValues}` : ''}`;
                        return `<option value="${escapeAttr(String(rowIdx))}">${escapeHtml(label)}</option>`;
                    }).join('')}
                </select>
                <p class="planner-review-help" style="margin: 6px 0 0;">Use Select Header after choosing a candidate row. This now stores the chosen row instead of only dismissing the review item.</p>
            </div>
        `
        : '';

    const mergedMax = 18;
    const mergedList =
        merged > 0 && Array.isArray(td.merged_cells)
            ? (() => {
                  const slice = td.merged_cells.slice(0, mergedMax);
                  const rest = merged > mergedMax ? merged - mergedMax : 0;
                  const rows = slice
                      .map((m) => {
                          if (!m || typeof m !== 'object') return '';
                          const r = escapeHtml(String(m.range ?? ''));
                          const spans =
                              m.rows_spanned != null && m.cols_spanned != null
                                  ? `${Number(m.rows_spanned)}×${Number(m.cols_spanned)}`
                                  : '';
                          const tv =
                              m.top_left_value != null && String(m.top_left_value).trim() !== ''
                                  ? escapeHtml(String(m.top_left_value).slice(0, 80))
                                  : '';
                          return `<li><code>${r}</code>${spans ? ` <span class="muted">(${spans})</span>` : ''}${
                              tv ? ` — <span class="muted">${tv}</span>` : ''
                          }</li>`;
                      })
                      .filter(Boolean)
                      .join('');
                  const more = rest ? `<li class="muted">…and ${rest} more region(s) (see Advanced raw payload for full list)</li>` : '';
                  return `
            <details class="structural-merged-details" style="margin-top: 12px;">
                <summary style="cursor:pointer; font-weight: 600;">Merged Excel ranges (${merged}) — why fill / densify steps may follow</summary>
                <ul class="planner-guided-setup-list" style="margin: 8px 0 0;">${rows}${more}</ul>
            </details>`;
              })()
            : '';

    return `
        <section class="planner-review-card">
            <h4 style="margin: 0 0 6px;">Structural Findings</h4>
            ${sourceLine}
            <div class="planner-summary-grid">
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Noise Score</span>
                    <strong>${escapeHtml(noisePercent)}</strong>
                </div>
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Merged Regions</span>
                    <strong>${merged}</strong>
                </div>
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Header Candidates</span>
                    <strong>${headerCandidates.length}</strong>
                </div>
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Empty Rows / Cols</span>
                    <strong>${emptyRows} / ${emptyCols}</strong>
                </div>
            </div>
            ${mergedList}
            ${headerList}
            ${headerPicker}
        </section>
    `;
}

function looksLikeIntegerPreviewKeys(keys) {
    return Array.isArray(keys) && keys.length > 0 && keys.every((key) => /^\d+$/.test(String(key).trim()));
}

function looksLikeHeaderPreviewRow(row, keys) {
    if (!row || !Array.isArray(keys) || keys.length === 0) return false;
    const named = keys.filter((key) => {
        const token = String(row[key] ?? '').trim();
        if (!token || ['nan', 'none', 'null'].includes(token.toLowerCase())) return false;
        if (/^\d+$/.test(token)) return false;
        const numeric = Number(token.replace(/,/g, ''));
        return !Number.isFinite(numeric);
    }).length;
    return named >= Math.max(1, Math.floor(0.6 * keys.length));
}

function renderCheckpointDataPreview(rows, columnOrder) {
    if (!Array.isArray(rows) || rows.length === 0) {
        return '<div class="empty-state compact" style="padding: 10px;">No processed preview is attached yet. Click <strong>Refresh preview</strong> to load mapped columns.</div>';
    }
    const columns = Array.isArray(columnOrder) && columnOrder.length
        ? columnOrder.map(String)
        : Object.keys(rows[0] || {});
    const firstRow = rows[0] || {};
    const promoteHeader = looksLikeIntegerPreviewKeys(columns) && looksLikeHeaderPreviewRow(firstRow, columns);
    const headers = promoteHeader
        ? columns.map((key, idx) => String(firstRow[key] ?? '').trim() || `Column ${idx + 1}`)
        : columns;
    const bodyRows = promoteHeader ? rows.slice(1) : rows;
    return `
        <table>
            <thead>
                <tr>${headers.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr>
            </thead>
            <tbody>
                ${bodyRows.slice(0, 30).map(row => `
                    <tr>${columns.map(c => `<td>${escapeHtml(String(row[c] ?? ''))}</td>`).join('')}</tr>
                `).join('')}
            </tbody>
        </table>
    `;
}

async function loadStallOutputPreview(checkpointId) {
    const container = document.getElementById('checkpointSheetPreview');
    if (!container) return;
    if (!checkpointId) {
        container.innerHTML = '<div class="error">Missing checkpoint id; cannot load preview.</div>';
        return;
    }
    container.innerHTML = '<div class="loading">Loading mapped preview…</div>';
    try {
        const data = await fetchJson(`/api/review/checkpoint/${encodeURIComponent(checkpointId)}`);
        const rows = Array.isArray(data.data_preview) ? data.data_preview : [];
        container.innerHTML = renderCheckpointDataPreview(
            rows,
            Array.isArray(data.data_preview_column_order) ? data.data_preview_column_order : [],
        );
    } catch (e) {
        console.error('[Review] Failed to load stall preview:', e);
        container.innerHTML = `<div class="error">Failed to load preview: ${escapeHtml(e.message || String(e))}</div>`;
    }
}

async function loadCheckpointSheetPreview(jobId, sheetName, sourceId) {
    const container = document.getElementById('checkpointSheetPreview');
    if (!container) return;
    if (!jobId) {
        container.innerHTML = '<div class="error">Missing job id; cannot load preview.</div>';
        return;
    }
    container.innerHTML = '<div class="loading">Loading sheet preview…</div>';
    try {
        const qs = new URLSearchParams();
        if (sheetName) qs.set('sheet_name', sheetName);
        if (sourceId) qs.set('source_id', sourceId);
        qs.set('start_row', '0');
        qs.set('end_row', '40');
        qs.set('start_col', '0');
        qs.set('end_col', '20');
        const data = await fetchJson(`/api/demarcation/preview/${jobId}?${qs.toString()}`);
        const rows = Array.isArray(data.preview) ? data.preview : [];
        if (rows.length === 0) {
            container.innerHTML = '<div class="empty-state compact" style="padding: 10px;">No rows returned for this sheet preview.</div>';
            return;
        }
        const keys = Array.isArray(data.columns) && data.columns.length
            ? data.columns.map(String)
            : Object.keys(rows[0] || {});
        const firstRow = rows[0] || {};
        const promoteHeader = looksLikeIntegerPreviewKeys(keys) && looksLikeHeaderPreviewRow(firstRow, keys);
        const headers = promoteHeader
            ? keys.map((key, idx) => {
                const label = String(firstRow[key] ?? '').trim();
                return label || (Array.isArray(data.column_letters) ? data.column_letters[idx] : '') || `Column ${idx + 1}`;
            })
            : (Array.isArray(data.column_letters) && data.column_letters.length === keys.length
                ? data.column_letters.map(String)
                : keys);
        const bodyRows = promoteHeader ? rows.slice(1) : rows;
        const rangeNote = data.excel_range
            ? (promoteHeader
                ? `Range: <code>${escapeHtml(String(data.excel_range))}</code>. First row used as column names.`
                : `Range: <code>${escapeHtml(String(data.excel_range))}</code>. Column letters are Excel positions; the first data row is often the real header.`)
            : '';
        container.innerHTML = `
            ${rangeNote ? `<p class="planner-review-help" style="margin: 0 0 6px;">${rangeNote}</p>` : ''}
            <table>
                <thead>
                    <tr>${headers.map(c => `<th>${escapeHtml(String(c))}</th>`).join('')}</tr>
                </thead>
                <tbody>
                    ${bodyRows.slice(0, 40).map(row => `
                        <tr>${keys.map(c => `<td>${escapeHtml(String(row[c] ?? ''))}</td>`).join('')}</tr>
                    `).join('')}
                </tbody>
            </table>
        `;
    } catch (e) {
        console.error('[Review] Failed to load sheet preview:', e);
        container.innerHTML = `<div class="error">Failed to load sheet preview: ${escapeHtml(e.message || String(e))}</div>`;
    }
}

function renderRelationshipReviewContent(data) {
    const cpId = data.checkpoint_id || reviewState.selectedCheckpointId || '';
    const detailsEl = document.getElementById('reviewDetails');
    const triggerData = data.trigger_data || {};
    const sources = Array.isArray(triggerData.sources) ? triggerData.sources : [];
    const proposals = Array.isArray(triggerData.relationship_proposals) ? triggerData.relationship_proposals : [];
    const hero = _pickHeroProposal(proposals);
    const heroIndex = hero ? proposals.indexOf(hero) : 0;
    const initialDupMode = String((hero && hero.duplicate_merge_mode) || 'auto').trim() || 'auto';
    const lab = _combineOperationLabel(hero && hero.relationship_kind);
    const c = Number(data.confidence);
    const confPct = Number.isFinite(c) && c > 0 ? `${(c * 100).toFixed(0)}%` : null;
    const confClass =
        Number.isFinite(c) && c > 0.9 ? 'confidence-high' : Number.isFinite(c) && c > 0.6 ? 'confidence-medium' : 'confidence-low';
    const alignLine = _alignmentColumnsLine(hero, sources);
    const kindLower = String(hero && hero.relationship_kind || '').toLowerCase();
    const gate = triggerData.pipeline_evals_gate || {};
    const gateBlocked = gate.pass === false;
    const gateBanner = gateBlocked ? `
        <div class="reason-box" style="border-color: var(--danger); background: rgba(239,68,68,0.08); margin-bottom: 16px;">
            <p><strong>Evals blocked export</strong> — fix critical integrity issues before merging (e.g. context bleed across sheets).</p>
            <ul style="margin: 8px 0 0; font-size: 0.85rem;">${(gate.blocked_reasons || []).slice(0, 6).map((r) => `<li>${escapeHtml(r)}</li>`).join('')}</ul>
            <p style="margin-top: 10px; font-size: 0.85rem;">You may <strong>Proceed anyway</strong> with a logged reason (not recommended for production data).</p>
            <label style="display:block;margin-top:8px;font-size:0.85rem;">Override reason (required to proceed)</label>
            <input type="text" id="pipelineEvalsOverrideReason" class="planner-rule-input" placeholder="e.g. Analyst verified bleed is acceptable for this draft" />
        </div>
    ` : '';
    const colHeading =
        kindLower.includes('union') || kindLower.includes('independent')
            ? 'Column alignment'
            : 'Keys & columns';

    detailsEl.innerHTML = `
        <div class="combine-confirm" data-combine-confirm="1">
            ${gateBanner}
            <div class="combine-confirm-header">
                <div>
                    <h2 class="combine-confirm-title">Confirm how data will be combined</h2>
                    <p class="combine-confirm-sub muted">One operation will be applied before processing continues.</p>
                </div>
                <div class="combine-confirm-badge-wrap">
                    ${confPct ? `<span class="combine-confirm-badge ${confClass}">Recommended · ${escapeHtml(confPct)} confidence</span>` : '<span class="review-status-chip warning">Review</span>'}
                </div>
            </div>

            <section class="combine-panel">
                <div class="combine-panel-label">
                    <span class="combine-panel-label-mark" aria-hidden="true">⚙️</span>
                    <h3 class="combine-panel-title">Operation</h3>
                </div>
                <p class="combine-op-line" data-combine-op-display><strong>${escapeHtml(lab.headline)}</strong> <span class="muted">(${escapeHtml(lab.sub)})</span></p>
                <button type="button" class="btn-link combine-change-link" data-toggle-combine-edit aria-expanded="false">Change operation</button>
            </section>

            <section class="combine-panel">
                <div class="combine-panel-label">
                    <span class="combine-panel-label-mark" aria-hidden="true">📂</span>
                    <h3 class="combine-panel-title">Sources</h3>
                </div>
                <div class="combine-source-tree">${_sourcesFileTreeHtml(sources)}</div>
            </section>

            <section class="combine-panel">
                <div class="combine-panel-label">
                    <span class="combine-panel-label-mark" aria-hidden="true">⊞</span>
                    <h3 class="combine-panel-title">${escapeHtml(colHeading)}</h3>
                </div>
                <p class="combine-columns-line"><code>${escapeHtml(alignLine)}</code></p>
            </section>

            <section class="combine-panel">
                <div class="combine-panel-label">
                    <span class="combine-panel-label-mark" aria-hidden="true">↔️</span>
                    <h3 class="combine-panel-title">Before → after (samples)</h3>
                </div>
                <div id="combinePreviewMount" class="combine-preview-mount"></div>
            </section>

            <div id="combineChangeDrawer" class="combine-change-drawer" hidden>
                <div class="combine-form-stack">
                    ${_renderHiddenRelationshipFormCards(proposals, sources, heroIndex)}
                </div>
            </div>

            <div class="review-actions combine-confirm-actions" data-relationship-review-actions>
                <div class="relationship-review-actions-row">
                    <button type="button" class="btn-primary" onclick="submitRelationshipReview('${escapeJsString(cpId)}', 'approve')">${gateBlocked ? 'Proceed anyway' : 'Confirm &amp; continue'}</button>
                    <button type="button" class="btn-danger btn-iconish" onclick="submitRelationshipReview('${escapeJsString(cpId)}', 'cancel')" title="Stop run">Stop run</button>
                </div>
            </div>
        </div>
    `;
    _wireCombineChangeDrawer(detailsEl);
    void hydrateRelationshipSamples(cpId, initialDupMode, {});
}

function renderColumnDecisionContent(data) {
    const detailsEl = document.getElementById('reviewDetails');
    const triggerData = data.trigger_data || {};
    const proposedMappings = Array.isArray(triggerData.proposed_mappings) ? triggerData.proposed_mappings : [];
    const unresolvedItems = Array.isArray(triggerData.unresolved_items) ? triggerData.unresolved_items : [];
    const keptMappings = proposedMappings.filter(item => String(item.decision || '').toLowerCase() !== 'discard');
    const mappedCount = keptMappings.filter(item => item.target_column && item.target_column !== 'No match').length;
    const excludedCount = proposedMappings.filter(item => String(item.decision || '').toLowerCase() === 'discard').length;
    const confidenceValues = keptMappings
        .map(item => Number(item.target_match_confidence ?? item.confidence ?? 0))
        .filter(value => Number.isFinite(value) && value > 0);
    const averageConfidence = confidenceValues.length
        ? `${(confidenceValues.reduce((sum, value) => sum + value, 0) / confidenceValues.length * 100).toFixed(0)}%`
        : 'N/A';

    detailsEl.innerHTML = `
        <div class="review-detail-header">
            <div>
                <h2>${escapeHtml(data.filename || 'Checkpoint')}</h2>
                <p class="review-detail-subtitle">${escapeHtml(data.title || 'Column Mapping Review')}</p>
            </div>
            <div class="review-detail-meta">
                <span class="confidence-tag confidence-medium" style="font-size: 14px; padding: 4px 10px;">
                    ${(Number(data.confidence || 0.5) * 100).toFixed(0)}% Confidence
                </span>
                <span class="review-detail-job-id">Job ID: ${escapeHtml(data.job_id || '')}</span>
            </div>
        </div>

        <div class="reason-box">
            <p><strong>What this means:</strong> The agent proposed source-to-target column mappings, but some required target fields still need review.</p>
            <p class="reason-box-followup">${escapeHtml(data.reason || 'Review the proposed mappings before continuing.')}</p>
            ${data.description ? `<p class="reason-box-followup">${escapeHtml(data.description)}</p>` : ''}
        </div>

        <section class="planner-review-card planner-summary-card">
            <div class="planner-summary-grid">
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Proposed Mappings</span>
                    <strong>${proposedMappings.length}</strong>
                </div>
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Mapped Targets</span>
                    <strong>${mappedCount}</strong>
                </div>
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Unresolved Required</span>
                    <strong>${unresolvedItems.length}</strong>
                </div>
                <div class="planner-summary-stat">
                    <span class="planner-summary-label">Average Confidence</span>
                    <strong>${averageConfidence}</strong>
                </div>
            </div>
            <div class="planner-summary-note">
                Approve if these mappings look right overall. If the missing required targets are a problem, keep this item for follow-up instead of approving blindly.
            </div>
        </section>

        <section class="planner-review-card">
            <h4>Required Targets Still Missing</h4>
            <p class="planner-review-help">These mandatory target fields do not currently have a source column mapped to them.</p>
            ${unresolvedItems.length
                ? `<div class="mapping-chip-list">${unresolvedItems.map(item => `
                    <span class="planner-chip danger">${escapeHtml(item.target_column || 'Unknown target')}</span>
                `).join('')}</div>`
                : '<div class="empty-state compact">All mandatory target fields currently have a proposed mapping.</div>'}
        </section>

        <section class="planner-review-card">
            <h4>Proposed Source To Target Mappings</h4>
            <p class="planner-review-help">This is the agent's current understanding of how your source columns map into the target schema.</p>
            <div class="mapping-review-list">
                ${proposedMappings.length
                    ? proposedMappings.map(mapping => renderMappingReviewCard(mapping)).join('')
                    : '<div class="empty-state compact">No proposed mappings were attached to this checkpoint.</div>'}
            </div>
        </section>

        <section class="planner-review-card">
            <h4>What To Do</h4>
            <div class="mapping-guidance-list">
                <div class="mapping-guidance-item"><strong>Approve</strong> if the mappings look acceptable and you just want to clear this review item.</div>
                <div class="mapping-guidance-item"><strong>Resolve</strong> if you have reviewed the mapping issue and want to mark it as handled.</div>
                <div class="mapping-guidance-item">If an important target is missing, use the setup or mapping workflow to correct it before relying on the output.</div>
            </div>
        </section>

        <div class="review-actions">
            ${renderCheckpointActions(data.checkpoint_id, data.available_actions || ['approve'])}
        </div>
    `;
}

function renderMappingReviewCard(mapping) {
    const sourceColumn = mapping.source_column || 'Unknown source column';
    const targetColumn = mapping.target_column || 'No match';
    const decision = String(mapping.decision || 'review').trim() || 'review';
    const confidence = Number(mapping.target_match_confidence ?? mapping.confidence ?? 0);
    const confidenceLabel = Number.isFinite(confidence) && confidence > 0 ? `${(confidence * 100).toFixed(0)}%` : 'N/A';
    const classification = mapping.classification || mapping.source_column_type || 'Unclassified';
    const reasoning = mapping.reasoning || 'No explanation was attached for this mapping.';
    const isDiscard = decision.toLowerCase() === 'discard';

    return `
        <div class="mapping-review-card ${isDiscard ? 'discarded' : ''}">
            <div class="mapping-review-header">
                <div class="mapping-review-route">
                    <span class="mapping-review-source">${escapeHtml(sourceColumn)}</span>
                    <span class="mapping-review-arrow">-></span>
                    <span class="mapping-review-target">${escapeHtml(targetColumn)}</span>
                </div>
                <div class="mapping-review-badges">
                    <span class="field-badge ${isDiscard ? 'discard' : 'dimension'}">${escapeHtml(decision)}</span>
                    <span class="field-badge metric">${escapeHtml(confidenceLabel)}</span>
                </div>
            </div>
            <div class="mapping-review-meta">
                <span><strong>Type:</strong> ${escapeHtml(classification)}</span>
                <span><strong>Method:</strong> ${escapeHtml(mapping.target_match_method || 'n/a')}</span>
            </div>
            <div class="mapping-review-reason">${escapeHtml(reasoning)}</div>
        </div>
    `;
}

async function handleApproveDeletions(jobId) {
    try {
        const approveResult = await fetchJson(`/api/jobs/${jobId}/approve-deletions`, {
            method: 'POST',
            body: JSON.stringify({ approve_all: true })
        });

        if (approveResult.success) {
            showToast('Deletions approved. Resuming...', 'success');
            const resumeResult = await fetchJson(`/api/jobs/${jobId}/resume`, { method: 'POST' });
            if (resumeResult.success) {
                showToast('Job completed successfully!', 'success');
                reviewState.approved++;
                fetchPendingReviews();
                document.getElementById('reviewDetails').innerHTML = `
                    <div class="empty-state">
                        <div class="empty-icon">✅</div>
                        <h3>Processing Resumed</h3>
                        <p>The job has finished. Check the dashboard for results.</p>
                        <button class="btn-primary" onclick="navigateTo('dashboard')" style="margin-top: 16px;">Go to Dashboard</button>
                    </div>
                `;
            }
        }
    } catch (e) {
        console.error('[Review] Error resuming:', e);
        showToast('Failed to resume processing', 'error');
    }
}

async function handleRejectDeletions(jobId) {
    try {
        const result = await fetchJson(`/api/jobs/${jobId}/reject-deletions`, { method: 'POST' });
        if (result.success) {
            showToast('Job rejected', 'success');
            reviewState.rejected++;
            fetchPendingReviews();
            document.getElementById('reviewDetails').innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">❌</div>
                    <h3>Job Rejected</h3>
                    <p>Destructive operations were cancelled and the job stopped.</p>
                </div>
            `;
        }
    } catch (e) {
        console.error('[Review] Error rejecting deletions:', e);
        showToast('Failed to reject', 'error');
    }
}

function renderReviewDetailContent(data) {
    const fields = data.schema_preview?.fields || [];
    const previewData = data.data_preview || [];
    const columns = previewData.length > 0 ? Object.keys(previewData[0]) : [];

    const confClass = data.confidence > 0.9 ? 'confidence-high' :
        (data.confidence > 0.6 ? 'confidence-medium' : 'confidence-low');

    const detailsEl = document.getElementById('reviewDetails');
    detailsEl.innerHTML = `
        <div class="review-detail-header">
            <div>
                <h2>${escapeHtml(data.filename)}</h2>
                <p class="review-detail-subtitle">Low-confidence review</p>
            </div>
            <div class="review-detail-meta">
                <span class="confidence-tag ${confClass}">${(data.confidence * 100).toFixed(0)}% confidence</span>
                <span class="review-detail-job-id">Job ${escapeHtml(data.job_id)}</span>
            </div>
        </div>

        <div class="reason-box">
            <p><strong>Review reason:</strong> ${escapeHtml(data.reason)}</p>
        </div>

        <div class="preview-section">
            <h4>Execution History</h4>
            <div class="field-list" style="max-height: 150px; overflow-y: auto;">
                ${(data.tool_history || []).filter(s => s.step === 'execute_tools').map(step => `
                    <div class="field-item">
                        <span style="font-weight: 600;">🛠️ Executed Tools:</span>
                        <span style="margin-left: 8px; color: var(--text-secondary);">${(step.tools || []).join(', ') || 'No tools recorded'}</span>
                    </div>
                `).join('') || '<p style="padding: 8px; color: var(--text-muted);">No tools executed.</p>'}
            </div>
        </div>

        <div class="preview-section">
            <h4>Inferred Schema Preview</h4>
            <div class="field-list" style="max-height: 200px; overflow-y: auto;">
                ${fields.map(f => `
                    <div class="field-item" style="padding: 8px 12px; margin-bottom: 4px; font-size: 12px;">
                        <span style="font-weight: 600; width: 120px; display: inline-block;">${escapeHtml(f.name)}</span>
                        <span class="field-badge ${f.role}" style="font-size: 10px; padding: 1px 6px;">${f.role}</span>
                        <span style="color: var(--text-muted); margin-left: auto;">${f.type}</span>
                    </div>
                `).join('')}
            </div>
        </div>

        <div class="preview-section">
            <h4>Data Preview</h4>
            <div class="mini-data-preview">
                <table>
                    <thead>
                        <tr>${columns.map(c => `<th>${escapeHtml(c)}</th>`).join('')}</tr>
                    </thead>
                    <tbody>
                        ${previewData.map(row => `
                            <tr>${columns.map(c => `<td>${escapeHtml(String(row[c] ?? ''))}</td>`).join('')}</tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="review-actions">
            <button class="btn-primary btn-approve" onclick="handleApprove('${data.job_id}')">
                Approve Results
            </button>
            <button class="btn-secondary btn-reject" onclick="handleReject('${data.job_id}')">
                Reject & Discard
            </button>
            <button class="btn-secondary" onclick="navigateTo('debug')" style="margin-left: auto;">
                Trace
            </button>
        </div>
    `;
}

async function handleApprove(jobId) {
    await approveReview(jobId);
    document.getElementById('reviewDetails').innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">👈</div>
            <h3>Select an Item</h3>
            <p>Click on a pending item to review</p>
        </div>
    `;
}

async function handleReject(jobId) {
    await rejectReview(jobId);
    document.getElementById('reviewDetails').innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">👈</div>
            <h3>Select an Item</h3>
            <p>Click on a pending item to review</p>
        </div>
    `;
}

// ===== Checkpoint Resolution =====
function setReviewActionLoading(isLoading) {
    document.querySelectorAll('.review-actions .btn-action, .planner-sticky-actions .btn-action').forEach((button) => {
        button.disabled = Boolean(isLoading);
    });
}

async function resolveCheckpoint(checkpointId, action, resolutionData = {}) {
    setReviewActionLoading(true);
    try {
        const result = await fetchJson(`/api/review/checkpoint/${checkpointId}/resolve`, {
            method: 'POST',
            body: JSON.stringify({ action, resolution_data: resolutionData || {} })
        });
        if (result.success) {
            const message = result.message || `Checkpoint resolved: ${action}`;
            showToast(message, 'success');
            if (result.status === 'processing' || result.status === 'queued' || result.status === 'completed') {
                routeJobByState(result);
                return;
            }
            await fetchPendingReviews({ skipAutoSelect: true });
            if (result.status === 'awaiting_review') {
                const targetId = result.next_checkpoint_id
                    || pickLatestPendingCheckpointId(reviewState.items, result.job_id);
                if (targetId) {
                    reviewState.selectedCheckpointId = targetId;
                    await showCheckpointDetail(targetId, { scrollIntoView: true });
                }
            } else if (result.status === 'cancelled') {
                reviewState.selectedCheckpointId = null;
                reviewState.selectedJobId = null;
            }
        } else {
            showToast(`Failed: ${result.error}`, 'error');
        }
    } catch (e) {
        console.error('[Review] Error resolving checkpoint:', e);
        showToast('Failed to resolve checkpoint', 'error');
    } finally {
        setReviewActionLoading(false);
    }
}

function collectStructuralReviewResolutionData(action) {
    if (action !== 'select_header_row') return {};
    const select = document.getElementById('structuralHeaderRowSelect');
    if (!select || !select.value) {
        throw new Error('Choose a header row before using Select Header.');
    }
    const headerRow = Number(select.value);
    if (Number.isNaN(headerRow)) {
        throw new Error('Selected header row is invalid.');
    }
    return { header_row: headerRow };
}

async function submitStructuralReview(checkpointId, action) {
    try {
        const resolutionData = collectStructuralReviewResolutionData(action);
        await resolveCheckpoint(checkpointId, action, resolutionData);
    } catch (e) {
        console.error('[Review] Error submitting structural review:', e);
        showToast(e.message || 'Failed to resolve structural review', 'error');
    }
}

async function submitPlanReview(checkpointId, action) {
    setPlanActionLoading(action, true);
    try {
        const resolutionData = action === 'cancel'
            ? collectPlanReviewResolutionData({ skipApprovalValidation: true })
            : collectPlanReviewResolutionData();
        // #region agent log
        fetch('http://127.0.0.1:7681/ingest/f5ccfc44-c685-4d6a-b01d-1cd50d8f8c97',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'3fc92d'},body:JSON.stringify({sessionId:'3fc92d',runId:String(checkpointId||'unknown'),hypothesisId:'H7_H8',location:'web/js/review.js:1257',message:'submitPlanReview start',data:{checkpointId,action,hasResolutionData:Boolean(resolutionData),updatedToolCallsCount:Array.isArray(resolutionData?.updated_tool_calls)?resolutionData.updated_tool_calls.length:null},timestamp:Date.now()})}).catch(()=>{});
        // #endregion
        if (action === 'cancel' && !resolutionData.reason) {
            resolutionData.reason = 'Cancelled from planner review';
        }

        const requestUrl = `/api/review/checkpoint/${checkpointId}/resolve`;
        const requestBody = JSON.stringify({ action, resolution_data: resolutionData });
        let result;
        try {
            result = await fetchJson(requestUrl, {
                method: 'POST',
                body: requestBody
            });
            // #region agent log
            fetch('http://127.0.0.1:7681/ingest/f5ccfc44-c685-4d6a-b01d-1cd50d8f8c97',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'3fc92d'},body:JSON.stringify({sessionId:'3fc92d',runId:String(checkpointId||'unknown'),hypothesisId:'H7',location:'web/js/review.js:1269',message:'submitPlanReview resolve success',data:{checkpointId,action,status:result?.status,success:result?.success,jobId:result?.job_id},timestamp:Date.now()})}).catch(()=>{});
            // #endregion
        } catch (requestError) {
            // #region agent log
            fetch('http://127.0.0.1:7681/ingest/f5ccfc44-c685-4d6a-b01d-1cd50d8f8c97',{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'3fc92d'},body:JSON.stringify({sessionId:'3fc92d',runId:String(checkpointId||'unknown'),hypothesisId:'H5_H6_H7',location:'web/js/review.js:1274',message:'submitPlanReview resolve failed',data:{checkpointId,action,requestUrl,error:String(requestError?.message||requestError),bodyLength:requestBody.length},timestamp:Date.now()})}).catch(()=>{});
            // #endregion
            throw requestError;
        }

        if (!result.success) {
            showToast(`Failed: ${result.error || 'Unable to resolve planner review'}`, 'error');
            return;
        }

        if (result.status === 'cancelled') {
            reviewState.selectedCheckpointId = null;
            reviewState.selectedJobId = null;
        }

        await fetchPendingReviews({ skipAutoSelect: true });

        if (result.status === 'awaiting_review') {
            showToast(result.message || 'Another review is ready.', 'warning');
            const targetId = result.next_checkpoint_id
                || pickLatestPendingCheckpointId(reviewState.items, result.job_id)
                || pickLatestPlanReviewCheckpointId(reviewState.items, result.job_id);
            if (targetId) {
                reviewState.selectedCheckpointId = targetId;
                await showCheckpointDetail(targetId, { scrollIntoView: true });
            }
        } else if (result.status === 'completed') {
            showToast(result.message || 'Processing complete.', 'success');
            routeJobByState(result);
        } else if (result.status === 'awaiting_approval') {
            showToast('Plan accepted. Paused again for an approval step.', 'warning');
        } else if (result.status === 'processing' || result.status === 'queued') {
            showToast(result.message || 'Resuming…', 'success');
            routeJobByState(result);
        } else if (result.status === 'cancelled') {
            showToast(result.message || 'Job cancelled.', 'warning');
            document.getElementById('reviewDetails').innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">🛑</div>
                    <h3>Job cancelled</h3>
                    <p>This job will not continue.</p>
                </div>
            `;
        } else {
            showToast(result.message || 'Saved.', 'success');
        }
    } catch (e) {
        console.error('[Review] Error submitting planner review:', e);
        showToast(e.message || 'Failed to submit planner review', 'error');
    } finally {
        setPlanActionLoading(action, false);
    }
}

function setRelationshipReviewSubmitting(isSubmitting) {
    const wrap = document.querySelector('[data-relationship-review-actions]');
    if (!wrap) return;
    wrap.querySelectorAll('button').forEach((btn) => {
        btn.disabled = Boolean(isSubmitting);
    });
}

async function submitRelationshipReview(checkpointId, action) {
    const effectiveId = reviewState.selectedCheckpointId || checkpointId;
    setRelationshipReviewSubmitting(true);
    try {
        const relationships = Array.from(document.querySelectorAll('[data-relationship-card]')).map((card) => {
            const isHero = card.getAttribute('data-hero-card') === '1';
            const rel = {
                relationship_id: card.dataset.relationshipId,
                relationship_kind: card.querySelector('[data-relationship-field="relationship_kind"]')?.value || 'independent',
                join_keys: (card.querySelector('[data-relationship-field="join_keys"]')?.value || '')
                    .split(',')
                    .map((value) => value.trim())
                    .filter(Boolean),
                status: 'approved',
            };
            if (isHero) {
                const mountEl = document.getElementById('combinePreviewMount');
                const dupMap = collectDuplicateKeyGroupDecisionsFromMount(mountEl);
                if (dupMap && Object.keys(dupMap).length) {
                    rel.duplicate_key_group_decisions = dupMap;
                }
                rel.duplicate_merge_mode = mountEl?.dataset?.duplicateMergeMode || 'auto';
            }
            return rel;
        });
        const analystNotes = document.getElementById('relationshipAnalystNotes')?.value?.trim() || '';
        const overrideReason = document.getElementById('pipelineEvalsOverrideReason')?.value?.trim() || '';
        const resolution = {
            relationships,
            analyst_notes: analystNotes,
            reason: analystNotes,
        };
        if (overrideReason) {
            resolution.override_reason = overrideReason;
            resolution.proceed_anyway = true;
        }
        if (action === 'approve') {
            showToast('Submitting… combining sources can take a minute. You will be sent to Processing.', 'info');
        }
        let result;
        try {
            result = await fetchJson(`/api/review/checkpoint/${encodeURIComponent(effectiveId)}/resolve`, {
                method: 'POST',
                body: JSON.stringify({
                    action,
                    resolution_data: resolution,
                })
            });
        } catch (err) {
            if (err?.status === 409 && err?.data?.requires_override) {
                showToast('Critical eval gate blocked export — enter an override reason to proceed anyway.', 'error');
                return;
            }
            throw err;
        }
        if (!result.success) {
            showToast(`Failed: ${result.error || 'Unable to resolve relationship review'}`, 'error');
            return;
        }
        if (result.status === 'cancelled') {
            reviewState.selectedCheckpointId = null;
            reviewState.selectedJobId = null;
        }
        const toastKind = result.status === 'cancelled' ? 'warning' : 'success';
        showToast(result.message || 'Relationship review resolved', toastKind);
        await fetchPendingReviews({ skipAutoSelect: true });
        if (result.status === 'processing' || result.status === 'queued') {
            routeJobByState(result);
            return;
        }
        if (result.status === 'completed') {
            routeJobByState(result);
            return;
        }
        if (result.status === 'cancelled') {
            document.getElementById('reviewDetails').innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">🛑</div>
                    <h3>Job cancelled</h3>
                    <p>This job will not continue.</p>
                </div>
            `;
            return;
        }
        const nextId = pickLatestPendingCheckpointId(reviewState.items, result.job_id);
        if (nextId) {
            reviewState.selectedCheckpointId = nextId;
            await showCheckpointDetail(nextId, { scrollIntoView: true });
        }
    } catch (e) {
        console.error('[Review] Error submitting relationship review:', e);
        const msg = e?.message || String(e);
        if (msg.includes('Checkpoint not found') || e?.status === 404) {
            await fetchPendingReviews();
            const fallbackId = (reviewState.items || []).find(
                (it) => it.type === 'file_relationship_review' && it.checkpoint_id,
            )?.checkpoint_id;
            showToast(
                fallbackId
                    ? 'That review item expired (server may have restarted). Select it again from the queue and retry.'
                    : 'That review item is no longer on the server. Refresh the queue and start a new run if needed.',
                'error',
            );
        } else {
            showToast(msg || 'Failed to submit relationship review', 'error');
        }
    } finally {
        setRelationshipReviewSubmitting(false);
    }
}

window.submitRelationshipReview = submitRelationshipReview;

function collectPlanReviewResolutionData(opts = {}) {
    const skipApproval = opts.skipApprovalValidation === true;
    const analystNotes = document.getElementById('plannerAnalystNotes')?.value?.trim() || '';
    const updatedReasoning = document.getElementById('plannerReasoning')?.value?.trim() || '';
    const expectedColumnsRaw = document.getElementById('plannerExpectedColumns')?.value || '';

    let approvalNotes = [];
    if (!skipApproval) {
        const cards = Array.from(document.querySelectorAll('[data-approval-card]'));
        approvalNotes = cards.map((card) => {
            const index = Number(card.dataset.approvalIndex);
            const choiceEl = card.querySelector(`input[name="planner-approval-choice-${index}"]:checked`);
            const yn = card.querySelector(`input[name="planner-approval-yn-${index}"]:checked`)?.value || '';
            const extra = card.querySelector('.planner-approval-extra')?.value?.trim() || '';
            let analyst_confirm = '';
            const parts = [];
            const NOTES_ONLY_MIN = 15;
            if (choiceEl) {
                let rawId = '';
                try {
                    rawId = decodeURIComponent(choiceEl.value || '');
                } catch {
                    rawId = choiceEl.value || '';
                }
                analyst_confirm = rawId;
                const label = choiceEl.closest('label')?.textContent?.trim() || rawId;
                parts.push(`Choice: ${label}`);
                if (rawId === 'other_notes' && extra.length < NOTES_ONLY_MIN) {
                    throw new Error(
                        `Item ${index + 1}: "None of the above" needs at least ${NOTES_ONLY_MIN} characters in notes (you have ${extra.length}).`
                    );
                }
            } else if (yn === 'yes' || yn === 'no') {
                analyst_confirm = yn;
                parts.push(`Answer: ${yn === 'yes' ? 'Yes' : 'No'}`);
            } else if (extra.length >= NOTES_ONLY_MIN) {
                analyst_confirm = 'notes_only';
                parts.push(`Notes-only (no listed option selected): ${extra}`);
            } else {
                throw new Error(
                    `Please select an option for checklist item ${index + 1}, or write at least ${NOTES_ONLY_MIN} characters in notes if none of the choices apply.`
                );
            }
            if (extra && analyst_confirm !== 'notes_only') parts.push(`Notes: ${extra}`);
            return {
                index,
                resolution_note: parts.join(' · '),
                analyst_confirm: analyst_confirm,
            };
        });
    }

    const updatedToolCalls = Array.from(document.querySelectorAll('.planner-tool-card')).map((card, index) => {
        const toolName = card.querySelector('[data-tool-field="tool"]')?.value?.trim() || '';
        const description = card.querySelector('[data-tool-field="description"]')?.value?.trim() || '';
        const paramsText = card.querySelector('[data-tool-field="params"]')?.value?.trim() || '{}';
        let params = {};
        try {
            params = paramsText ? JSON.parse(paramsText) : {};
        } catch (e) {
            throw new Error(`Plan step ${index + 1} has invalid JSON parameters.`);
        }
        return {
            step: Number(card.dataset.originalStep || index + 1),
            tool: toolName,
            params,
            description,
        };
    }).filter(item => item.tool);

    const ruleGroups = new Map();
    document.querySelectorAll('.planner-rule-input').forEach(input => {
        const index = Number(input.dataset.ruleIndex);
        const field = input.dataset.ruleField;
        if (!ruleGroups.has(index)) {
            ruleGroups.set(index, {});
        }
        ruleGroups.get(index)[field] = input.value.trim();
    });

    const updatedRuleActions = Array.from(ruleGroups.values())
        .filter(item => item.target_column || item.action || item.value || item.description);

    const updatedExpectedColumns = Array.from(new Set(
        expectedColumnsRaw
            .split(/\n|,/)
            .map(value => value.trim())
            .filter(Boolean)
    ));

    return {
        analyst_notes: analystNotes,
        approval_items: approvalNotes,
        updated_tool_calls: updatedToolCalls,
        updated_expected_columns: updatedExpectedColumns,
        updated_reasoning: updatedReasoning,
        updated_rule_actions: updatedRuleActions,
        reason: analystNotes || updatedReasoning,
    };
}

/** Plain-language title from internal tool id (e.g. layout.extract). */
function humanizePlanToolTitle(toolName) {
    const n = String(toolName || '').toLowerCase();
    if (!toolName) return 'Processing step';
    if (n.includes('layout.extract')) return 'Extract the data table from the sheet';
    if (n.includes('infer_block_boundary')) return 'Infer candidate block delimiter columns';
    if (n.includes('classify_metric_level')) return 'Detect block vs row-level metric grain';
    if (n.includes('allocate_block_metric')) return 'Split block totals across rows (equal or weighted)';
    if (n.includes('aggregate_weekly')) return 'Roll up rows to weekly Monday dates';
    if (n.includes('date_range_to_weekly')) return 'Convert date ranges into weekly buckets';
    if (n.includes('expand_period_to_daily')) return 'Expand period totals into daily rows';
    if (n.includes('infer_granularity_expand_to_daily')) return 'Infer date cadence, expand to daily rows';
    if (n.includes('build_date_from_parts')) return 'Build a date column from parts';
    if (n.includes('fill_merged') || n.includes('unmerge_and_fill')) return 'Fill values from merged / parent cells';
    if (n.includes('drop_blank_columns')) return 'Remove blank columns';
    if (n.includes('drop_blank_rows')) return 'Remove completely empty rows';
    if (n.includes('drop_columns')) return 'Remove columns';
    if (n.includes('densify')) return 'Fill down sparse dimension values';
    if (n.includes('rename')) return 'Rename columns';
    if (n.includes('filter_summaries') || n.includes('filter_empty')) return 'Filter rows';
    if (n.includes('skip_rows')) return 'Skip header or spacer rows';
    if (n.includes('merge')) return 'Merge or combine data';
    if (n.includes('normalize')) return 'Normalize values';
    if (n.includes('type_cast') || n.includes('cast') || n.includes('dtype')) return 'Adjust column types';
    if (n.includes('deduplicate')) return 'Remove duplicate rows';
    if (n.includes('transpose')) return 'Transpose the table';
    if (n.includes('align_columns')) return 'Align columns to a template';
    if (n.includes('map_values')) return 'Map or recode values';
    if (n.includes('calculate')) return 'Calculate derived columns';
    if (n.includes('format')) return 'Format cell values';
    if (n.includes('unpivot')) return 'Unpivot wide columns to long format';
    if (n.includes('pivot')) return 'Pivot or reshape the table';
    if (n.includes('date') && (n.includes('iso') || n.includes('parse') || n.includes('standard'))) return 'Standardize dates';
    return 'Run an automation step';
}

function planReadoutStepTitle(tool) {
    const id = String(tool.tool || tool.name || '').trim();
    const title = humanizePlanToolTitle(id);
    if (title !== 'Run an automation step') return title;
    if (!id) return title;
    const tail = id.includes('.') ? id.slice(id.lastIndexOf('.') + 1) : id;
    const words = tail.replace(/_/g, ' ').trim();
    return words ? `Run: ${words}` : title;
}

/** Short human summary of JSON params (indices treated as 0-based → show 1-based row/col for readability). */
function formatPlanParamsForHumans(params) {
    if (!params || typeof params !== 'object') return '';
    const p = params;
    const bits = [];
    if (p.header_row != null && p.header_row !== '') {
        const hr = Number(p.header_row);
        if (!Number.isNaN(hr)) bits.push(`header row in spreadsheet: ${hr + 1}`);
    }
    if (p.start_row != null && p.end_row != null) {
        const a = Number(p.start_row);
        const b = Number(p.end_row);
        if (!Number.isNaN(a) && !Number.isNaN(b)) bits.push(`rows ${a + 1}–${b + 1}`);
    }
    if (p.start_col != null && p.end_col != null) {
        const a = Number(p.start_col);
        const b = Number(p.end_col);
        if (!Number.isNaN(a) && !Number.isNaN(b)) bits.push(`columns ${a + 1}–${b + 1}`);
    }
    if (p.threshold != null && p.threshold !== '') bits.push(`threshold: ${p.threshold}`);
    if (Array.isArray(p.columns) && p.columns.length) {
        const cols = p.columns.map((c) => String(c)).slice(0, 6);
        bits.push(`columns: ${cols.join(', ')}${p.columns.length > 6 ? '…' : ''}`);
    }
    if (Array.isArray(p.dimension_cols) && p.dimension_cols.length) {
        const cols = p.dimension_cols.map((c) => String(c)).slice(0, 6);
        bits.push(`fill down: ${cols.join(', ')}${p.dimension_cols.length > 6 ? '…' : ''}`);
    }
    if (Array.isArray(p.group_by_cols) && p.group_by_cols.length) {
        const cols = p.group_by_cols.map((c) => String(c)).slice(0, 6);
        bits.push(`group by: ${cols.join(', ')}${p.group_by_cols.length > 6 ? '…' : ''}`);
    }
    return bits.join(' · ');
}

function buildPlanStepReadableHtml(tool, index) {
    const toolName = tool.tool || tool.name || '';
    const description = (tool.description || tool.reason || '').trim();
    const params = tool.params || {};
    const stepNumber = Number(tool.step || index + 1);
    const title = humanizePlanToolTitle(toolName);
    const scopeLine = formatPlanParamsForHumans(params);
    const descBlock = description
        ? `<p class="planner-tool-readable-desc">${escapeHtml(description.length > 400 ? `${description.slice(0, 397)}…` : description)}</p>`
        : '';
    const scopeBlock = scopeLine
        ? `<p class="planner-tool-readable-scope"><span class="planner-tool-readable-scope-label">Details</span> ${escapeHtml(scopeLine)}</p>`
        : '';
    return `
        <div class="planner-tool-readable">
            <div class="planner-tool-readable-head">
                <span class="planner-item-index">Step ${stepNumber}</span>
                <h5 class="planner-tool-readable-title">${escapeHtml(title)}</h5>
            </div>
            ${scopeBlock}
            ${descBlock}
        </div>`;
}

window.toggleAllPlannerTechnicalSteps = function (open) {
    document.querySelectorAll('.planner-tool-technical').forEach((el) => {
        el.open = !!open;
    });
};

function renderPlanToolEditor(tool, index) {
    const toolName = tool.tool || tool.name || '';
    const description = tool.description || tool.reason || '';
    const params = tool.params || {};
    const stepNumber = Number(tool.step || index + 1);
    const readable = buildPlanStepReadableHtml(tool, index);
    return `
        <div class="planner-tool-card" data-original-step="${stepNumber}">
            ${readable}
            <details class="planner-tool-technical">
                <summary class="planner-tool-technical-summary">Technical details — tool id, description, parameters (JSON)</summary>
                <div class="planner-tool-grid">
                    <input class="planner-rule-input" data-tool-field="tool" value="${escapeHtml(toolName)}" placeholder="Tool name">
                    <input class="planner-rule-input" data-tool-field="description" value="${escapeHtml(description)}" placeholder="Why this step exists">
                </div>
                <textarea class="planner-tool-params" data-tool-field="params" spellcheck="false" placeholder='{"columns": ["foo", "bar"]}'>${escapeHtml(JSON.stringify(params, null, 2))}</textarea>
            </details>
        </div>
    `;
}

function renderPlanRuleActionEditor(item = {}, index = 0) {
    return `
        <div class="planner-rule-row" data-rule-row="${index}">
            <input class="planner-rule-input" data-rule-index="${index}" data-rule-field="target_column" value="${escapeHtml(item.target_column || '')}" placeholder="Target column">
            <input class="planner-rule-input" data-rule-index="${index}" data-rule-field="action" value="${escapeHtml(item.action || '')}" placeholder="Action">
            <input class="planner-rule-input" data-rule-index="${index}" data-rule-field="value" value="${escapeHtml(item.value ?? '')}" placeholder="Value">
            <input class="planner-rule-input" data-rule-index="${index}" data-rule-field="description" value="${escapeHtml(item.description || '')}" placeholder="Description">
        </div>
    `;
}

function addPlanRuleActionRow() {
    const list = document.getElementById('plannerRuleList');
    if (!list) return;
    const nextIndex = list.querySelectorAll('.planner-rule-row').length;
    const wrapper = document.createElement('div');
    wrapper.innerHTML = renderPlanRuleActionEditor({}, nextIndex).trim();
    list.appendChild(wrapper.firstElementChild);
}

function buildFallbackPlanReasoning(data, toolCalls, expectedColumns, excludedColumns) {
    const segments = [];
    if (toolCalls.length) {
        segments.push(`The planner prepared ${toolCalls.length} execution step${toolCalls.length === 1 ? '' : 's'}.`);
    }
    if (expectedColumns.length) {
        segments.push(`It expects ${expectedColumns.length} final output column${expectedColumns.length === 1 ? '' : 's'}.`);
    }
    if (excludedColumns.length) {
        segments.push(`Schema mapping already excluded ${excludedColumns.length} source column${excludedColumns.length === 1 ? '' : 's'}.`);
    }
    if (data.reason) {
        segments.push(data.reason);
    }
    return segments.join(' ') || 'The planner did not return a detailed written rationale for this plan.';
}

function setPlanActionLoading(action, isLoading) {
    document.querySelectorAll('.plan-action-btn').forEach(button => {
        if (isLoading) {
            button.disabled = true;
            if (button.dataset.planAction === action) {
                button.dataset.originalLabel = button.textContent;
                button.textContent = 'Working...';
            }
            return;
        }

        button.disabled = false;
        if (button.dataset.originalLabel) {
            button.textContent = button.dataset.originalLabel;
            delete button.dataset.originalLabel;
        }
    });
}

function renderCheckpointActions(checkpointId, availableActions) {
    const actions = Array.isArray(availableActions) ? availableActions : [];
    const labels = {
        approve: { label: 'Approve', className: 'approve' },
        modify: { label: 'Revise', className: 'secondary' },
        regenerate: { label: 'Regenerate', className: 'secondary' },
        resolve: { label: 'Resolve', className: 'approve' },
        investigate: { label: 'Investigate', className: 'secondary' },
        select_header_row: { label: 'Select Header', className: 'secondary' },
        retry_with_hints: { label: 'Retry', className: 'secondary' },
        retry: { label: 'Retry', className: 'secondary' },
        reject: { label: 'Reject', className: 'reject' },
        skip_sheet: { label: 'Skip', className: 'reject' },
        ignore: { label: 'Ignore', className: 'reject' },
        cancel: { label: 'Cancel Job', className: 'reject' },
        accept: { label: 'Accept', className: 'approve' },
        accept_as_is: { label: 'Accept As-Is', className: 'approve' },
        accept_with_note: { label: 'Accept', className: 'approve' },
        rollback: { label: 'Rollback', className: 'reject' },
        manual_correction: { label: 'Manual Fix', className: 'secondary' },
        fix_manually: { label: 'Manual Fix', className: 'secondary' },
        proceed: { label: 'Proceed', className: 'approve' },
        skip_destructive: { label: 'Skip Destructive', className: 'secondary' },
        skip_verification: { label: 'Skip Verification', className: 'secondary' },
        retry_with_different_tools: { label: 'Retry', className: 'secondary' },
    };

    const priorityOrder = [
        'approve', 'accept', 'accept_as_is', 'accept_with_note', 'proceed',
        'modify', 'resolve', 'regenerate', 'retry', 'retry_with_hints', 'retry_with_different_tools',
        'investigate', 'manual_correction', 'fix_manually', 'select_header_row',
        'skip_verification',
        'reject', 'skip_sheet', 'ignore', 'rollback', 'cancel', 'skip_destructive'
    ];
    const orderedActions = [
        ...priorityOrder.filter(action => actions.includes(action)),
        ...actions.filter(action => !priorityOrder.includes(action)),
    ];

    return `
        <div class="action-buttons">
            ${orderedActions.map(action => {
                const config = labels[action] || { label: toTitleCase(action), className: 'secondary' };
                const handler = action === 'select_header_row'
                    ? `submitStructuralReview('${checkpointId}', '${action}')`
                    : `resolveCheckpoint('${checkpointId}', '${action}')`;
                return `<button class="btn-action ${config.className}" onclick="event.stopPropagation(); ${handler}" title="${escapeHtml(config.label)}">${escapeHtml(config.label)}</button>`;
            }).join('')}
        </div>
    `;
}

function renderApprovalItem(item) {
    if (!item || typeof item !== 'object') {
        return `<li>${escapeHtml(String(item || 'Planner review item'))}</li>`;
    }

    const summary = item.summary || item.description || item.issue || item.reason || item.title || 'Planner review item';
    const owner = item.owner || item.review_owner || item.assignee;
    const target = item.target_column || item.column || item.field || item.target;
    const suffix = [target ? `Target: ${target}` : '', owner ? `Owner: ${owner}` : ''].filter(Boolean).join(' | ');

    return `<li>${escapeHtml(summary)}${suffix ? ` <span class="item-id">(${escapeHtml(suffix)})</span>` : ''}</li>`;
}

function renderRuleAction(item) {
    if (!item || typeof item !== 'object') {
        return `<li>${escapeHtml(String(item || 'Rule action'))}</li>`;
    }

    const target = item.target_column || item.column || 'unknown';
    const action = item.action || item.rule_type || 'apply';
    const value = item.value ?? item.rule_expression ?? '';
    const detail = value !== '' ? ` = ${String(value)}` : '';
    return `<li>${escapeHtml(`${target}: ${action}${detail}`)}</li>`;
}

function toTitleCase(value) {
    return String(value || '')
        .split('_')
        .map(part => part ? part[0].toUpperCase() + part.slice(1) : '')
        .join(' ');
}