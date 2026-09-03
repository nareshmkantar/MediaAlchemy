document.addEventListener('DOMContentLoaded', () => {
    initUpload();
    loadRecentJobs();
});

const uploadState = {
    jobId: null,
    dataFiles: [],
    dataFileMeta: [],
    selectedSchemaFile: null,
    selectedSourceId: null,
    selectedSheet: null,
    hierarchyComplete: false,
    mixedGrainAck: false,
};

/** Last `sources` array passed to `renderSourceSelector`. */
let lastRenderedSources = [];

/** Hierarchy propose payload + working edits. */
let hierarchyCatalog = null;
let hierarchyState = {
    sources: [],
    comparison: null,
    kpis: null,
    dirty: false,
    enterpriseInfo: null,
};

/** Show compact “Jump to” dropdown when there are many sources (all pills still render). */
const SOURCE_DROPDOWN_THRESHOLD = 6;

let hierarchySaveTimer = null;

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

function fileBasename(name) {
    const s = String(name ?? '');
    const i = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    return i >= 0 ? s.slice(i + 1) : s;
}

function groupUploadSourcesByFile(sources) {
    const order = [];
    const map = new Map();
    for (const s of sources || []) {
        const fid = s.file_id || '_';
        if (!map.has(fid)) {
            map.set(fid, { file_id: fid, file_name: s.file_name || '', sources: [] });
            order.push(fid);
        }
        const g = map.get(fid);
        if (!g.file_name && s.file_name) g.file_name = s.file_name;
        g.sources.push(s);
    }
    return order.map((fid) => map.get(fid));
}

function initUploadSourcePanelDelegation() {
    const root = document.getElementById('sourceSelectorContainer');
    if (!root || root.dataset.delegationBound === '1') return;
    root.dataset.delegationBound = '1';
    root.addEventListener('click', async (e) => {
        const rmFile = e.target.closest('.upload-remove-file-btn');
        if (rmFile) {
            e.preventDefault();
            const fid = rmFile.dataset.fileId || '';
            if (!fid) return;
            const ok = window.confirm('Remove this entire workbook and all its sheets from the job?');
            if (!ok) return;
            await removeUploadDataFile(fid);
            return;
        }
        const rmSh = e.target.closest('.upload-remove-sheet-btn');
        if (rmSh) {
            e.preventDefault();
            const pill = rmSh.closest('.upload-sheet-pill');
            const sid = pill?.dataset?.sourceId || '';
            if (!sid) return;
            const ok = window.confirm('Remove this sheet from the job?');
            if (!ok) return;
            await removeUploadSource(sid);
            return;
        }
        const pill = e.target.closest('.upload-sheet-pill');
        if (pill && !e.target.closest('.upload-remove-sheet-btn')) {
            selectSource(pill, pill.dataset.sourceId || '', pill.dataset.sheetName || '');
        }
    });
}

async function removeUploadSource(sourceId) {
    if (!uploadState.jobId || !sourceId) return;
    try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(uploadState.jobId)}/sources/remove`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source_id: sourceId }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || res.statusText);
        uploadState.dataFileMeta = Array.isArray(data.data_files) ? data.data_files : uploadState.dataFileMeta;
        renderSourceSelector(data.sources || []);
        refreshUploadUxStepper();
        await loadHierarchyPropose(false);
        showToast('Sheet removed from job.', 'success');
    } catch (err) {
        showToast(err.message || 'Remove failed', 'error');
    }
}

async function removeUploadDataFile(fileId) {
    if (!uploadState.jobId || !fileId) return;
    try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(uploadState.jobId)}/data-files/remove`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file_id: fileId }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || res.statusText);
        uploadState.dataFileMeta = Array.isArray(data.data_files) ? data.data_files : uploadState.dataFileMeta;
        renderSourceSelector(data.sources || []);
        refreshUploadUxStepper();
        await loadHierarchyPropose(false);
        showToast('Workbook removed from job.', 'success');
    } catch (err) {
        showToast(err.message || 'Remove failed', 'error');
    }
}

function refreshUploadUxStepper() {
    /* Stepper removed; job progress is shown in Console / Results. */
}

function initUpload() {
    setupDataZone();
    initUploadSourcePanelDelegation();
    initHierarchyPanel();

    document.getElementById('mappingBtn')?.addEventListener('click', () => goToSetup());
    refreshUploadUxStepper();

    // Restore hierarchy panel when returning with ?job_id= or last job in localStorage
    const params = new URLSearchParams(window.location.search);
    const jobFromUrl = params.get('job_id') || getCurrentJob().jobId;
    if (jobFromUrl) {
        const sheetFromUrl = params.get('sheet_name') || getCurrentJob().sheetName;
        const sourceFromUrl = params.get('source_id') || getCurrentJob().sourceId;
        uploadState.jobId = jobFromUrl;
        setCurrentJob(jobFromUrl, sheetFromUrl || null, sourceFromUrl || null);
        // Keep address bar in sync so refresh / further nav keep the job
        try {
            const p = new URLSearchParams();
            p.set('job_id', jobFromUrl);
            if (sheetFromUrl) p.set('sheet_name', sheetFromUrl);
            if (sourceFromUrl) p.set('source_id', sourceFromUrl);
            window.history.replaceState({}, '', `${window.location.pathname}?${p.toString()}`);
        } catch (e) { /* ignore */ }
        getJobStatus(jobFromUrl)
            .then((job) => {
                uploadState.dataFileMeta = Array.isArray(job.data_files) ? job.data_files : [];
                const sources = (job.source_registry || []).map((s) => ({
                    source_id: s.source_id,
                    file_id: s.file_id,
                    file_name: s.file_name,
                    sheet_name: s.sheet_name,
                }));
                if (sources.length) {
                    document.getElementById('fileInfoData')?.classList.remove('hidden');
                    const dropZone = document.getElementById('dropZoneData');
                    if (dropZone) dropZone.style.display = 'none';
                    renderSourceSelector(sources);
                }
                return loadHierarchyPropose(false);
            })
            .catch(() => { /* ignore */ });
    }

    // Flush enterprise / hierarchy draft when leaving the page
    const flushUploadDraft = () => {
        if (!uploadState.jobId || !hierarchyState.dirty) return;
        try {
            const payload = {
                sources: hierarchyState.sources || [],
                mixed_grain_acknowledged: Boolean(uploadState.mixedGrainAck),
                mark_complete: false,
                enterprise_info: collectEnterpriseInfoPayload(),
            };
            const body = JSON.stringify(payload);
            if (navigator.sendBeacon) {
                const blob = new Blob([body], { type: 'application/json' });
                navigator.sendBeacon(`/api/hierarchy/register/${encodeURIComponent(uploadState.jobId)}`, blob);
            } else {
                void saveHierarchyRegister(false);
            }
        } catch (e) { /* ignore */ }
    };
    window.addEventListener('pagehide', flushUploadDraft);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') flushUploadDraft();
    });
}

