/**
 * Bucketed context briefing inspector — curated LLM prompt vs archive ContextPacket.
 */
(function () {
    const BUCKET_ORDER = ['prompt', 'template', 'docs', 'tools', 'memory'];

    function escapeHtml(str) {
        if (typeof window.escapeHtml === 'function') return window.escapeHtml(str);
        return String(str ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function formatChars(n) {
        const v = Number(n) || 0;
        if (v >= 1000) return `${(v / 1000).toFixed(1)}k`;
        return String(v);
    }

    function findContextBriefingForEvent(eventId, eventMap) {
        const map = eventMap || window.eventMap || {};
        const event = map[eventId];
        if (!event) return null;

        function fromLlm(e) {
            const ic = e?.input_context;
            if (ic && ic.context_briefing) return ic.context_briefing;
            return null;
        }

        if (event.process_type === 'llm_call' && event.module === 'plan_generator') {
            return fromLlm(event);
        }

        const children = Object.values(map).filter((e) => e && e.parent_event_id === eventId);
        for (const child of children) {
            if (child.process_type === 'llm_call' && child.module === 'plan_generator') {
                const b = fromLlm(child);
                if (b) return b;
            }
        }

        if (/generate_plan|plan_generator/i.test(String(event.module || ''))) {
            for (const e of Object.values(map)) {
                if (e?.process_type === 'llm_call' && e.module === 'plan_generator') {
                    const b = fromLlm(e);
                    if (b) return b;
                }
            }
        }

        return null;
    }

    function sectionStatusBadge(section) {
        if (section.in_curated_prompt) {
            if (section.truncated) {
                return '<span class="ctx-badge ctx-badge--warn">truncated</span>';
            }
            if (section.delivery === 'system_prompt') {
                return '<span class="ctx-badge ctx-badge--system">system</span>';
            }
            return '<span class="ctx-badge ctx-badge--ok">sent</span>';
        }
        return '<span class="ctx-badge ctx-badge--muted">dropped</span>';
    }

    function cacheSectionBody(sectionId, body) {
        window.__contextBriefingBodyCache = window.__contextBriefingBodyCache || {};
        const key = `ctx-${sectionId}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
        window.__contextBriefingBodyCache[key] = body || '';
        return key;
    }

    function renderSectionRow(section) {
        const sid = escapeHtml(section.id || 'section');
        const title = escapeHtml(section.title || section.id || 'Section');
        const chars = formatChars(section.chars);
        const cacheKey = cacheSectionBody(sid, section.body || section.preview || '');
        const preview = escapeHtml(String(section.preview || '').slice(0, 220));
        return `
            <li class="ctx-section-row">
                <div class="ctx-section-row-head">
                    <button type="button" class="btn-link ctx-section-open" data-ctx-body-key="${cacheKey}" data-ctx-title="${title}">
                        ${title}
                    </button>
                    ${sectionStatusBadge(section)}
                    <span class="ctx-section-chars muted">${chars} chars</span>
                </div>
                <pre class="ctx-section-preview">${preview}${(section.preview || '').length > 220 ? '…' : ''}</pre>
            </li>`;
    }

    function renderArchiveItem(item) {
        const title = escapeHtml(item.title || item.id || 'Item');
        const cacheKey = cacheSectionBody(item.id || title, item.body || item.preview || '');
        const preview = escapeHtml(String(item.preview || '').slice(0, 200));
        return `
            <li class="ctx-section-row ctx-section-row--archive">
                <div class="ctx-section-row-head">
                    <button type="button" class="btn-link ctx-section-open" data-ctx-body-key="${cacheKey}" data-ctx-title="${title}">
                        ${title}
                    </button>
                    <span class="ctx-badge ctx-badge--archive">archive</span>
                    ${item.chars != null ? `<span class="ctx-section-chars muted">${formatChars(item.chars)} chars</span>` : ''}
                </div>
                <pre class="ctx-section-preview">${preview}${String(item.preview || '').length > 200 ? '…' : ''}</pre>
            </li>`;
    }

    function renderBucketBlock(bucketKey, bucket) {
        if (!bucket) return '';
        const label = escapeHtml(bucket.label || bucketKey);
        const sections = Array.isArray(bucket.sections) ? bucket.sections : [];
        const items = Array.isArray(bucket.items) ? bucket.items : [];
        const included = bucket.included_count != null ? bucket.included_count : sections.filter((s) => s.in_curated_prompt).length;
        const total = bucket.section_count != null ? bucket.section_count : sections.length || items.length;
        const body =
            sections.length > 0
                ? `<ul class="ctx-section-list">${sections.map(renderSectionRow).join('')}</ul>`
                : `<ul class="ctx-section-list">${items.map(renderArchiveItem).join('')}</ul>`;
        return `
            <details class="ctx-bucket" open>
                <summary class="ctx-bucket-summary">
                    <span class="ctx-bucket-label">${label}</span>
                    <span class="ctx-bucket-meta">${included}/${total} in prompt · ${formatChars(bucket.chars_included || 0)} chars</span>
                </summary>
                <div class="ctx-bucket-body">${body}</div>
            </details>`;
    }

    function renderBriefingSummary(briefing) {
        const dropped = Array.isArray(briefing.dropped_section_ids) ? briefing.dropped_section_ids.length : 0;
        const included = Array.isArray(briefing.included_section_ids) ? briefing.included_section_ids.length : 0;
        return `
            <div class="ctx-briefing-summary">
                <div class="ctx-briefing-stat">
                    <span class="ctx-briefing-stat-label">Curated user prompt</span>
                    <strong>${formatChars(briefing.curated_user_prompt_chars)} chars</strong>
                </div>
                <div class="ctx-briefing-stat">
                    <span class="ctx-briefing-stat-label">Sections sent</span>
                    <strong>${included}</strong>
                </div>
                <div class="ctx-briefing-stat">
                    <span class="ctx-briefing-stat-label">Dropped by budget</span>
                    <strong>${dropped}</strong>
                </div>
                <div class="ctx-briefing-stat">
                    <span class="ctx-briefing-stat-label">Tools catalog (system)</span>
                    <strong>${formatChars(briefing.tools_catalog_chars)} chars</strong>
                </div>
            </div>
            <p class="ctx-briefing-note muted">Curated view is what the plan_generator LLM receives. Archive below is the full ContextPacket kept for audit.</p>`;
    }

    function renderCuratedBuckets(briefing) {
        const buckets = briefing.buckets || {};
        const blocks = BUCKET_ORDER.map((k) => renderBucketBlock(k, buckets[k])).filter(Boolean);
        if (!blocks.length) {
            return '<p class="snapshot-panel-hint">No curated sections recorded.</p>';
        }
        return blocks.join('');
    }

    function renderArchiveBuckets(briefing) {
        const archive = briefing.archive || {};
        const note = archive.note ? `<p class="ctx-briefing-note muted">${escapeHtml(archive.note)}</p>` : '';
        const buckets = archive.buckets || {};
        const blocks = Object.keys(buckets)
            .sort((a, b) => BUCKET_ORDER.indexOf(a) - BUCKET_ORDER.indexOf(b))
            .map((k) => renderBucketBlock(k, buckets[k]))
            .filter(Boolean);
        if (!blocks.length) {
            return note + '<p class="snapshot-panel-hint">No archive context packet on this step.</p>';
        }
        return note + blocks.join('');
    }

    function renderContextBriefingPanel(briefing, opts = {}) {
        if (!briefing || typeof briefing !== 'object') {
            return opts.emptyHtml || '<p class="snapshot-panel-hint">No context briefing — re-run the job on a build with planner prompt curation enabled.</p>';
        }
        const mode = opts.mode || 'curated';
        const summary = renderBriefingSummary(briefing);
        const body = mode === 'archive' ? renderArchiveBuckets(briefing) : renderCuratedBuckets(briefing);
        return `
            <div class="ctx-briefing-panel" data-ctx-mode="${escapeHtml(mode)}">
                ${summary}
                <div class="ctx-briefing-mode-tabs" role="tablist">
                    <button type="button" class="ctx-briefing-mode-btn ${mode === 'curated' ? 'active' : ''}" data-ctx-mode="curated">Curated → LLM</button>
                    <button type="button" class="ctx-briefing-mode-btn ${mode === 'archive' ? 'active' : ''}" data-ctx-mode="archive">Archive (ContextPacket)</button>
                </div>
                <div class="ctx-briefing-buckets">${body}</div>
            </div>`;
    }

    function wireContextBriefingPanel(root, briefing, onModeChange) {
        if (!root || !briefing) return;
        root.querySelectorAll('.ctx-section-open').forEach((btn) => {
            btn.addEventListener('click', (ev) => {
                ev.preventDefault();
                const key = btn.getAttribute('data-ctx-body-key');
                const title = btn.getAttribute('data-ctx-title') || 'Context section';
                const cache = window.__contextBriefingBodyCache || {};
                const body = cache[key] || '';
                if (typeof window.openModal === 'function') {
                    window.openModal(title, body, false);
                }
            });
        });
        root.querySelectorAll('.ctx-briefing-mode-btn').forEach((btn) => {
            btn.addEventListener('click', () => {
                const mode = btn.getAttribute('data-ctx-mode') || 'curated';
                if (typeof onModeChange === 'function') {
                    onModeChange(mode);
                } else {
                    root.outerHTML = renderContextBriefingPanel(briefing, { mode });
                    wireContextBriefingPanel(root.parentElement.querySelector('.ctx-briefing-panel'), briefing);
                }
            });
        });
    }

    window.ContextBriefingUI = {
        BUCKET_ORDER,
        findContextBriefingForEvent,
        renderContextBriefingPanel,
        wireContextBriefingPanel,
    };
})();