function initHierarchyPanel() {
    document.getElementById('hierarchyRefreshBtn')?.addEventListener('click', () => {
        if (uploadState.jobId) loadHierarchyPropose(true);
    });
    document.getElementById('hierarchySaveBtn')?.addEventListener('click', () => saveHierarchyRegister(true));
    document.getElementById('hierarchyMixedAck')?.addEventListener('change', (e) => {
        uploadState.mixedGrainAck = Boolean(e.target.checked);
        hierarchyState.dirty = true;
        updateProcessButton();
        scheduleHierarchySave();
    });
    const entCard = document.getElementById('enterpriseInfoCard');
    if (entCard && entCard.dataset.bound !== '1') {
        entCard.dataset.bound = '1';
        entCard.addEventListener('change', onEnterpriseInfoChange);
        entCard.addEventListener('input', onEnterpriseInfoChange);
        entCard.addEventListener('click', onEnterpriseInfoClick);
    }
    const cards = document.getElementById('hierarchySourceCards');
    if (cards && cards.dataset.bound !== '1') {
        cards.dataset.bound = '1';
        cards.addEventListener('change', onHierarchyCardChange);
    }
}

async function ensureHierarchyCatalog() {
    if (hierarchyCatalog) return hierarchyCatalog;
    try {
        const data = await fetchJson('/api/hierarchy/catalog');
        hierarchyCatalog = data;
        return data;
    } catch (err) {
        console.warn('Hierarchy catalog load failed', err);
        hierarchyCatalog = { publishers: [], standard_fields: [], media_hierarchy: [], enterprise_info_defaults: null };
        return hierarchyCatalog;
    }
}

function showHierarchySection(show) {
    const section = document.getElementById('hierarchyRegisterSection');
    if (!section) return;
    section.classList.toggle('hidden', !show);
}

function defaultEnterpriseInfo() {
    const d = (hierarchyCatalog && hierarchyCatalog.enterprise_info_defaults) || {};
    return {
        fields: Array.isArray(d.fields) ? d.fields.map((f) => ({ ...f, options: normalizeOptionList(f.options) })) : [],
        values: { ...(d.values || {}) },
        enabled_optional_ids: Array.isArray(d.enabled_optional_ids) ? [...d.enabled_optional_ids] : [],
        additional_columns: Array.isArray(d.additional_columns)
            ? d.additional_columns.map((c) => ({ ...c }))
            : [],
        advertiser: { ...(d.advertiser || { mode: 'fixed', value: '', source_column: '', name: 'Advertiser' }) },
    };
}

function normalizeOptionList(raw) {
    const list = (raw || []).map((o) => {
        if (o && typeof o === 'object') {
            const standard = String(o.standard || o.label || o.name || o.value || '').trim();
            let aliases = [];
            if (Array.isArray(o.aliases)) {
                aliases = o.aliases.map((a) => String(a || '').trim()).filter(Boolean);
            } else {
                for (const key of ['value', 'code', 'label', 'name']) {
                    const v = String(o[key] || '').trim();
                    if (v && !aliases.includes(v)) aliases.push(v);
                }
            }
            if (!standard && !aliases.length) return null;
            const std = standard || aliases[0];
            if (!aliases.map((a) => a.toLowerCase()).includes(std.toLowerCase())) aliases = [std, ...aliases];
            return { standard: std, aliases, label: std, value: std };
        }
        const s = String(o || '').trim();
        return s ? { standard: s, aliases: [s], label: s, value: s } : null;
    }).filter(Boolean);
    list.sort((a, b) => String(a.standard || '').localeCompare(String(b.standard || ''), undefined, { sensitivity: 'base' }));
    return list;
}

function ensureEnterpriseInfoState(fromApi) {
    const catalogFields = (hierarchyCatalog?.enterprise_info_defaults?.fields) || [];
    const catalogAdditional = (hierarchyCatalog?.enterprise_info_defaults?.additional_columns) || [];
    const mergeOptions = (fields) => (fields || []).map((f) => {
        const cat = catalogFields.find((c) => c.id === f.id);
        const opts = normalizeOptionList(f.options);
        return {
            ...f,
            options: opts.length ? opts : normalizeOptionList(cat?.options),
        };
    });
    if (fromApi && typeof fromApi === 'object') {
        const base = defaultEnterpriseInfo();
        const incomingAdditional = Array.isArray(fromApi.additional_columns) ? fromApi.additional_columns : null;
        let additional = incomingAdditional
            ? catalogAdditional.map((tpl) => {
                const hit = incomingAdditional.find((c) => c.id === tpl.id) || {};
                return { ...tpl, ...hit, id: tpl.id, name: hit.name || tpl.name };
            })
            : base.additional_columns.map((c) => ({ ...c }));
        if (fromApi.advertiser && typeof fromApi.advertiser === 'object') {
            const adv = fromApi.advertiser;
            additional = additional.map((c) => {
                if (c.id !== 'advertiser') return c;
                const mode = adv.source_column ? 'column' : 'fixed';
                return {
                    ...c,
                    enabled: true,
                    mode,
                    value: adv.value || c.value || '',
                    source_column: adv.source_column || c.source_column || '',
                    name: adv.name || c.name,
                };
            });
        }
        hierarchyState.enterpriseInfo = {
            fields: mergeOptions(Array.isArray(fromApi.fields) ? fromApi.fields : base.fields),
            values: { ...(fromApi.values || {}) },
            enabled_optional_ids: Array.isArray(fromApi.enabled_optional_ids) && fromApi.enabled_optional_ids.length
                ? [...fromApi.enabled_optional_ids]
                : [...(base.enabled_optional_ids || [])],
            additional_columns: additional,
            advertiser: { ...(fromApi.advertiser || base.advertiser) },
        };
        const defaults = base.values || {};
        for (const [k, v] of Object.entries(defaults)) {
            if (!String(hierarchyState.enterpriseInfo.values[k] || '').trim() && v) {
                hierarchyState.enterpriseInfo.values[k] = v;
            }
        }
        return;
    }
    if (!hierarchyState.enterpriseInfo) {
        hierarchyState.enterpriseInfo = defaultEnterpriseInfo();
    }
}

async function loadHierarchyPropose(forceToast = false) {
    if (!uploadState.jobId) {
        showHierarchySection(false);
        uploadState.hierarchyComplete = false;
        updateProcessButton();
        return;
    }
    await ensureHierarchyCatalog();
    showHierarchySection(true);
    const cards = document.getElementById('hierarchySourceCards');
    if (cards) cards.innerHTML = '<p class="hierarchy-empty">Analyzing hierarchy…</p>';
    try {
        const data = await fetchJson(`/api/hierarchy/propose/${encodeURIComponent(uploadState.jobId)}`);
        hierarchyState.sources = Array.isArray(data.sources) ? data.sources : [];
        hierarchyState.comparison = data.comparison || null;
        hierarchyState.kpis = data.kpis || null;
        ensureEnterpriseInfoState(data.enterprise_info || hierarchyCatalog.enterprise_info_defaults);
        hierarchyState.dirty = false;
        uploadState.hierarchyComplete = Boolean(data.hierarchy_register_complete);
        uploadState.mixedGrainAck = Boolean(data.mixed_grain_acknowledged);
        const ack = document.getElementById('hierarchyMixedAck');
        if (ack) ack.checked = uploadState.mixedGrainAck;
        renderHierarchyPanel();
        updateProcessButton();
        if (forceToast) showToast('Registration analysis refreshed.', 'success');
    } catch (err) {
        if (cards) cards.innerHTML = `<p class="hierarchy-empty">Could not analyze hierarchy: ${escapeHtml(err.message || err)}</p>`;
        showToast(err.message || 'Hierarchy propose failed', 'error');
    }
}

function confPill(confidence, registered) {
    if (registered) return '<span class="hierarchy-pill ok">Registered</span>';
    const c = Number(confidence) || 0;
    if (c >= 85) return `<span class="hierarchy-pill ok">${c}%</span>`;
    if (c >= 60) return `<span class="hierarchy-pill warn">${c}%</span>`;
    if (c > 0) return `<span class="hierarchy-pill bad">${c}%</span>`;
    return '<span class="hierarchy-pill muted">Unknown</span>';
}

function publisherOptions(selectedId) {
    const pubs = (hierarchyCatalog && hierarchyCatalog.publishers) || [];
    const opts = [`<option value="">Unknown — select publisher</option>`];
    for (const p of pubs) {
        const sel = p.id === selectedId ? 'selected' : '';
        opts.push(`<option value="${escapeAttr(p.id)}" ${sel}>${escapeHtml(p.name)}</option>`);
    }
    opts.push(`<option value="custom" ${selectedId === 'custom' ? 'selected' : ''}>Custom…</option>`);
    return opts.join('');
}

function mediaHierarchyLevels() {
    const levels = (hierarchyCatalog && hierarchyCatalog.media_hierarchy) || [];
    return Array.isArray(levels) ? levels.filter((lv) => lv && lv.id) : [];
}

function grainSpineLabel() {
    const names = mediaHierarchyLevels().map((lv) => lv.name || lv.id).filter(Boolean);
    return names.length ? names.join(' → ') : 'Data grain';
}

function hierarchyLevelsForSource(src) {
    const shared = mediaHierarchyLevels();
    if (shared.length) return shared;
    if (Array.isArray(src.hierarchy) && src.hierarchy.length) return src.hierarchy;
    return [];
}

function fieldOptionsFor(fieldId) {
    const info = hierarchyState.enterpriseInfo;
    const field = (info?.fields || []).find((f) => f.id === fieldId)
        || ((hierarchyCatalog?.enterprise_info_defaults?.fields) || []).find((f) => f.id === fieldId);
    return normalizeOptionList(field?.options);
}

function optionMatches(opt, current) {
    const c = String(current || '').trim().toLowerCase();
    if (!c) return false;
    if (String(opt.standard || opt.label || '').trim().toLowerCase() === c) return true;
    return (opt.aliases || []).some((a) => String(a).trim().toLowerCase() === c);
}

function renderEnterpriseFieldControl(f, required) {
    const values = hierarchyState.enterpriseInfo?.values || {};
    const current = String(values[f.id] || '').trim();
    const options = fieldOptionsFor(f.id);
    const removeBtn = !required
        ? `<button type="button" class="btn-link enterprise-remove-opt" data-ent-action="remove-opt" data-field-id="${escapeAttr(f.id)}" title="Remove optional field">Remove</button>`
        : '';

    if (!options.length) {
        return `<div class="hierarchy-field enterprise-field" data-field-id="${escapeAttr(f.id)}">
            <label>${escapeHtml(f.name || f.id)}${required ? ' *' : ''}${removeBtn}</label>
            <input type="text" class="sia-input" data-ent-action="value" data-field-id="${escapeAttr(f.id)}"
                value="${escapeAttr(current)}" placeholder="${escapeAttr(f.name || f.id)}" ${required ? 'required' : ''}>
        </div>`;
    }

    const matched = options.find((o) => optionMatches(o, current));
    const useCustom = Boolean(current) && !matched;
    const selectVal = useCustom ? '__custom__' : (matched ? (matched.standard || matched.label) : '');
    const optsHtml = [
        `<option value="">— select —</option>`,
        ...options.map((o) => {
            const std = o.standard || o.label;
            const title = (o.aliases || []).length ? `title="${escapeAttr((o.aliases || []).join(', '))}"` : '';
            return `<option value="${escapeAttr(std)}" ${std === selectVal ? 'selected' : ''} ${title}>${escapeHtml(std)}</option>`;
        }),
        `<option value="__custom__" ${useCustom ? 'selected' : ''}>Other…</option>`,
    ].join('');

    return `<div class="hierarchy-field enterprise-field enterprise-field--combo" data-field-id="${escapeAttr(f.id)}">
        <label>${escapeHtml(f.name || f.id)}${required ? ' *' : ''}${removeBtn}</label>
        <select class="sia-select" data-ent-action="value-select" data-field-id="${escapeAttr(f.id)}" ${required && !current ? 'required' : ''}>
            ${optsHtml}
        </select>
        <input type="text" class="enterprise-custom-input sia-input ${useCustom ? '' : 'hidden'}"
            data-ent-action="value-custom" data-field-id="${escapeAttr(f.id)}"
            value="${escapeAttr(useCustom ? current : '')}"
            placeholder="Other ${escapeAttr(f.name || f.id)}">
    </div>`;
}

function renderAdditionalColumnsSection(info) {
    const cols = info.additional_columns || [];
    if (!cols.length) return '';
    const rows = cols.map((col, idx) => {
        const enabled = Boolean(col.enabled);
        return `<label class="enterprise-chip ${enabled ? 'is-enabled' : ''}" data-add-idx="${idx}">
            <input type="checkbox" data-ent-action="addcol-enable" data-add-idx="${idx}" ${enabled ? 'checked' : ''}>
            <span class="enterprise-chip-name">${escapeHtml(col.name || col.id)}</span>
            <input type="text" class="enterprise-chip-value" data-ent-action="addcol-value" data-add-idx="${idx}"
                value="${escapeAttr(col.value || col.default_value || '')}"
                placeholder="Default"
                ${enabled ? '' : 'disabled'}>
        </label>`;
    }).join('');
    return `
        <div class="enterprise-additional enterprise-additional--compact">
            <div class="enterprise-info-head">
                <h4>Constant columns</h4>
                <p>Optional fixed values for this job.</p>
            </div>
            <div class="enterprise-chip-list">${rows}</div>
        </div>`;
}

function renderEnterpriseInfoCard() {
    const el = document.getElementById('enterpriseInfoCard');
    if (!el) return;
    ensureEnterpriseInfoState(hierarchyState.enterpriseInfo);
    const info = hierarchyState.enterpriseInfo;
    const enabled = new Set(info.enabled_optional_ids || []);
    const fields = info.fields || [];
    const mandatory = fields.filter((f) => f.mandatory);
    const optional = fields.filter((f) => !f.mandatory);
    const addable = optional
        .filter((f) => !enabled.has(f.id))
        .slice()
        .sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id), undefined, { sensitivity: 'base' }));
    const shownOptional = optional.filter((f) => enabled.has(f.id));

    el.innerHTML = `
        <div class="enterprise-info-head">
            <h3>Enterprise info</h3>
            <p>Set once for the job. Lists from <a href="/pages/config.html">Config</a>.</p>
        </div>
        <div class="enterprise-fields-grid enterprise-fields-grid--compact">
            ${mandatory.map((f) => renderEnterpriseFieldControl(f, true)).join('')}
            ${shownOptional.map((f) => renderEnterpriseFieldControl(f, false)).join('')}
            ${addable.length ? `
            <div class="hierarchy-field enterprise-field enterprise-field--add">
                <label>Add field</label>
                <select class="sia-select" data-ent-action="add-opt">
                    <option value="">— select —</option>
                    ${addable.map((f) => `<option value="${escapeAttr(f.id)}">${escapeHtml(f.name || f.id)}</option>`).join('')}
                </select>
            </div>` : ''}
        </div>
        ${renderAdditionalColumnsSection(info)}
    `;
}

function syncAdvertiserFromAdditional(info) {
    const adv = (info.additional_columns || []).find((c) => c.id === 'advertiser');
    if (!adv) return;
    info.advertiser = {
        name: adv.name || 'Advertiser',
        mode: 'fixed',
        value: adv.enabled ? (adv.value || '') : '',
        source_column: '',
    };
}

function onEnterpriseInfoChange(e) {
    const el = e.target;
    const action = el.dataset?.entAction;
    if (!action) return;
    ensureEnterpriseInfoState(hierarchyState.enterpriseInfo);
    const info = hierarchyState.enterpriseInfo;
    if (action === 'value') {
        info.values[el.dataset.fieldId] = el.value;
    } else if (action === 'value-select') {
        const fid = el.dataset.fieldId;
        const wrap = el.closest('.enterprise-field');
        const custom = wrap?.querySelector('[data-ent-action="value-custom"]');
        if (el.value === '__custom__') {
            if (custom) {
                custom.classList.remove('hidden');
                custom.focus();
                info.values[fid] = custom.value || '';
            } else {
                info.values[fid] = '';
            }
        } else {
            if (custom) {
                custom.classList.add('hidden');
                custom.value = '';
            }
            info.values[fid] = el.value;
        }
    } else if (action === 'value-custom') {
        info.values[el.dataset.fieldId] = el.value;
        const wrap = el.closest('.enterprise-field');
        const sel = wrap?.querySelector('[data-ent-action="value-select"]');
        if (sel && sel.value !== '__custom__') sel.value = '__custom__';
    } else if (action === 'add-opt' && el.value) {
        if (!info.enabled_optional_ids.includes(el.value)) {
            info.enabled_optional_ids.push(el.value);
        }
        renderEnterpriseInfoCard();
    } else if (action === 'addcol-enable') {
        const idx = Number(el.dataset.addIdx);
        const col = info.additional_columns[idx];
        if (col) {
            col.enabled = el.checked;
            col.mode = 'fixed';
            col.source_column = '';
            if (col.enabled && !col.value && col.default_value) {
                col.value = col.default_value;
            }
            syncAdvertiserFromAdditional(info);
            renderEnterpriseInfoCard();
        }
    } else if (action === 'addcol-value') {
        const idx = Number(el.dataset.addIdx);
        const col = info.additional_columns[idx];
        if (col) {
            col.value = el.value;
            col.mode = 'fixed';
            col.source_column = '';
            syncAdvertiserFromAdditional(info);
        }
    } else {
        return;
    }
    hierarchyState.dirty = true;
    updateProcessButton();
    scheduleHierarchySave();
}

function onEnterpriseInfoClick(e) {
    const btn = e.target.closest('[data-ent-action="remove-opt"]');
    if (!btn) return;
    e.preventDefault();
    ensureEnterpriseInfoState(hierarchyState.enterpriseInfo);
    const id = btn.dataset.fieldId;
    hierarchyState.enterpriseInfo.enabled_optional_ids =
        (hierarchyState.enterpriseInfo.enabled_optional_ids || []).filter((x) => x !== id);
    renderEnterpriseInfoCard();
    hierarchyState.dirty = true;
    scheduleHierarchySave();
}

function renderHierarchyPanel() {
    const kpisEl = document.getElementById('hierarchyKpis');
    const banner = document.getElementById('hierarchyComparisonBanner');
    const cards = document.getElementById('hierarchySourceCards');
    const ackLabel = document.getElementById('hierarchyMixedAckLabel');
    if (!cards) return;

    renderEnterpriseInfoCard();

    const k = hierarchyState.kpis || {};
    if (kpisEl) {
        kpisEl.innerHTML = `
            <div class="hierarchy-kpi"><b>${k.files ?? 0}</b><span>Files</span></div>
            <div class="hierarchy-kpi"><b>${k.sources ?? 0}</b><span>Sheets</span></div>
            <div class="hierarchy-kpi"><b>${k.publishers_detected ?? 0}</b><span>Publishers</span></div>
            <div class="hierarchy-kpi"><b>${k.unregistered ?? 0}</b><span>Unregistered</span></div>
        `;
    }

    const cmp = hierarchyState.comparison || {};
    const warnings = Array.isArray(cmp.warnings) ? cmp.warnings : [];
    if (banner) {
        if (warnings.length) {
            banner.classList.remove('hidden');
            banner.classList.toggle('info', !cmp.mixed_grain);
            banner.innerHTML = warnings.map((w) => `<div>${escapeHtml(w.message || '')}</div>`).join('');
        } else {
            banner.classList.add('hidden');
            banner.innerHTML = '';
        }
    }
    if (ackLabel) {
        ackLabel.classList.toggle('hidden', !cmp.requires_mixed_grain_ack && !cmp.mixed_grain);
    }

    const sources = hierarchyState.sources || [];
    if (!sources.length) {
        cards.innerHTML = '<p class="hierarchy-empty">Upload data files to register media hierarchy.</p>';
        return;
    }

    const levels = mediaHierarchyLevels();
    cards.innerHTML = sources.map((src, idx) => {
        const grainId = src.grain_level_id || '';
        const detectedByLevel = {};
        (src.detected_hierarchy_columns || []).forEach((hit) => {
            const id = hit.target || '';
            if (!id) return;
            if (!detectedByLevel[id]) detectedByLevel[id] = [];
            if (hit.source_column) detectedByLevel[id].push(hit.source_column);
        });
        const tree = levels.map((lv) => {
            const checked = (lv.id === grainId) || (String(lv.level) === String(src.grain_level));
            const found = detectedByLevel[lv.id] || [];
            const foundHint = found.length
                ? `<span class="hierarchy-tree-found" title="${escapeAttr(found.join(', '))}">in file: ${escapeHtml(found[0])}${found.length > 1 ? ` +${found.length - 1}` : ''}</span>`
                : '';
            return `<label class="hierarchy-tree-item ${checked ? 'is-grain' : ''} ${found.length ? 'is-detected' : ''}">
                <input type="radio" name="grain-${escapeAttr(src.source_id)}" data-hier-action="grain"
                    data-source-idx="${idx}" value="${escapeAttr(lv.id)}" ${checked ? 'checked' : ''}>
                <span>${escapeHtml(lv.name)}${lv.mandatory ? '' : ' <span class="hierarchy-pill muted">optional</span>'}${foundHint}</span>
            </label>`;
        }).join('');

        const sheetLabel = src.sheet_name ? ` · ${src.sheet_name}` : '';
        return `<article class="hierarchy-source-card" data-source-idx="${idx}">
            <div class="hierarchy-source-card-head">
                <div>
                    <h3>${escapeHtml(fileBasename(src.file_name) || src.source_id)}${escapeHtml(sheetLabel)}</h3>
                    <div class="hierarchy-source-meta">${escapeHtml(src.publisher_reason || '')} · ${escapeHtml(src.grain_reason || '')}</div>
                </div>
                ${confPill(src.publisher_confidence, src.registered)}
            </div>
            <div class="hierarchy-card-grid">
                <div>
                    <div class="hierarchy-field">
                        <label>Publisher</label>
                        <select data-hier-action="publisher" data-source-idx="${idx}">
                            ${publisherOptions(src.publisher_id)}
                        </select>
                    </div>
                    <div class="hierarchy-tree">
                        <div class="hierarchy-tree-title">Data grain — ${escapeHtml(grainSpineLabel())}</div>
                        <p class="hierarchy-tree-hint">Same ladder for every publisher (from Config). Pick the deepest level this file actually reports at.</p>
                        ${tree}
                    </div>
                </div>
                ${renderHierarchyPreview(src, levels)}
            </div>
        </article>`;
    }).join('');
}

function renderHierarchyPreview(src, levels) {
    const preview = src.preview || {};
    const headers = Array.isArray(preview.headers) ? preview.headers : (src.columns || []);
    const rows = Array.isArray(preview.rows) ? preview.rows : [];
    const roles = preview.column_roles || {};
    const nameById = Object.fromEntries((levels || []).map((lv) => [lv.id, lv.name || lv.id]));
    if (!headers.length) {
        return `<div class="hierarchy-preview"><p class="hierarchy-preview-empty">No sample rows could be read from this file.</p></div>`;
    }
    const meta = preview.truncated
        ? `Showing ${headers.length} of ${preview.total_columns} columns`
        : `${headers.length} columns`;
    const thead = headers.map((h) => {
        const role = roles[h];
        const label = role ? nameById[role] || role : '';
        return `<th class="${role ? 'is-hierarchy' : ''}" title="${escapeAttr(h)}">
            ${escapeHtml(h)}${label ? `<span class="hierarchy-preview-role">${escapeHtml(label)}</span>` : ''}
        </th>`;
    }).join('');
    const tbody = rows.length
        ? rows.map((row) => `<tr>${headers.map((h, i) => {
            const role = roles[h];
            return `<td class="${role ? 'is-hierarchy' : ''}">${escapeHtml(row[i] == null ? '' : String(row[i]))}</td>`;
        }).join('')}</tr>`).join('')
        : `<tr><td colspan="${headers.length}" class="hierarchy-preview-empty">Headers only — no data rows in the sample.</td></tr>`;
    return `<div class="hierarchy-preview">
        <div class="hierarchy-preview-head">
            <strong>Sample from this file</strong>
            <span>${escapeHtml(meta)} · ${rows.length} row${rows.length === 1 ? '' : 's'}</span>
        </div>
        <div class="hierarchy-preview-scroll">
            <table class="hierarchy-preview-table">
                <thead><tr>${thead}</tr></thead>
                <tbody>${tbody}</tbody>
            </table>
        </div>
    </div>`;
}

function onHierarchyCardChange(e) {
    const el = e.target;
    const action = el.dataset.hierAction;
    if (!action) return;
    const idx = Number(el.dataset.sourceIdx);
    const src = hierarchyState.sources[idx];
    if (!src) return;

    if (action === 'publisher') {
        src.publisher_id = el.value || null;
        if (!src.publisher_id) {
            src.publisher_name = null;
            src.registered = false;
        } else if (src.publisher_id === 'custom') {
            src.publisher_name = 'Custom';
        } else {
            const pubs = (hierarchyCatalog && hierarchyCatalog.publishers) || [];
            const pub = pubs.find((p) => p.id === src.publisher_id);
            src.publisher_name = pub ? pub.name : src.publisher_id;
        }
        src.hierarchy = mediaHierarchyLevels();
        src.publisher_confidence = 99;
        src.publisher_reason = 'Analyst override';
        src.registered = Boolean(src.publisher_id && (src.grain_level_id || src.grain_level != null));
        hierarchyState.dirty = true;
        recomputeLocalComparison();
        renderHierarchyPanel();
        updateProcessButton();
        scheduleHierarchySave();
        return;
    }

    if (action === 'grain') {
        const levels = hierarchyLevelsForSource(src);
        const lv = levels.find((l) => l.id === el.value);
        if (lv) {
            src.grain_level_id = lv.id;
            src.grain_level = lv.level;
            src.grain_level_index = levels.indexOf(lv);
            src.grain_level_name = lv.name;
            src.grain_confidence = 99;
            src.grain_reason = 'Analyst override';
            src.hierarchy = levels;
            src.registered = Boolean(src.publisher_id);
            hierarchyState.dirty = true;
            recomputeLocalComparison();
            renderHierarchyPanel();
            updateProcessButton();
            scheduleHierarchySave();
        }
    }
}

function recomputeLocalComparison() {
    const grains = (hierarchyState.sources || []).map((s) => ({
        source_id: s.source_id,
        file_name: s.file_name,
        publisher_id: s.publisher_id,
        publisher_name: s.publisher_name,
        grain_level: s.grain_level,
        grain_level_id: s.grain_level_id,
        grain_level_name: s.grain_level_name,
    }));
    const ids = new Set(grains.map((g) => g.grain_level_id).filter(Boolean));
    const mixed = ids.size > 1;
    const warnings = [];
    if (mixed) {
        warnings.push({
            code: 'mixed_grain',
            severity: 'warning',
            message: grains.map((g) => {
                const label = fileBasename(g.file_name) || g.source_id;
                return `${label} (${g.publisher_name || 'Unknown'}) is ${g.grain_level_name || 'unset'}`;
            }).join('; ') + '. Stacking without rollup can double-count.',
        });
    }
    hierarchyState.comparison = {
        ...(hierarchyState.comparison || {}),
        mixed_grain: mixed,
        requires_mixed_grain_ack: mixed,
        warnings,
        grains,
    };
    const unregistered = (hierarchyState.sources || []).filter((s) => !s.publisher_id || (s.grain_level_id == null && s.grain_level == null)).length;
    hierarchyState.kpis = {
        ...(hierarchyState.kpis || {}),
        unregistered,
        publishers_detected: new Set((hierarchyState.sources || []).map((s) => s.publisher_id).filter(Boolean)).size,
        mixed_grain: mixed,
    };
}

function scheduleHierarchySave() {
    if (hierarchySaveTimer) clearTimeout(hierarchySaveTimer);
    hierarchySaveTimer = setTimeout(() => saveHierarchyRegister(false), 700);
}

function collectEnterpriseInfoPayload() {
    ensureEnterpriseInfoState(hierarchyState.enterpriseInfo);
    const info = hierarchyState.enterpriseInfo;
    syncAdvertiserFromAdditional(info);
    return {
        values: { ...(info.values || {}) },
        enabled_optional_ids: [...(info.enabled_optional_ids || [])],
        additional_columns: (info.additional_columns || []).map((c) => ({
            id: c.id,
            name: c.name,
            enabled: Boolean(c.enabled),
            mode: 'fixed',
            value: c.value || '',
            source_column: '',
        })),
        advertiser: {
            value: info.advertiser?.value || '',
            source_column: '',
            name: info.advertiser?.name || 'Advertiser',
            mode: 'fixed',
        },
    };
}

function enterpriseInfoReady() {
    ensureEnterpriseInfoState(hierarchyState.enterpriseInfo);
    const info = hierarchyState.enterpriseInfo;
    const values = info.values || {};
    const mandatory = (info.fields || []).filter((f) => f.mandatory);
    return mandatory.every((f) => String(values[f.id] || '').trim());
}

async function saveHierarchyRegister(showSuccessToast) {
    if (!uploadState.jobId) return false;
    const payload = {
        sources: hierarchyState.sources || [],
        enterprise_info: collectEnterpriseInfoPayload(),
        mixed_grain_acknowledged: Boolean(uploadState.mixedGrainAck),
        mark_complete: true,
    };
    try {
        const res = await fetch(`/api/hierarchy/register/${encodeURIComponent(uploadState.jobId)}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            uploadState.hierarchyComplete = false;
            updateProcessButton();
            if (showSuccessToast) {
                showToast((data.reasons || []).join('; ') || data.error || 'Registration incomplete', 'warning');
            }
            if (res.status === 400) {
                const soft = await fetch(`/api/hierarchy/register/${encodeURIComponent(uploadState.jobId)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ ...payload, mark_complete: false }),
                });
                const softData = await soft.json().catch(() => ({}));
                if (soft.ok) {
                    hierarchyState.dirty = false;
                    uploadState.hierarchyComplete = Boolean(softData.hierarchy_register_complete);
                    if (softData.comparison) hierarchyState.comparison = softData.comparison;
                    if (softData.enterprise_info) ensureEnterpriseInfoState(softData.enterprise_info);
                }
            }
            updateProcessButton();
            return false;
        }
        hierarchyState.dirty = false;
        uploadState.hierarchyComplete = Boolean(data.hierarchy_register_complete);
        if (data.comparison) hierarchyState.comparison = data.comparison;
        if (data.enterprise_info) ensureEnterpriseInfoState(data.enterprise_info);
        updateProcessButton();
        if (showSuccessToast) {
            showToast(
                uploadState.hierarchyComplete
                    ? 'Registration saved. You can start Guided Setup.'
                    : 'Registration saved (still incomplete).',
                uploadState.hierarchyComplete ? 'success' : 'warning',
            );
        }
        return uploadState.hierarchyComplete;
    } catch (err) {
        showToast(err.message || 'Failed to save registration', 'error');
        return false;
    }
}

function hierarchyReadyForSetup() {
    const sources = hierarchyState.sources || [];
    if (!sources.length && uploadState.jobId) return uploadState.hierarchyComplete;
    if (!enterpriseInfoReady()) return false;
    const allRegistered = sources.every((s) => s.publisher_id && (s.grain_level_id || s.grain_level != null));
    const mixed = Boolean(hierarchyState.comparison && hierarchyState.comparison.mixed_grain);
    if (mixed && !uploadState.mixedGrainAck) return false;
    return allRegistered;
}

function setupDataZone() {
    const dropZone = document.getElementById('dropZoneData');
    const fileInput = document.getElementById('fileInputData');

    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', async (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        await handleDataFiles(Array.from(e.dataTransfer.files || []));
    });
    fileInput.addEventListener('change', async (e) => {
        await handleDataFiles(Array.from(e.target.files || []));
        fileInput.value = '';
    });
}

function setupSchemaZone() {
    const dropZone = document.getElementById('dropZoneSchema');
    const fileInput = document.getElementById('fileInputSchema');
    const fileInfo = document.getElementById('fileInfoSchema');
    const removeFile = document.getElementById('removeFileSchema');

    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', async (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        const [file] = Array.from(e.dataTransfer.files || []);
        if (file) await handleSchemaFile(file);
    });
    fileInput.addEventListener('change', async (e) => {
        const [file] = Array.from(e.target.files || []);
        if (file) await handleSchemaFile(file);
    });
    removeFile?.addEventListener('click', (e) => {
        e.stopPropagation();
        clearSchemaSelection();
    });
}

async function handleDataFiles(files) {
    const validFiles = files.filter(file => ['xlsx', 'xls', 'csv'].includes(file.name.split('.').pop().toLowerCase()));
    if (!validFiles.length) {
        showToast('Please upload Excel or CSV files.', 'warning');
        return;
    }

    const fileInfo = document.getElementById('fileInfoData');
    const dropZone = document.getElementById('dropZoneData');
    const sourceList = document.getElementById('sheetTabList');
    const sourceContainer = document.getElementById('sourceSelectorContainer');
    if (sourceContainer) sourceContainer.classList.remove('hidden');
    if (sourceList) sourceList.innerHTML = '<p style="font-size: 11px; color: var(--text-secondary);">Uploading files...</p>';

    try {
        for (const file of validFiles) {
            const payload = new FormData();
            payload.append('file', file);
            payload.append('type', 'data');
            if (uploadState.jobId) payload.append('job_id', uploadState.jobId);

            const response = await fetch('/api/upload', { method: 'POST', body: payload });
            if (!response.ok) {
                let msg = `Failed to upload ${file.name}`;
                try {
                    const err = await response.json();
                    if (err.error) msg = err.error;
                } catch (_) {
                    /* ignore non-JSON body */
                }
                showToast(msg, 'error');
                throw new Error(msg);
            }
            const data = await response.json();
            uploadState.jobId = data.job_id;
            uploadState.dataFiles.push(file);
            uploadState.dataFileMeta = Array.isArray(data.data_files) ? data.data_files : uploadState.dataFileMeta;
            renderSourceSelector(data.sources || []);
        }

        window.lastUploadedJobId = uploadState.jobId;
        fileInfo?.classList.remove('hidden');
        dropZone.style.display = 'none';
        updateProcessButton();
        refreshUploadUxStepper();
        await loadHierarchyPropose(false);
    } catch (e) {
        console.error(e);
        if (sourceList) {
            sourceList.innerHTML = '<p style="font-size: 11px; color: var(--text-secondary);">Upload stopped — fix the issue and try again.</p>';
        }
    }
}

function clearSchemaSelection() {
    uploadState.selectedSchemaFile = null;
    const fileInfo = document.getElementById('fileInfoSchema');
    const dropZone = document.getElementById('dropZoneSchema');
    const fileInput = document.getElementById('fileInputSchema');
    if (fileInput) fileInput.value = '';
    if (fileInfo) fileInfo.classList.add('hidden');
    if (dropZone) dropZone.style.display = 'block';
    updateProcessButton();
}

async function uploadSchemaFile(jobId, file) {
    const schemaForm = new FormData();
    schemaForm.append('file', file);
    schemaForm.append('type', 'schema');
    schemaForm.append('job_id', jobId);
    const schemaRes = await fetch('/api/upload', { method: 'POST', body: schemaForm });
    let data = {};
    try {
        data = await schemaRes.json();
    } catch (_) {
        /* ignore non-JSON body */
    }
    if (!schemaRes.ok) {
        const detail = data.template_validation_error || data.error || `Schema upload failed (${schemaRes.status})`;
        throw new Error(detail);
    }
    return data;
}

async function validateAndUploadSchema(file) {
    if (!uploadState.jobId || !file) return false;
    try {
        await uploadSchemaFile(uploadState.jobId, file);
        return true;
    } catch (e) {
        clearSchemaSelection();
        showToast(`Template rejected: ${e.message}`, 'error');
        return false;
    }
}

async function handleSchemaFile(file) {
    if (!file.name.toLowerCase().endsWith('.json')) {
        showToast('Please upload a .json target scope template.', 'warning');
        return;
    }
    if (uploadState.jobId) {
        const ok = await validateAndUploadSchema(file);
        if (!ok) return;
    }
    uploadState.selectedSchemaFile = file;
    document.getElementById('fileNameSchema').textContent = file.name;
    document.getElementById('fileSizeSchema').textContent = formatFileSize(file.size);
    document.getElementById('fileInfoSchema').classList.remove('hidden');
    document.getElementById('dropZoneSchema').style.display = 'none';
    updateProcessButton();
    if (uploadState.jobId) {
        showToast('Target template validated and attached.', 'success');
    }
}

function renderSourceSelector(sources) {
    const sheetContainer = document.getElementById('sourceSelectorContainer');
    const sheetTabList = document.getElementById('sheetTabList');
    const sheetDropdownContainer = document.getElementById('sheetDropdownContainer');
    const sheetDropdown = document.getElementById('sheetDropdown');
    const sheetInput = document.getElementById('sheetSelect');
    const sourceInput = document.getElementById('sourceSelect');
    if (!sheetContainer || !sheetTabList || !sheetDropdown || !sheetDropdownContainer) return;

    lastRenderedSources = Array.isArray(sources) ? sources : [];
    sheetContainer.classList.remove('hidden');

    if (!lastRenderedSources.length) {
        sheetTabList.innerHTML = '<p class="upload-sources-empty">No sheets in this job. Upload data files above.</p>';
        sheetDropdownContainer.classList.add('hidden');
        uploadState.selectedSourceId = null;
        uploadState.selectedSheet = null;
        if (sheetInput) sheetInput.value = '';
        if (sourceInput) sourceInput.value = '';
        const fileInfo = document.getElementById('fileInfoData');
        const dropZone = document.getElementById('dropZoneData');
        if (uploadState.jobId && !(uploadState.dataFileMeta || []).length) {
            if (fileInfo) fileInfo.classList.add('hidden');
            if (dropZone) dropZone.style.display = 'block';
        }
        updateProcessButton();
        refreshUploadUxStepper();
        return;
    }

    const groups = groupUploadSourcesByFile(lastRenderedSources);
    const parts = [];
    for (const g of groups) {
        const title = g.file_name || '';
        const base = fileBasename(g.file_name);
        const fid = escapeAttr(g.file_id);
        parts.push(`<section class="upload-file-block" data-file-id="${fid}">
            <header class="upload-file-block-head">
                <span class="upload-file-block-title" title="${escapeAttr(title)}">${escapeHtml(base)}</span>
                <button type="button" class="btn-icon upload-remove-file-btn" data-file-id="${fid}" title="Remove entire workbook">✕</button>
            </header>
            <div class="upload-sheet-pill-row">`);
        for (const source of g.sources) {
            const sn = source.sheet_name != null ? String(source.sheet_name) : '';
            const isActive = source.source_id === uploadState.selectedSourceId;
            const sid = escapeAttr(source.source_id);
            parts.push(`
                <div class="upload-sheet-pill ${isActive ? 'active' : ''}" data-source-id="${sid}" data-sheet-name="${escapeAttr(sn)}">
                    <span class="upload-sheet-pill-label">${escapeHtml(sn || source.source_id || 'Sheet')}</span>
                    <button type="button" class="upload-remove-sheet-btn" title="Remove this sheet" aria-label="Remove sheet">×</button>
                </div>`);
        }
        parts.push('</div></section>');
    }
    sheetTabList.innerHTML = parts.join('');

    if (lastRenderedSources.length > SOURCE_DROPDOWN_THRESHOLD) {
        sheetDropdownContainer.classList.remove('hidden');
        sheetDropdown.innerHTML = lastRenderedSources.map((source) => {
            const label = source.sheet_name ? `${fileBasename(source.file_name)} — ${source.sheet_name}` : fileBasename(source.file_name);
            return `<option value="${escapeAttr(source.source_id)}" data-sheet-name="${escapeAttr(source.sheet_name || '')}">${escapeHtml(label)}</option>`;
        }).join('');
        sheetDropdown.onchange = () => {
            const selected = sheetDropdown.value;
            const option = sheetDropdown.selectedOptions[0];
            const sheetName = option?.dataset.sheetName || '';
            const matchingTab = Array.from(sheetTabList.querySelectorAll('.upload-sheet-pill'))
                .find((tab) => tab.dataset.sourceId === selected);
            selectSource(matchingTab || null, selected, sheetName);
        };
    } else {
        sheetDropdownContainer.classList.add('hidden');
    }

    let pick = lastRenderedSources.find((s) => s.source_id === uploadState.selectedSourceId) || lastRenderedSources[0];
    if (sheetInput) sheetInput.value = pick.sheet_name || '';
    if (sourceInput) sourceInput.value = pick.source_id;
    uploadState.selectedSheet = pick.sheet_name || '';
    uploadState.selectedSourceId = pick.source_id;
    const firstTab = Array.from(sheetTabList.querySelectorAll('.upload-sheet-pill'))
        .find((t) => t.dataset.sourceId === pick.source_id)
        || sheetTabList.querySelector('.upload-sheet-pill');
    document.querySelectorAll('.upload-sheet-pill').forEach((t) => t.classList.remove('active'));
    if (firstTab) firstTab.classList.add('active');
    if (sheetDropdown && sheetDropdown.options.length) {
        sheetDropdown.value = pick.source_id;
    }
    updateProcessButton();
}

window.selectSource = function (element, sourceId, sheetName) {
    document.querySelectorAll('.upload-sheet-pill').forEach((t) => t.classList.remove('active'));
    if (element) element.classList.add('active');

    uploadState.selectedSourceId = sourceId;
    uploadState.selectedSheet = sheetName || '';

    const sheetInput = document.getElementById('sheetSelect');
    const sourceInput = document.getElementById('sourceSelect');
    if (sheetInput) sheetInput.value = sheetName || '';
    if (sourceInput) sourceInput.value = sourceId || '';
    const dropdown = document.getElementById('sheetDropdown');
    if (dropdown) dropdown.value = sourceId || '';

    if (uploadState.jobId) {
        setCurrentJob(uploadState.jobId, sheetName || null, sourceId || null);
    }
};

function updateProcessButton() {
    const hasDataSource = Boolean(uploadState.jobId && uploadState.selectedSourceId);
    const hierarchyOk = hierarchyReadyForSetup() || uploadState.hierarchyComplete;
    const canContinue = hasDataSource && hierarchyOk;
    const mappingBtn = document.getElementById('mappingBtn');
    if (mappingBtn) {
        mappingBtn.disabled = !canContinue;
        if (!hasDataSource) {
            mappingBtn.title = 'Upload at least one data file first.';
        } else if (!hierarchyOk) {
            mappingBtn.title = 'Complete Enterprise Info (Market/Category/Brand) and publisher + grain for each sheet.';
        } else {
            mappingBtn.title = 'Open Guided Setup';
        }
    }
}

async function goToSetup() {
    if (!uploadState.jobId || !uploadState.selectedSourceId) {
        showToast('Upload at least one data file first.', 'warning');
        return;
    }
    const mappingBtn = document.getElementById('mappingBtn');
    mappingBtn.disabled = true;
    mappingBtn.innerHTML = '<span class="spinner"></span> Preparing Setup...';

    try {
        const saved = await saveHierarchyRegister(false);
        if (!saved && !hierarchyReadyForSetup()) {
            showToast('Complete Enterprise Info and publisher + grain for every sheet before Guided Setup.', 'warning');
            mappingBtn.disabled = false;
            mappingBtn.innerHTML = '<span>🧭</span> Start Guided Setup';
            updateProcessButton();
            return;
        }
        if ((hierarchyState.comparison || {}).mixed_grain && !uploadState.mixedGrainAck) {
            showToast('Acknowledge mixed hierarchy grain before continuing.', 'warning');
            mappingBtn.disabled = false;
            mappingBtn.innerHTML = '<span>🧭</span> Start Guided Setup';
            updateProcessButton();
            return;
        }
        setCurrentJob(uploadState.jobId, uploadState.selectedSheet || null, uploadState.selectedSourceId || null);
        showToast('Guided setup workspace ready.', 'success');
        const sourceQuery = uploadState.selectedSourceId ? `&source_id=${encodeURIComponent(uploadState.selectedSourceId)}` : '';
        const sheetQuery = uploadState.selectedSheet ? `&sheet_name=${encodeURIComponent(uploadState.selectedSheet)}` : '';
        window.location.href = `setup.html?job_id=${uploadState.jobId}${sheetQuery}${sourceQuery}`;
    } catch (e) {
        showToast(`Error: ${e.message}`, 'error');
        mappingBtn.disabled = false;
        mappingBtn.innerHTML = '<span>🧭</span> Start Guided Setup';
        updateProcessButton();
    }
}

async function loadRecentJobs() {
    try {
        const payload = await fetchJson('/api/jobs');
        const jobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
        const list = document.getElementById('jobsList');
        const banner = document.getElementById('jobsRetentionBanner');

        if (!list) return;

        if (banner) {
            if (payload?.retention_warning && payload?.retention_message) {
                banner.hidden = false;
                banner.textContent = payload.retention_message;
            } else {
                banner.hidden = true;
                banner.textContent = '';
            }
        }

        if (jobs.length === 0) {
            list.innerHTML = '<p class="empty-state">No recent jobs</p>';
            return;
        }

        const total = payload?.total_count ?? jobs.length;
        const header = total > 5
            ? `<p class="jobs-list-meta">${total} job(s) on server — showing 5 most recent. Delete finished jobs to free memory.</p>`
            : '';

        list.innerHTML = header + jobs.slice(0, 5).map(job => `
            <div class="job-item ${job.status}" onclick="viewJob('${job.job_id}')">
                <span class="job-id">${job.job_id}</span>
                <span class="job-file">${job.filename}</span>
                <span class="job-status">${job.status}</span>
                <button class="job-delete-btn" title="Delete job" onclick="deleteJob(event, '${job.job_id}')">Delete</button>
            </div>
        `).join('');

    } catch (e) {
        console.error('Failed to load jobs:', e);
    }
}

function viewJob(jobId) {
    fetchJson(`/api/status/${jobId}`)
        .then(job => {
            const primarySource = (job.source_registry || [])[0] || {};
            setCurrentJob(jobId, primarySource.sheet_name || job.sheet_name || getCurrentJob().sheetName, primarySource.source_id || getCurrentJob().sourceId);
            routeJobByState(job);
        })
        .catch(() => {
            setCurrentJob(jobId);
            window.location.href = '/pages/debug.html';
        });
}

async function deleteJob(event, jobId) {
    event.stopPropagation();
    const confirmed = window.confirm(`Delete job ${jobId}? This removes its saved setup, review state, and generated files.`);
    if (!confirmed) {
        return;
    }

    try {
        const result = await fetchJson(`/api/jobs/${jobId}`, { method: 'DELETE' });
        const current = getCurrentJob();
        if (current?.jobId === jobId) {
            localStorage.removeItem('currentJobId');
            localStorage.removeItem('currentSheet');
            localStorage.removeItem('currentSourceId');
        }
        showToast(result.message || 'Job deleted', 'success');
        loadRecentJobs();
        if (typeof updateNavStatus === 'function') {
            updateNavStatus();
        }
    } catch (e) {
        showToast(`Delete failed: ${e.message}`, 'error');
    }
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}