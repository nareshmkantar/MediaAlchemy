/**
 * Config module — edit media_hierarchies.json via /api/hierarchy/catalog.
 * Grouped settings: Master list + Upload / Output schema / Matching.
 */
const CONFIG_TABS = [
    'overview', 'mapping', 'values',
];
const FIELD_VALUE_CLOSED = new Set([
    'market', 'country', 'category', 'brand', 'advertiser', 'business_unit', 'region',
    'sub_category_1', 'sub_category_2', 'sub_brand', 'channel', 'device',
    'campaign_objective', 'campaign_type', 'buying_model', 'campaign_kpi',
    'publisher', 'creative_format', 'creative_size',
]);
const FIELD_VALUE_OPEN = new Set([
    'campaign', 'ad_group', 'ad', 'creative', 'audience', 'inventory', 'creative_variant',
]);
const FIELD_VALUE_NONE = new Set([
    'date', 'spends', 'impressions', 'clicks', 'video_views',
]);
const FIELD_VALUE_CAP = 100;
const STARTER_VALUE_LISTS = {
    campaign_objective: [
        { standard: 'Awareness', aliases: ['Awareness', 'AWAR', 'Brand'] },
        { standard: 'Consideration', aliases: ['Consideration', 'Consider', 'Traffic'] },
        { standard: 'Conversion', aliases: ['Conversion', 'Conver', 'Conv', 'Perf', 'Performance'] },
    ],
    buying_model: [
        { standard: 'Auction', aliases: ['Auction', 'RTB', 'Real-time bidding'] },
        { standard: 'Reservation', aliases: ['Reservation', 'Reserved'] },
        { standard: 'Fixed Price', aliases: ['Fixed', 'Fixed Price'] },
        { standard: 'Programmatic Guaranteed', aliases: ['PG', 'Programmatic Guaranteed'] },
    ],
    campaign_kpi: [
        { standard: 'ROAS', aliases: ['ROAS', 'Return on ad spend'] },
        { standard: 'CTR', aliases: ['CTR', 'Click-through rate'] },
        { standard: 'CPI', aliases: ['CPI', 'Cost per install'] },
        { standard: 'CPA', aliases: ['CPA', 'Cost per acquisition', 'Cost per action'] },
        { standard: 'CPC', aliases: ['CPC', 'Cost per click'] },
        { standard: 'CPM', aliases: ['CPM', 'Cost per mille', 'Cost per thousand'] },
        { standard: 'VTR', aliases: ['VTR', 'View-through rate'] },
        { standard: 'CPV', aliases: ['CPV', 'Cost per view'] },
    ],
    campaign_type: [
        { standard: 'Prospecting', aliases: ['Prospecting', 'Prospect'] },
        { standard: 'Retargeting', aliases: ['Retargeting', 'Remarketing'] },
        { standard: 'Always on', aliases: ['Always on', 'AO', 'Always-on'] },
    ],
    device: [
        { standard: 'Desktop', aliases: ['Desktop', 'DT', 'PC'] },
        { standard: 'Mobile', aliases: ['Mobile', 'MW', 'Phone'] },
        { standard: 'Tablet', aliases: ['Tablet', 'Tab'] },
        { standard: 'CTV', aliases: ['CTV', 'Connected TV', 'OTT'] },
    ],
    channel: [
        { standard: 'digital', aliases: ['digital', 'Digital', 'Online'] },
        { standard: 'TV', aliases: ['TV', 'Television'] },
        { standard: 'radio', aliases: ['radio', 'Radio'] },
        { standard: 'out of home', aliases: ['OOH', 'out of home', 'Out of Home'] },
        { standard: 'print', aliases: ['print', 'Print'] },
    ],
};
let configState = null;
let learnedState = null;
let configTab = 'overview';
let configDirty = false;
let configMasterQuery = '';
let configValueField = 'country';
let configValueQuery = '';

document.addEventListener('DOMContentLoaded', () => {
    const hash = String(location.hash || '').replace(/^#/, '');
    if (CONFIG_TABS.includes(hash)) configTab = hash;
    document.querySelectorAll('.config-side-link').forEach((btn) => {
        btn.addEventListener('click', () => setConfigTab(btn.dataset.configTab || 'overview'));
    });
    document.getElementById('configReloadBtn')?.addEventListener('click', () => loadConfigCatalog(true));
    document.getElementById('configSaveBtn')?.addEventListener('click', saveConfigCatalog);
    window.addEventListener('hashchange', () => {
        const t = String(location.hash || '').replace(/^#/, '');
        if (CONFIG_TABS.includes(t) && t !== configTab) setConfigTab(t, { skipHash: true });
    });
    window.addEventListener('beforeunload', (e) => {
        if (!configDirty) return;
        e.preventDefault();
        e.returnValue = '';
    });
    const body = document.getElementById('configBody');
    if (body && body.dataset.bound !== '1') {
        body.dataset.bound = '1';
        body.addEventListener('input', onConfigInput);
        body.addEventListener('change', onConfigInput);
        body.addEventListener('click', onConfigClick);
        body.addEventListener('keydown', onConfigKeydown);
    }
    loadConfigCatalog(false);
});

function setConfigTab(tab, { skipHash = false } = {}) {
    const next = CONFIG_TABS.includes(tab) ? tab : 'overview';
    configTab = next;
    document.querySelectorAll('.config-side-link').forEach((b) => {
        b.classList.toggle('active', b.dataset.configTab === next);
    });
    if (!skipHash) {
        const want = `#${next}`;
        if (location.hash !== want) history.replaceState(null, '', want);
    }
    renderConfigBody();
}

function markConfigDirty() {
    configDirty = true;
    updateSaveChrome();
}

function updateSaveChrome() {
    const badge = document.getElementById('configDirtyBadge');
    if (badge) badge.classList.toggle('hidden', !configDirty);
    document.querySelector('.config-page')?.classList.toggle('is-dirty', configDirty);
}

function renderWherePills(items) {
    return `<div class="cfg-where" aria-label="Used in">
        <span class="cfg-where-label">Used in</span>
        ${(items || []).map((t) => `<span class="cfg-pill">${escapeHtml(t)}</span>`).join('')}
    </div>`;
}

function fieldValueMode(fieldId, kinds) {
    const id = String(fieldId || '');
    const kindBlob = (kinds || []).join(' ').toLowerCase();
    if (FIELD_VALUE_NONE.has(id) || kindBlob.includes('metric') || kindBlob === 'date' || id === 'date') {
        return 'none';
    }
    if (FIELD_VALUE_OPEN.has(id)) return 'open';
    if (FIELD_VALUE_CLOSED.has(id)) return 'closed';
    if (kindBlob.includes('metric')) return 'none';
    return 'closed';
}

function mappingModeMeta(id, kinds) {
    const mode = fieldValueMode(id, kinds);
    if (mode === 'none') {
        return { mode, label: 'Skip value list', hint: 'Date / metric — map the column name only', tab: id && FIELD_VALUE_NONE.has(id) && !['date'].includes(id) ? 'metrics' : 'mapping' };
    }
    if (mode === 'open') {
        return { mode, label: 'Pass-through', hint: `Do not pre-map IDs. Optional ≤${FIELD_VALUE_CAP} names`, tab: 'values' };
    }
    return { mode, label: 'Value synonyms', hint: 'Finite list: DEU → Germany', tab: 'values' };
}

function normalizeOpt(o) {
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
        if (!aliases.includes(std)) aliases = [std, ...aliases];
        const seen = new Set();
        aliases = aliases.filter((a) => {
            const k = a.toLowerCase();
            if (seen.has(k)) return false;
            seen.add(k);
            return true;
        });
        return { standard: std, aliases, label: std, value: std };
    }
    const s = String(o || '').trim();
    return s ? { standard: s, aliases: [s], label: s, value: s } : null;
}

function normalizeField(f) {
    return {
        id: f?.id || '',
        name: f?.name || '',
        default: f?.default || '',
        options: (f?.options || []).map(normalizeOpt).filter(Boolean),
    };
}

function ensureEnterpriseShape(raw) {
    const ei = raw && typeof raw === 'object' ? raw : {};
    let additional = Array.isArray(ei.additional_columns)
        ? ei.additional_columns.map((c) => ({
            id: c.id || '',
            name: c.name || '',
            help: c.help || '',
            default_value: c.default_value || c.default || '',
            default_source_column: c.default_source_column || '',
            default_mode: 'fixed',
        }))
        : [];
    if (!additional.length && ei.advertiser) {
        additional = [{
            id: 'advertiser',
            name: ei.advertiser.name || 'Advertiser',
            help: ei.advertiser.help || '',
            default_value: ei.advertiser.default || '',
            default_source_column: '',
            default_mode: 'fixed',
        }];
    }
    return {
        mandatory_fields: Array.isArray(ei.mandatory_fields) ? ei.mandatory_fields.map(normalizeField) : [],
        optional_fields: Array.isArray(ei.optional_fields) ? ei.optional_fields.map(normalizeField) : [],
        additional_columns: additional,
        advertiser: {
            id: ei.advertiser?.id || 'advertiser',
            name: ei.advertiser?.name || 'Advertiser',
            default: ei.advertiser?.default || '',
            mode: ei.advertiser?.mode || 'fixed_or_column',
            help: ei.advertiser?.help || '',
        },
    };
}

async function loadConfigCatalog(toastOk) {
    const body = document.getElementById('configBody');
    try {
        const data = await fetchJson('/api/hierarchy/catalog');
        configState = {
            enterprise_info: ensureEnterpriseShape(data.enterprise_info),
            media_hierarchy: Array.isArray(data.media_hierarchy) ? data.media_hierarchy.map((l) => ({ ...l })) : [],
            common_attributes: Array.isArray(data.common_attributes) ? data.common_attributes.map((a) => ({
                ...a,
                options: (a.options || []).map(normalizeOpt).filter(Boolean),
            })) : [],
            standard_fields: Array.isArray(data.standard_fields) ? data.standard_fields : [],
            mapping_dictionary: Array.isArray(data.mapping_dictionary) ? data.mapping_dictionary.map((m) => ({ ...m })) : [],
            publishers: Array.isArray(data.publishers) ? data.publishers.map((p) => ({ ...p, aliases: [...(p.aliases || [])] })) : [],
            metrics: Array.isArray(data.metrics) ? data.metrics.map((m) => ({
                id: m.id || '',
                name: m.name || m.label || m.id || '',
                aliases: [...(m.aliases || [])],
                supports_currency: Boolean(m.supports_currency),
                group: m.group || '',
            })) : [],
            detection_order: Array.isArray(data.detection_order) ? [...data.detection_order] : [],
            combined_field_delimiters: data.combined_field_delimiters || [],
            value_token_hints: data.value_token_hints || {},
            _version: data._version || '3.0',
            _description: data._description || '',
        };
        seedClosedFieldOptions(configState);
        configDirty = false;
        updateSaveChrome();
        await loadLearnedMappings();
        renderConfigBody();
        if (toastOk) showToast('Config reloaded', 'success');
    } catch (err) {
        if (body) body.innerHTML = `<p class="help-text">Failed to load: ${escapeHtml(err.message || err)}</p>`;
        showToast(err.message || 'Failed to load config', 'error');
    }
}

/**
 * Aliases and values the app captured from confirmed mappings and ingests.
 * Kept in its own store, so it loads and saves independently of the catalog.
 */
async function loadLearnedMappings() {
    try {
        learnedState = await fetchJson('/api/learned-mappings');
    } catch (err) {
        learnedState = null;
        console.warn('Could not load learned mappings', err);
    }
}

/** Review a learned entry. Writes through immediately — no Save needed. */
async function reviewLearned(payload) {
    try {
        learnedState = await fetchJson('/api/learned-mappings', {
            method: 'POST',
            body: JSON.stringify(payload),
        });
        renderConfigBody();
    } catch (err) {
        showToast(err.message || 'Could not update learned mapping', 'error');
    }
}

function learnedAliasesFor(targetId) {
    const entries = learnedState?.column_aliases?.[String(targetId || '')];
    return Array.isArray(entries) ? entries : [];
}

function learnedValuesFor(fieldId) {
    const bucket = learnedState?.field_values?.[String(fieldId || '')];
    return Array.isArray(bucket?.values) ? bucket.values : [];
}

function learnedChip(label, meta, actions) {
    return `<span class="cfg-learned-chip ${meta.validated ? 'is-validated' : ''}" title="Seen ${meta.count || 1}× · ${escapeAttr(meta.last_seen || '')}">
        <span class="cfg-learned-label">${escapeHtml(label)}</span>
        ${meta.validated ? '' : '<span class="cfg-pill cfg-pill--new">new</span>'}
        ${actions}
    </span>`;
}

function seedClosedFieldOptions(state) {
    if (!state) return;
    const market = [...(state.enterprise_info?.mandatory_fields || []), ...(state.enterprise_info?.optional_fields || [])]
        .find((f) => f.id === 'market');
    (state.common_attributes || []).forEach((a) => {
        if (!Array.isArray(a.options)) a.options = [];
        if (a.options.length) return;
        if (a.id === 'country' && market?.options?.length) {
            a.options = market.options.map((o) => ({ ...o, aliases: [...(o.aliases || [])] }));
            return;
        }
        const starter = STARTER_VALUE_LISTS[a.id];
        if (starter) a.options = starter.map(normalizeOpt).filter(Boolean);
    });
}

function enterpriseFieldById(id) {
    const ei = configState?.enterprise_info || {};
    const all = [...(ei.mandatory_fields || []), ...(ei.optional_fields || [])];
    return all.find((f) => f.id === id) || null;
}

function collectMasterRows() {
    const byId = new Map();
    const upsert = (id, patch) => {
        const key = String(id || patch.name || '').trim() || `anon-${byId.size}`;
        const cur = byId.get(key) || {
            id: id || '',
            name: patch.name || id || '',
            kinds: [],
            used: new Set(),
            editTab: patch.editTab,
            required: false,
            group: patch.group || '',
            editPriority: 0,
        };
        if (patch.name && (!cur.name || cur.name === cur.id)) cur.name = patch.name;
        if (patch.kind && !cur.kinds.includes(patch.kind)) cur.kinds.push(patch.kind);
        (patch.used || []).forEach((u) => cur.used.add(u));
        if (patch.required) cur.required = true;
        if (patch.group && !cur.group) cur.group = patch.group;
        const pri = patch.editPriority || 0;
        if (pri > (cur.editPriority || 0)) {
            cur.editTab = patch.editTab;
            cur.editPriority = pri;
        } else if (!cur.editTab) {
            cur.editTab = patch.editTab;
            cur.editPriority = pri;
        }
        byId.set(key, cur);
    };
    const ei = configState.enterprise_info || {};
    (ei.mandatory_fields || []).forEach((f) => upsert(f.id, {
        name: f.name, kind: 'Enterprise · required', editTab: 'enterprise', editPriority: 50,
        required: true, used: ['Upload', 'Column shaping', 'Schema Mapping'],
    }));
    (ei.optional_fields || []).forEach((f) => upsert(f.id, {
        name: f.name, kind: 'Enterprise · optional', editTab: 'enterprise', editPriority: 50,
        used: ['Upload', 'Column shaping', 'Schema Mapping'],
    }));
    (ei.additional_columns || []).forEach((c) => upsert(c.id, {
        name: c.name, kind: 'Upload constant', editTab: 'additional', editPriority: 40,
        used: ['Upload', 'Schema Mapping'],
    }));
    (configState.media_hierarchy || []).forEach((lv) => upsert(lv.id, {
        name: lv.name, kind: 'Hierarchy', editTab: 'hierarchy', editPriority: 45,
        required: Boolean(lv.mandatory), group: 'delivery',
        used: ['Column shaping', 'Schema Mapping', 'Grain'],
    }));
    (configState.common_attributes || []).forEach((a) => upsert(a.id, {
        name: a.name, kind: 'Attribute', editTab: 'attributes', editPriority: 30,
        group: a.group || '',
        used: ['Column shaping', 'Schema Mapping'],
    }));
    (configState.metrics || []).forEach((m) => upsert(m.id, {
        name: m.name, kind: 'Metric', editTab: 'metrics', editPriority: 60,
        group: m.group || 'metric',
        used: ['Schema Mapping'],
    }));
    (configState.standard_fields || []).forEach((f) => {
        if (!f?.id) return;
        const type = String(f.type || '');
        if (type === 'metric') {
            upsert(f.id, {
                name: f.label || f.id, kind: 'Metric', editTab: 'metrics', editPriority: 55,
                required: Boolean(f.mandatory), used: ['Schema Mapping'],
            });
        } else if (type === 'date') {
            upsert(f.id, {
                name: f.label || f.id, kind: 'Date', editTab: 'attributes', editPriority: 20,
                required: Boolean(f.mandatory), used: ['Schema Mapping'],
            });
        } else if (!byId.has(f.id)) {
            upsert(f.id, {
                name: f.label || f.id, kind: 'Standard field', editTab: 'attributes', editPriority: 10,
                required: Boolean(f.mandatory), used: ['Column shaping', 'Schema Mapping'],
            });
        } else if (f.mandatory) {
            upsert(f.id, { name: f.label, required: true, editTab: null, editPriority: 0, used: [] });
        }
    });
    return [...byId.values()]
        .sort((a, b) => String(a.name).localeCompare(String(b.name), undefined, { sensitivity: 'base' }))
        .map((row) => {
            const meta = mappingModeMeta(row.id, row.kinds);
            row.mappingMode = meta;
            if (row.id === 'publisher') row.editTab = 'publishers';
            else if (meta.mode === 'closed' || meta.mode === 'open') row.editTab = 'values';
            else if (meta.mode === 'none' && FIELD_VALUE_NONE.has(row.id) && row.id !== 'date') row.editTab = 'metrics';
            return row;
        });
}

function configCounts() {
    return {
        overview: collectMasterRows().length,
        mapping: collectMasterRows().length,
        values: collectMasterRows().filter((r) => r.mappingMode?.mode === 'closed').length,
    };
}

function updateSideCounts() {
    const counts = configState ? configCounts() : {};
    document.querySelectorAll('[data-count-for]').forEach((el) => {
        const n = counts[el.dataset.countFor];
        el.textContent = typeof n === 'number' ? String(n) : '';
    });
    // Flag the sections holding captured entries nobody has looked at yet.
    const review = learnedState?.counts || {};
    const flag = (tab, n) => document
        .querySelector(`[data-config-tab="${tab}"]`)
        ?.classList.toggle('has-review', Number(n || 0) > 0);
    flag('mapping', review.aliases_unvalidated);
    flag('values', review.values_unvalidated);
}

function renderConfigBody() {
    const body = document.getElementById('configBody');
    if (!body || !configState) return;
    const map = {
        overview: renderOverviewTab,
        mapping: renderMappingTab,
        values: renderValuesTab,
    };
    body.innerHTML = (map[configTab] || renderOverviewTab)();
    updateSideCounts();
    document.querySelectorAll('.config-side-link').forEach((b) => {
        b.classList.toggle('active', b.dataset.configTab === configTab);
    });
}

function renderOverviewTab() {
    const rows = collectMasterRows();
    const counts = configCounts();
    const q = String(configMasterQuery || '').trim().toLowerCase();
    const visible = q
        ? rows.filter((r) => [r.name, r.id, r.kinds.join(' '), r.group, r.mappingMode?.label].join(' ').toLowerCase().includes(q))
        : rows;
    const tableRows = visible.map((r) => `
        <tr>
            <td>
                <input class="cfg-input" data-cfg-field="master-name" data-field-id="${escapeAttr(r.id)}"
                    value="${escapeAttr(r.name || r.id)}" aria-label="Display name for ${escapeAttr(r.id)}">
                ${r.required ? '<span class="cfg-pill cfg-pill--req">Required</span>' : ''}
            </td>
            <td class="cfg-mono">${escapeHtml(r.id)}</td>
            <td><span class="cfg-pill cfg-pill--mode-${escapeAttr(r.mappingMode?.mode || '')}">${escapeHtml(r.mappingMode?.label || '')}</span></td>
            <td>${escapeHtml((r.kinds || []).join(' · '))}</td>
            <td class="cfg-td-action">
                <button type="button" class="cfg-text-btn" data-cfg-action="goto"
                    data-tab="${r.mappingMode?.mode === 'closed' ? 'values' : 'mapping'}">
                    ${r.mappingMode?.mode === 'closed' ? 'Values' : 'Aliases'} →
                </button>
            </td>
        </tr>`).join('');
    return `
        <div class="cfg-panel-head">
            <h2>1. Master List</h2>
            <p>Every permitted destination column. Edit the display name here; the stable id is what mappings and pipeline rules use.</p>
        </div>
        <div class="cfg-stat-grid">
            <div class="cfg-stat">
                <span class="cfg-stat-n">${counts.overview}</span>
                <span class="cfg-stat-l">Standard columns</span>
            </div>
            <button type="button" class="cfg-stat" data-cfg-action="goto" data-tab="mapping">
                <span class="cfg-stat-n">${counts.mapping}</span>
                <span class="cfg-stat-l">Column alias groups</span>
            </button>
            <button type="button" class="cfg-stat" data-cfg-action="goto" data-tab="values">
                <span class="cfg-stat-n">${counts.values}</span>
                <span class="cfg-stat-l">Finite value lists</span>
            </button>
        </div>
        <div class="cfg-toolbar">
            <input type="search" id="configMasterSearch" class="cfg-input cfg-search" placeholder="Search by name or id…"
                value="${escapeAttr(configMasterQuery)}" autocomplete="off">
            <span class="cfg-toolbar-meta">${visible.length} of ${rows.length}</span>
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead>
                    <tr>
                        <th>Name</th>
                        <th>Id</th>
                        <th>How we map values</th>
                        <th>Lives in</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>${tableRows || '<tr><td colspan="5" class="cfg-empty">No columns match</td></tr>'}</tbody>
            </table>
        </div>
        <div class="cfg-add-row" style="margin-top:12px">
            <input class="cfg-input" data-cfg-temp="master-name" placeholder="New column name">
            <input class="cfg-input cfg-input--mono" data-cfg-temp="master-id" placeholder="stable_id">
            <select class="cfg-input" data-cfg-temp="master-type">
                <option value="dimension">Dimension</option>
                <option value="metric">Metric</option>
                <option value="hierarchy">Hierarchy</option>
                <option value="date">Date</option>
            </select>
            <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-master-field">Add column</button>
        </div>
    `;
}

function renderFieldBlock(field, listKey, idx, { allowDelete = true, optionQuery = '' } = {}) {
    const opts = field.options || [];
    const query = String(optionQuery || '').trim().toLowerCase();
    const visibleOptions = opts
        .map((option, optionIndex) => ({ option, optionIndex }))
        .filter(({ option }) => !query || [
            option.standard,
            option.label,
            ...(option.aliases || []),
        ].join(' ').toLowerCase().includes(query));
    const optRows = visibleOptions.map(({ option: o, optionIndex: oi }) => `
        <tr>
            <td>
                <input class="cfg-input" data-cfg-field="opt-standard" data-list="${escapeAttr(listKey)}" data-idx="${idx}" data-oi="${oi}"
                    value="${escapeAttr(o.standard || o.label || '')}" placeholder="Standard name">
            </td>
            <td>
                <input class="cfg-input" data-cfg-field="opt-aliases" data-list="${escapeAttr(listKey)}" data-idx="${idx}" data-oi="${oi}"
                    value="${escapeAttr((o.aliases || []).join(', '))}"
                    placeholder="US, USA, United States, …">
            </td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-opt" data-list="${escapeAttr(listKey)}" data-idx="${idx}" data-oi="${oi}">Remove</button></td>
        </tr>`).join('');

    return `<article class="cfg-card">
        <div class="cfg-card-head">
            <div class="cfg-inline-fields">
                <label>Label<input class="cfg-input" data-cfg-field="name" data-list="${escapeAttr(listKey)}" data-idx="${idx}" value="${escapeAttr(field.name)}"></label>
                <label>Id<input class="cfg-input cfg-input--mono" data-cfg-field="id" data-list="${escapeAttr(listKey)}" data-idx="${idx}" value="${escapeAttr(field.id)}"></label>
                <label>Default<input class="cfg-input" data-cfg-field="default" data-list="${escapeAttr(listKey)}" data-idx="${idx}" value="${escapeAttr(field.default)}" placeholder="Optional"></label>
            </div>
            ${allowDelete ? `<button type="button" class="cfg-text-btn danger" data-cfg-action="remove-field" data-list="${escapeAttr(listKey)}" data-idx="${idx}">Delete field</button>` : ''}
        </div>
        <p class="cfg-hint">Many source values → one standard name. Upload shows the standard name; aliases are used when matching source data.</p>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead>
                    <tr>
                        <th style="width:28%">Standard name</th>
                        <th>Known values / aliases (comma-separated)</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>${optRows || '<tr><td colspan="3" class="cfg-empty">No harmonized values yet</td></tr>'}</tbody>
            </table>
        </div>
        <div class="cfg-add-row cfg-add-row--harmonize">
            <input class="cfg-input" data-cfg-temp="opt-standard" data-list="${escapeAttr(listKey)}" data-idx="${idx}" placeholder="Standard name (e.g. United States)">
            <input class="cfg-input" data-cfg-temp="opt-aliases" data-list="${escapeAttr(listKey)}" data-idx="${idx}" placeholder="Aliases: US, USA, United States of America">
            <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-opt" data-list="${escapeAttr(listKey)}" data-idx="${idx}">Add mapping</button>
        </div>
    </article>`;
}

function renderEnterpriseTab() {
    const ei = configState.enterprise_info;
    return `
        <div class="cfg-panel-head">
            <h2>Enterprise fields</h2>
            <p>Market, Category, Brand, and optional lists shown on Upload. Each row is many-to-one: several known values map to one standard name.</p>
            ${renderWherePills(['Upload', 'Column shaping', 'Schema Mapping'])}
        </div>
        <h3 class="cfg-subhead">Required on Upload</h3>
        ${(ei.mandatory_fields || []).map((f, i) => renderFieldBlock(f, 'mandatory_fields', i)).join('') || '<p class="cfg-empty">None</p>'}
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-field" data-list="mandatory_fields">Add required field</button>
        <h3 class="cfg-subhead">Optional on Upload</h3>
        ${(ei.optional_fields || []).map((f, i) => renderFieldBlock(f, 'optional_fields', i)).join('') || '<p class="cfg-empty">None</p>'}
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-field" data-list="optional_fields">Add optional field</button>
    `;
}

function renderAdditionalTab() {
    const cols = configState.enterprise_info.additional_columns || [];
    const rows = cols.map((c, idx) => `
        <tr>
            <td><input class="cfg-input" data-cfg-field="ac-name" data-idx="${idx}" value="${escapeAttr(c.name)}"></td>
            <td><input class="cfg-input cfg-input--mono" data-cfg-field="ac-id" data-idx="${idx}" value="${escapeAttr(c.id)}"></td>
            <td><input class="cfg-input" data-cfg-field="ac-default" data-idx="${idx}" value="${escapeAttr(c.default_value)}" placeholder="Default value"></td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-ac" data-idx="${idx}">Remove</button></td>
        </tr>
        <tr class="cfg-help-row">
            <td colspan="4"><input class="cfg-input" data-cfg-field="ac-help" data-idx="${idx}" value="${escapeAttr(c.help)}" placeholder="Help text shown on Upload"></td>
        </tr>`).join('');

    return `
        <div class="cfg-panel-head">
            <h2>Extra constants</h2>
            <p>Values such as Advertiser or Publisher that analysts can turn on at Upload and fill with a default when the file has no column for them.</p>
            ${renderWherePills(['Upload', 'Schema Mapping'])}
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead>
                    <tr>
                        <th>Name</th><th>Id</th><th>Default value</th><th></th>
                    </tr>
                </thead>
                <tbody>${rows || '<tr><td colspan="4" class="cfg-empty">No additional columns</td></tr>'}</tbody>
            </table>
        </div>
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-ac" style="margin-top:12px">Add column</button>
    `;
}

function renderHierarchyTab() {
    const rows = (configState.media_hierarchy || []).map((lv, idx) => `
        <tr>
            <td class="cfg-level">L${escapeHtml(lv.level ?? idx + 1)}</td>
            <td><input class="cfg-input" data-cfg-field="mh-name" data-idx="${idx}" value="${escapeAttr(lv.name || '')}"></td>
            <td><input class="cfg-input cfg-input--mono" data-cfg-field="mh-id" data-idx="${idx}" value="${escapeAttr(lv.id || '')}"></td>
            <td><label class="cfg-check"><input type="checkbox" data-cfg-field="mh-mandatory" data-idx="${idx}" ${lv.mandatory ? 'checked' : ''}> Required</label></td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-mh" data-idx="${idx}">Remove</button></td>
        </tr>`).join('');
    return `
        <div class="cfg-panel-head">
            <h2>Media hierarchy</h2>
            <p>Delivery spine used for grain selection: Publisher → Campaign → Ad Group → Ad → Creative. Same spine for every publisher.</p>
            ${renderWherePills(['Column shaping', 'Schema Mapping', 'Grain'])}
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead><tr><th></th><th>Name</th><th>Id</th><th></th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-mh" style="margin-top:12px">Add level</button>
    `;
}

function renderAttributesTab() {
    const rows = (configState.common_attributes || []).map((a, idx) => `
        <tr>
            <td><input class="cfg-input" data-cfg-field="ca-name" data-idx="${idx}" value="${escapeAttr(a.name || '')}"></td>
            <td><input class="cfg-input cfg-input--mono" data-cfg-field="ca-id" data-idx="${idx}" value="${escapeAttr(a.id || '')}"></td>
            <td><input class="cfg-input" data-cfg-field="ca-group" data-idx="${idx}" value="${escapeAttr(a.group || '')}"></td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-ca" data-idx="${idx}">Remove</button></td>
        </tr>`).join('');
    return `
        <div class="cfg-panel-head">
            <h2>Attributes</h2>
            <p>Destination dimensions in Column shaping and Schema Mapping (Audience, Campaign Objective, Device Type, …). This is not metrics.</p>
            ${renderWherePills(['Column shaping', 'Schema Mapping'])}
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead><tr><th>Name</th><th>Id</th><th>Group</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-ca" style="margin-top:12px">Add attribute</button>
    `;
}

function renderMetricsTab() {
    const rows = (configState.metrics || []).map((m, idx) => `
        <tr>
            <td><input class="cfg-input" data-cfg-field="met-name" data-idx="${idx}" value="${escapeAttr(m.name || '')}"></td>
            <td><input class="cfg-input cfg-input--mono" data-cfg-field="met-id" data-idx="${idx}" value="${escapeAttr(m.id || '')}"></td>
            <td><input class="cfg-input" data-cfg-field="met-aliases" data-idx="${idx}" value="${escapeAttr((m.aliases || []).join(', '))}" placeholder="spend, cost, media_cost"></td>
            <td><label class="cfg-check"><input type="checkbox" data-cfg-field="met-currency" data-idx="${idx}" ${m.supports_currency ? 'checked' : ''}> Currency</label></td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-met" data-idx="${idx}">Remove</button></td>
        </tr>`).join('');
    return `
        <div class="cfg-panel-head">
            <h2>Metrics</h2>
            <p>Numeric outputs for Schema Mapping (Spend, Impressions, Clicks, ROAS). Packed campaign-name parts map to attributes, not these.</p>
            ${renderWherePills(['Schema Mapping'])}
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead><tr><th>Name</th><th>Id</th><th>Aliases</th><th></th><th></th></tr></thead>
                <tbody>${rows || '<tr><td colspan="5" class="cfg-empty">No metrics</td></tr>'}</tbody>
            </table>
        </div>
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-met" style="margin-top:12px">Add metric</button>
    `;
}

function renderMappingTab() {
    const aliasesByTarget = new Map();
    (configState.mapping_dictionary || []).forEach((mapping) => {
        const target = String(mapping.target || '').trim();
        const source = String(mapping.source || '').trim();
        if (!target || !source) return;
        if (!aliasesByTarget.has(target)) aliasesByTarget.set(target, []);
        const aliases = aliasesByTarget.get(target);
        if (!aliases.some((alias) => alias.toLowerCase() === source.toLowerCase())) aliases.push(source);
    });
    const rows = collectMasterRows().map((field) => `
        <tr>
            <td><strong>${escapeHtml(field.name || field.id)}</strong></td>
            <td class="cfg-mono">${escapeHtml(field.id)}</td>
            <td>
                <input class="cfg-input" data-cfg-field="map-aliases" data-target="${escapeAttr(field.id)}"
                    value="${escapeAttr((aliasesByTarget.get(field.id) || []).join(', '))}"
                    placeholder="Known source headers, comma-separated">
            </td>
            <td>${renderLearnedAliasCell(field.id)}</td>
        </tr>`).join('');
    const unreviewed = learnedState?.counts?.aliases_unvalidated || 0;
    return `
        <div class="cfg-panel-head">
            <h2>2. Column Mapping</h2>
            <p>For each Master List column, enter source header aliases. For example Budget, Cost, Media Cost and Spend all map to <span class="cfg-mono">spends</span>.</p>
            ${renderWherePills(['Schema Mapping'])}
        </div>
        <div class="cfg-callout">
            The standard id and display name already match automatically. Only add alternative source column names here.
        </div>
        <div class="cfg-callout cfg-callout--learn">
            Headers you confirm during Schema Mapping are remembered automatically and apply to the next upload straight away.
            ${unreviewed ? `<strong>${unreviewed}</strong> of them have not been reviewed yet — keep or drop each one below.` : 'Nothing is waiting for review.'}
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead><tr><th>Standard column</th><th>Id</th><th>Source column aliases</th><th>Learned from uploads</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

function renderLearnedAliasCell(targetId) {
    const entries = learnedAliasesFor(targetId);
    if (!entries.length) return '<span class="cfg-empty-inline">—</span>';
    return `<div class="cfg-learned-list">${entries.map((entry) => learnedChip(entry.alias, entry, `
        ${entry.validated ? '' : `<button type="button" class="cfg-chip-btn" title="Keep this alias"
            data-cfg-action="validate-learned-alias" data-target="${escapeAttr(targetId)}" data-alias="${escapeAttr(entry.alias)}">Keep</button>`}
        <button type="button" class="cfg-chip-btn cfg-chip-btn--drop" title="Forget this alias"
            data-cfg-action="remove-learned-alias" data-target="${escapeAttr(targetId)}" data-alias="${escapeAttr(entry.alias)}">×</button>
    `)).join('')}</div>`;
}

function finiteValueFields() {
    const ei = configState.enterprise_info || {};
    const seen = new Set();
    const fields = [];
    const add = (field, listKey, index) => {
        if (!field?.id || seen.has(field.id) || fieldValueMode(field.id) !== 'closed') return;
        seen.add(field.id);
        fields.push({ id: field.id, name: field.name || field.id, field, listKey, index });
    };
    (ei.mandatory_fields || []).forEach((field, index) => add(field, 'mandatory_fields', index));
    (ei.optional_fields || []).forEach((field, index) => add(field, 'optional_fields', index));
    (configState.common_attributes || []).forEach((a, i) => {
        add(a, 'common_attributes', i);
    });
    fields.push({ id: 'publisher', name: 'Publisher', publisher: true });
    return fields.sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
}

/**
 * High-cardinality fields. They cannot have a fixed list, so instead they keep a
 * rolling queue of the most recent unique values, used to recognise the column.
 */
function openValueFields() {
    const ei = configState.enterprise_info || {};
    const known = new Map();
    [...(ei.mandatory_fields || []), ...(ei.optional_fields || []), ...(configState.common_attributes || [])]
        .forEach((field) => {
            if (field?.id && FIELD_VALUE_OPEN.has(field.id)) known.set(field.id, field.name || field.id);
        });
    (configState.standard_fields || []).forEach((field) => {
        if (field?.id && FIELD_VALUE_OPEN.has(field.id) && !known.has(field.id)) {
            known.set(field.id, field.name || field.label || field.id);
        }
    });
    FIELD_VALUE_OPEN.forEach((id) => {
        if (!known.has(id)) known.set(id, id.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase()));
    });
    return [...known.entries()]
        .map(([id, name]) => ({ id, name, open: true }))
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }));
}

/** Standard values an analyst can point a queued value at. */
function standardValueOptions(descriptor) {
    if (descriptor?.publisher) {
        return (configState.publishers || []).map((p) => p.name || p.id).filter(Boolean);
    }
    return (descriptor?.field?.options || [])
        .map((option) => option.standard || option.label || option.value || '')
        .filter(Boolean);
}

/** Values seen in uploads that no configured alias covers yet. */
function renderLearnedValueBlock(descriptor) {
    const entries = learnedValuesFor(descriptor.id);
    if (!entries.length) return '';
    const standards = standardValueOptions(descriptor);
    const rows = entries.map((entry) => {
        const options = [`<option value="">— pick a standard value —</option>`]
            .concat(standards.map((s) => `<option value="${escapeAttr(s)}" ${s === entry.standard ? 'selected' : ''}>${escapeHtml(s)}</option>`))
            .join('');
        return `
            <tr>
                <td>${escapeHtml(entry.value)} ${entry.validated ? '' : '<span class="cfg-pill cfg-pill--new">new</span>'}</td>
                <td class="cfg-num">${escapeHtml(String(entry.count || 1))}</td>
                <td class="cfg-arrow-cell">→</td>
                <td>
                    <select class="cfg-input" data-cfg-field="learned-value-standard"
                        data-field-id="${escapeAttr(descriptor.id)}" data-value="${escapeAttr(entry.value)}">${options}</select>
                </td>
                <td class="cfg-td-action">
                    <button type="button" class="cfg-text-btn" data-cfg-action="remove-learned-value"
                        data-field-id="${escapeAttr(descriptor.id)}" data-value="${escapeAttr(entry.value)}">Forget</button>
                </td>
            </tr>`;
    }).join('');
    return `
        <article class="cfg-card cfg-card--review">
            <div class="cfg-card-head">
                <div>
                    <h3 class="cfg-card-title">Seen in uploads, not in the list yet</h3>
                    <p class="cfg-hint">Pick a standard value to fold these into the fixed list, or forget them. Until then they pass through unchanged.</p>
                </div>
            </div>
            <div class="cfg-table-wrap">
                <table class="cfg-table">
                    <thead><tr><th>Source value</th><th>Seen</th><th></th><th>Standard value</th><th></th></tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        </article>`;
}

/** The rolling queue for an open field. */
function renderOpenQueueBlock(descriptor, query) {
    const q = String(query || '').trim().toLowerCase();
    const entries = learnedValuesFor(descriptor.id)
        .filter((entry) => !q || String(entry.value).toLowerCase().includes(q))
        .sort((a, b) => String(b.last_seen || '').localeCompare(String(a.last_seen || '')));
    const cap = learnedState?.open_queue_cap || FIELD_VALUE_CAP;
    const total = learnedValuesFor(descriptor.id).length;
    const rows = entries.map((entry) => `
        <tr>
            <td class="cfg-mono">${escapeHtml(entry.value)}</td>
            <td class="cfg-num">${escapeHtml(String(entry.count || 1))}</td>
            <td>${escapeHtml(String(entry.last_seen || '').replace('T', ' ').replace('+00:00', ' UTC'))}</td>
            <td class="cfg-td-action">
                <button type="button" class="cfg-text-btn" data-cfg-action="remove-learned-value"
                    data-field-id="${escapeAttr(descriptor.id)}" data-value="${escapeAttr(entry.value)}">Forget</button>
            </td>
        </tr>`).join('');
    return `
        <article class="cfg-card">
            <div class="cfg-card-head">
                <div>
                    <h3 class="cfg-card-title">${escapeHtml(descriptor.name)} — recent values</h3>
                    <p class="cfg-hint">
                        ${escapeHtml(descriptor.name)} has too many values for a fixed list, so the newest ${cap} unique values are kept
                        (${total} stored). They help recognise this column in a new file; the values themselves are never rewritten.
                    </p>
                </div>
            </div>
            <div class="cfg-table-wrap">
                <table class="cfg-table">
                    <thead><tr><th>Value</th><th>Seen</th><th>Last seen</th><th></th></tr></thead>
                    <tbody>${rows || '<tr><td colspan="4" class="cfg-empty">Nothing captured yet. Values arrive after a file is processed.</td></tr>'}</tbody>
                </table>
            </div>
        </article>`;
}

function valueSearchRows(fields, query) {
    const q = String(query || '').trim().toLowerCase();
    if (!q) return [];
    const rows = [];
    fields.forEach((descriptor) => {
        if (descriptor.publisher) {
            (configState.publishers || []).forEach((publisher) => {
                const standard = publisher.name || publisher.id;
                (publisher.aliases || []).concat([standard]).forEach((alias) => {
                    if (String(alias).toLowerCase().includes(q) || String(standard).toLowerCase().includes(q)) {
                        rows.push({ dimension: descriptor.name, id: descriptor.id, alias, standard });
                    }
                });
            });
            return;
        }
        (descriptor.field.options || []).forEach((option) => {
            const standard = option.standard || option.label || option.value || '';
            (option.aliases || []).concat([standard]).forEach((alias) => {
                if (String(alias).toLowerCase().includes(q) || String(standard).toLowerCase().includes(q)) {
                    rows.push({ dimension: descriptor.name, id: descriptor.id, alias, standard });
                }
            });
        });
    });
    const seen = new Set();
    return rows.filter((row) => {
        const key = `${row.id}\u0000${String(row.alias).toLowerCase()}\u0000${String(row.standard).toLowerCase()}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
    });
}

function renderPublisherValueBlock(query) {
    const q = String(query || '').trim().toLowerCase();
    const publisherRows = (configState.publishers || []).map((publisher, idx) => `
        <tr>
            <td><input class="cfg-input" data-cfg-field="pub-name" data-idx="${idx}" value="${escapeAttr(publisher.name || '')}"></td>
            <td><input class="cfg-input cfg-input--mono" data-cfg-field="pub-id" data-idx="${idx}" value="${escapeAttr(publisher.id || '')}"></td>
            <td><input class="cfg-input" data-cfg-field="pub-aliases" data-idx="${idx}"
                value="${escapeAttr((publisher.aliases || []).join(', '))}" placeholder="Meta, FB, Instagram"></td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-pub" data-idx="${idx}">Remove</button></td>
        </tr>`)
        .filter((_html, idx) => {
            if (!q) return true;
            const publisher = configState.publishers[idx];
            return [publisher.name, publisher.id, ...(publisher.aliases || [])].join(' ').toLowerCase().includes(q);
        })
        .join('');
    return `
        <article class="cfg-card">
            <div class="cfg-card-head">
                <div>
                    <h3 class="cfg-card-title">Publisher</h3>
                    <p class="cfg-hint">Publisher names are standard values; aliases identify and harmonize source values.</p>
                </div>
            </div>
            <div class="cfg-table-wrap">
                <table class="cfg-table">
                    <thead><tr><th>Standard publisher</th><th>Id</th><th>Known values / aliases</th><th></th></tr></thead>
                    <tbody>${publisherRows || '<tr><td colspan="4" class="cfg-empty">No matching publishers</td></tr>'}</tbody>
                </table>
            </div>
            <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-pub" style="margin-top:12px">Add publisher</button>
        </article>`;
}

function renderValuesTab() {
    const fields = finiteValueFields();
    const openFields = openValueFields();
    const all = [...fields, ...openFields];
    if (configValueField !== 'all' && !all.some((field) => field.id === configValueField)) {
        configValueField = fields[0]?.id || 'all';
    }
    const selected = all.find((field) => field.id === configValueField);
    const optionFor = (field) => `<option value="${escapeAttr(field.id)}" ${field.id === configValueField ? 'selected' : ''}>${escapeHtml(field.name)}</option>`;
    const selectorOptions = `
        <option value="all" ${configValueField === 'all' ? 'selected' : ''}>All dimensions (search)</option>
        <optgroup label="Fixed value lists">${fields.map(optionFor).join('')}</optgroup>
        <optgroup label="Recent-value queues">${openFields.map(optionFor).join('')}</optgroup>`;

    let content = '';
    if (configValueField === 'all') {
        const matches = valueSearchRows(fields, configValueQuery);
        content = configValueQuery
            ? `<div class="cfg-table-wrap">
                <table class="cfg-table">
                    <thead><tr><th>Dimension</th><th>Source value / alias</th><th></th><th>Standard value</th></tr></thead>
                    <tbody>${matches.map((row) => `
                        <tr>
                            <td><button type="button" class="cfg-text-btn" data-cfg-action="select-value-field" data-field-id="${escapeAttr(row.id)}">${escapeHtml(row.dimension)}</button></td>
                            <td>${escapeHtml(row.alias)}</td><td class="cfg-arrow-cell">→</td><td><strong>${escapeHtml(row.standard)}</strong></td>
                        </tr>`).join('') || '<tr><td colspan="4" class="cfg-empty">No value mappings match this search</td></tr>'}</tbody>
                </table>
            </div>`
            : '<p class="cfg-empty">Enter a value or alias to see which dimension and standard value it maps to.</p>';
    } else if (selected?.open) {
        content = renderOpenQueueBlock(selected, configValueQuery);
    } else if (selected?.publisher) {
        content = renderPublisherValueBlock(configValueQuery) + renderLearnedValueBlock(selected);
    } else if (selected) {
        content = renderFieldBlock(
            selected.field,
            selected.listKey,
            selected.index,
            { allowDelete: false, optionQuery: configValueQuery },
        ) + renderLearnedValueBlock(selected);
    }

    return `
        <div class="cfg-panel-head">
            <h2>3. Value Mapping</h2>
            <p>For finite dimensions, map source values to one standard value. These mappings support both column detection and value harmonization.</p>
            ${renderWherePills(['Column shaping', 'Schema Mapping', 'Upload'])}
        </div>
        <div class="cfg-callout">
            Example: if UK appears in sampled values, the column can be identified as Country; during harmonization UK becomes United Kingdom.
        </div>
        <div class="cfg-value-toolbar">
            <label>
                Dimension
                <select id="configValueDimension" class="cfg-input">${selectorOptions}</select>
            </label>
            <label>
                Search mappings
                <input id="configValueSearch" type="search" class="cfg-input"
                    value="${escapeAttr(configValueQuery)}" placeholder="Try UK, ROAS, Meta…">
            </label>
        </div>
        ${content}
        <p class="cfg-hint">
            Fixed value lists are yours to curate. Recent-value queues fill themselves from processed files and hold the newest
            ${escapeHtml(String(learnedState?.open_queue_cap || FIELD_VALUE_CAP))} unique values. Dates and metrics pass through unchanged.
        </p>
    `;
}

function renderPublishersTab() {
    const rows = (configState.publishers || []).map((p, idx) => `
        <tr>
            <td><input class="cfg-input" data-cfg-field="pub-name" data-idx="${idx}" value="${escapeAttr(p.name || '')}"></td>
            <td><input class="cfg-input cfg-input--mono" data-cfg-field="pub-id" data-idx="${idx}" value="${escapeAttr(p.id || '')}"></td>
            <td><input class="cfg-input" data-cfg-field="pub-aliases" data-idx="${idx}" value="${escapeAttr((p.aliases || []).join(', '))}" placeholder="Comma-separated aliases"></td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-pub" data-idx="${idx}">Remove</button></td>
        </tr>`).join('');
    return `
        <div class="cfg-panel-head">
            <h2>Publishers</h2>
            <p>Names and aliases used to detect the platform on Upload. All publishers share the same media hierarchy.</p>
            ${renderWherePills(['Upload'])}
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead><tr><th>Name</th><th>Id</th><th>Aliases</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-pub" style="margin-top:12px">Add publisher</button>
    `;
}

function fieldList(listKey) {
    if (listKey === 'common_attributes') return configState.common_attributes;
    return configState.enterprise_info[listKey];
}

function updateMasterFieldName(fieldId, name) {
    const id = String(fieldId || '');
    const label = String(name || '');
    const ei = configState.enterprise_info || {};
    for (const list of [
        ei.mandatory_fields,
        ei.optional_fields,
        ei.additional_columns,
        configState.media_hierarchy,
        configState.common_attributes,
        configState.metrics,
    ]) {
        const item = (list || []).find((entry) => String(entry.id || '') === id);
        if (item) item.name = label;
    }
    const standard = (configState.standard_fields || []).find((field) => String(field.id || '') === id);
    if (standard) standard.label = label;
}

function onConfigInput(e) {
    const el = e.target;
    if (el.id === 'configValueDimension') {
        configValueField = el.value || 'all';
        configValueQuery = '';
        renderConfigBody();
        return;
    }
    if (el.id === 'configValueSearch') {
        configValueQuery = el.value;
        renderConfigBody();
        const search = document.getElementById('configValueSearch');
        if (search) {
            search.focus();
            search.setSelectionRange(search.value.length, search.value.length);
        }
        return;
    }
    if (el.id === 'configMasterSearch') {
        configMasterQuery = el.value;
        renderConfigBody();
        const s = document.getElementById('configMasterSearch');
        if (s) {
            s.focus();
            const n = s.value.length;
            s.setSelectionRange(n, n);
        }
        return;
    }
    const field = el.dataset?.cfgField;
    if (!field || !configState) return;

    // The learned store saves on its own, so it never joins the dirty catalog.
    if (field === 'learned-value-standard') {
        reviewLearned({
            action: 'set_value_standard',
            field_id: el.dataset.fieldId,
            value: el.dataset.value,
            standard: el.value,
        });
        return;
    }
    const idx = Number(el.dataset.idx);
    const listKey = el.dataset.list;
    const oi = Number(el.dataset.oi);

    if (field === 'master-name') {
        updateMasterFieldName(el.dataset.fieldId, el.value);
    } else if (field === 'map-aliases') {
        const target = String(el.dataset.target || '').trim();
        if (!target) return;
        const aliases = String(el.value || '').split(',').map((s) => s.trim()).filter(Boolean);
        configState.mapping_dictionary = (configState.mapping_dictionary || [])
            .filter((mapping) => String(mapping.target || '') !== target);
        aliases.forEach((source) => configState.mapping_dictionary.push({ source, target }));
    } else if (listKey && (field === 'name' || field === 'id' || field === 'default')) {
        const list = fieldList(listKey);
        if (list?.[idx]) list[idx][field] = el.value;
    } else if (listKey && (field === 'opt-standard' || field === 'opt-aliases')) {
        const list = fieldList(listKey);
        const opt = list?.[idx]?.options?.[oi];
        if (!opt) return;
        if (field === 'opt-standard') {
            opt.standard = el.value;
            opt.label = el.value;
            opt.value = el.value;
        } else {
            opt.aliases = String(el.value || '').split(',').map((s) => s.trim()).filter(Boolean);
        }
    } else if (field === 'ac-name' || field === 'ac-id' || field === 'ac-help' || field === 'ac-default') {
        const col = configState.enterprise_info.additional_columns[idx];
        if (!col) return;
        if (field === 'ac-name') col.name = el.value;
        else if (field === 'ac-id') col.id = el.value;
        else if (field === 'ac-help') col.help = el.value;
        else if (field === 'ac-default') col.default_value = el.value;
    } else if (field === 'mh-name' || field === 'mh-id') {
        const lv = configState.media_hierarchy[idx];
        if (!lv) return;
        lv[field === 'mh-name' ? 'name' : 'id'] = el.value;
        lv.level = idx + 1;
    } else if (field === 'mh-mandatory') {
        const lv = configState.media_hierarchy[idx];
        if (lv) lv.mandatory = el.checked;
    } else if (field === 'ca-name' || field === 'ca-id' || field === 'ca-group') {
        const a = configState.common_attributes[idx];
        if (!a) return;
        a[field.replace('ca-', '')] = el.value;
    } else if (field === 'map-source' || field === 'map-target') {
        const m = configState.mapping_dictionary[idx];
        if (!m) return;
        m[field === 'map-source' ? 'source' : 'target'] = el.value;
    } else if (field === 'pub-name' || field === 'pub-id') {
        const p = configState.publishers[idx];
        if (!p) return;
        p[field === 'pub-name' ? 'name' : 'id'] = el.value;
    } else if (field === 'pub-aliases') {
        const p = configState.publishers[idx];
        if (p) p.aliases = String(el.value || '').split(',').map((s) => s.trim()).filter(Boolean);
    } else if (field === 'met-name' || field === 'met-id' || field === 'met-aliases' || field === 'met-currency') {
        const m = configState.metrics[idx];
        if (!m) return;
        if (field === 'met-name') m.name = el.value;
        else if (field === 'met-id') m.id = el.value;
        else if (field === 'met-aliases') m.aliases = String(el.value || '').split(',').map((s) => s.trim()).filter(Boolean);
        else m.supports_currency = el.checked;
    } else {
        return;
    }
    markConfigDirty();
}

function onConfigKeydown(e) {
    if (e.key !== 'Enter') return;
    const gotoRow = e.target.closest?.('[data-cfg-action="goto"]');
    if (gotoRow && (e.target === gotoRow || e.target.classList.contains('cfg-master-row'))) {
        e.preventDefault();
        setConfigTab(gotoRow.dataset.tab || 'overview');
        return;
    }
    const el = e.target;
    if (!el.dataset?.cfgTemp) return;
    e.preventDefault();
    const btn = el.closest('.cfg-add-row')?.querySelector('[data-cfg-action]');
    btn?.click();
}

function onConfigClick(e) {
    const btn = e.target.closest('[data-cfg-action]');
    if (!btn || !configState) return;
    const action = btn.dataset.cfgAction;
    const idx = Number(btn.dataset.idx);
    const listKey = btn.dataset.list;
    const oi = Number(btn.dataset.oi);

    if (action === 'goto') {
        setConfigTab(btn.dataset.tab || 'overview');
        return;
    } else if (action === 'validate-learned-alias') {
        reviewLearned({
            action: 'validate_alias',
            target_id: btn.dataset.target,
            alias: btn.dataset.alias,
            validated: true,
        });
        return;
    } else if (action === 'remove-learned-alias') {
        reviewLearned({
            action: 'remove_alias',
            target_id: btn.dataset.target,
            alias: btn.dataset.alias,
        });
        return;
    } else if (action === 'remove-learned-value') {
        reviewLearned({
            action: 'remove_value',
            field_id: btn.dataset.fieldId,
            value: btn.dataset.value,
        });
        return;
    } else if (action === 'select-value-field') {
        configValueField = btn.dataset.fieldId || 'all';
        configValueQuery = '';
        renderConfigBody();
        return;
    } else if (action === 'add-master-field') {
        const row = btn.closest('.cfg-add-row');
        const name = String(row?.querySelector('[data-cfg-temp="master-name"]')?.value || '').trim();
        const id = String(row?.querySelector('[data-cfg-temp="master-id"]')?.value || '')
            .trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
        const type = String(row?.querySelector('[data-cfg-temp="master-type"]')?.value || 'dimension');
        if (!name || !id) {
            showToast('Enter both a column name and stable id', 'error');
            return;
        }
        if (collectMasterRows().some((field) => field.id === id)) {
            showToast(`Column id "${id}" already exists`, 'error');
            return;
        }
        configState.standard_fields.push({ id, label: name, type, mandatory: false });
        if (type === 'metric') {
            configState.metrics.push({ id, name, aliases: [], supports_currency: false, group: '' });
        } else if (type === 'hierarchy') {
            configState.media_hierarchy.push({
                level: configState.media_hierarchy.length + 1, id, name, mandatory: false,
            });
        } else if (type === 'dimension') {
            configState.common_attributes.push({ id, name, group: '', options: [] });
        }
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-field' && listKey) {
        fieldList(listKey).push({ id: '', name: '', default: '', options: [] });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-field' && listKey) {
        fieldList(listKey).splice(idx, 1);
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-opt' && listKey) {
        const row = btn.closest('.cfg-card') || btn.closest('.cfg-add-row')?.parentElement;
        const stdEl = row?.querySelector(`[data-cfg-temp="opt-standard"][data-idx="${idx}"]`);
        const aliasEl = row?.querySelector(`[data-cfg-temp="opt-aliases"][data-idx="${idx}"]`);
        const standard = String(stdEl?.value || '').trim();
        const aliases = String(aliasEl?.value || '').split(',').map((s) => s.trim()).filter(Boolean);
        if (!standard && !aliases.length) return;
        const list = fieldList(listKey);
        if (!list[idx].options) list[idx].options = [];
        const std = standard || aliases[0];
        const all = aliases.length ? aliases : [std];
        if (!all.map((a) => a.toLowerCase()).includes(std.toLowerCase())) all.unshift(std);
        list[idx].options.push(normalizeOpt({ standard: std, aliases: all }));
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-opt' && listKey) {
        fieldList(listKey)[idx]?.options?.splice(oi, 1);
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-ac') {
        configState.enterprise_info.additional_columns.push({
            id: '', name: '', help: '', default_value: '', default_source_column: '', default_mode: 'fixed',
        });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-ac') {
        configState.enterprise_info.additional_columns.splice(idx, 1);
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-mh') {
        const n = configState.media_hierarchy.length + 1;
        configState.media_hierarchy.push({ level: n, id: '', name: '', mandatory: false });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-mh') {
        configState.media_hierarchy.splice(idx, 1);
        configState.media_hierarchy.forEach((lv, i) => { lv.level = i + 1; });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-ca') {
        configState.common_attributes.push({ id: '', name: '', group: '' });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-ca') {
        configState.common_attributes.splice(idx, 1);
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-met') {
        if (!Array.isArray(configState.metrics)) configState.metrics = [];
        configState.metrics.push({ id: '', name: '', aliases: [], supports_currency: false, group: '' });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-met') {
        configState.metrics.splice(idx, 1);
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-map') {
        configState.mapping_dictionary.push({ source: '', target: '' });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-map') {
        configState.mapping_dictionary.splice(idx, 1);
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'add-pub') {
        configState.publishers.push({ id: '', name: '', aliases: [] });
        markConfigDirty();
        renderConfigBody();
    } else if (action === 'remove-pub') {
        configState.publishers.splice(idx, 1);
        markConfigDirty();
        renderConfigBody();
    }
}

async function saveConfigCatalog() {
    if (!configState) return;
    const ei = configState.enterprise_info;
    const advCol = (ei.additional_columns || []).find((c) => c.id === 'advertiser');
    const payload = {
        _version: configState._version || '3.0',
        _description: configState._description
            || 'Enterprise info defaults + simplified media hierarchy + common attributes.',
        enterprise_info: {
            mandatory_fields: (ei.mandatory_fields || []).map((f) => ({
                id: f.id,
                name: f.name,
                default: f.default || '',
                options: (f.options || []).map((o) => ({
                    standard: o.standard || o.label || '',
                    aliases: Array.isArray(o.aliases) && o.aliases.length
                        ? o.aliases
                        : [o.standard || o.label || o.value].filter(Boolean),
                })),
            })),
            optional_fields: (ei.optional_fields || []).map((f) => ({
                id: f.id,
                name: f.name,
                default: f.default || '',
                options: (f.options || []).map((o) => ({
                    standard: o.standard || o.label || '',
                    aliases: Array.isArray(o.aliases) && o.aliases.length
                        ? o.aliases
                        : [o.standard || o.label || o.value].filter(Boolean),
                })),
            })),
            additional_columns: (ei.additional_columns || []).map((c) => ({
                id: c.id,
                name: c.name,
                help: c.help || '',
                default_value: c.default_value || '',
                default_source_column: '',
                default_mode: 'fixed',
            })),
            advertiser: {
                id: 'advertiser',
                name: advCol?.name || ei.advertiser?.name || 'Advertiser',
                default: advCol?.default_value || '',
                mode: 'fixed_or_column',
                help: advCol?.help || '',
            },
        },
        media_hierarchy: (configState.media_hierarchy || []).map((lv, i) => ({
            level: Number(lv.level) || i + 1,
            id: lv.id,
            name: lv.name,
            mandatory: Boolean(lv.mandatory),
        })),
        common_attributes: (configState.common_attributes || []).map((a) => ({
            id: a.id,
            name: a.name,
            group: a.group || '',
            options: (a.options || []).map((o) => ({
                standard: o.standard || o.label || '',
                aliases: Array.isArray(o.aliases) && o.aliases.length
                    ? o.aliases
                    : [o.standard || o.label || o.value].filter(Boolean),
            })),
        })),
        metrics: (configState.metrics || []).filter((m) => m.id).map((m) => ({
            id: m.id,
            name: m.name || m.id,
            aliases: m.aliases || [],
            supports_currency: Boolean(m.supports_currency),
            ...(m.group ? { group: m.group } : {}),
        })),
        standard_fields: (() => {
            const rest = (configState.standard_fields || []).filter((f) => f && f.type !== 'metric');
            const prevMand = {};
            (configState.standard_fields || []).forEach((f) => {
                if (f?.type === 'metric' && f.id) prevMand[f.id] = Boolean(f.mandatory);
            });
            const metricFields = (configState.metrics || []).filter((m) => m.id).map((m) => ({
                id: m.id,
                label: m.name || m.id,
                type: 'metric',
                mandatory: prevMand[m.id] != null ? prevMand[m.id] : ['spends', 'impressions'].includes(m.id),
            }));
            return [...rest, ...metricFields];
        })(),
        mapping_dictionary: configState.mapping_dictionary || [],
        publishers: Object.fromEntries(
            (configState.publishers || [])
                .filter((p) => p.id)
                .map((p) => [p.id, { id: p.id, name: p.name, aliases: p.aliases || [] }]),
        ),
        detection_order: configState.detection_order?.length
            ? configState.detection_order
            : (configState.publishers || []).map((p) => p.id).filter(Boolean),
        publisher_aliases: Object.fromEntries(
            (configState.publishers || [])
                .filter((p) => p.id)
                .map((p) => [p.id, p.aliases || []]),
        ),
        combined_field_delimiters: configState.combined_field_delimiters || [],
        value_token_hints: configState.value_token_hints || {},
    };

    try {
        const res = await fetch('/api/hierarchy/catalog', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `Save failed (${res.status})`);
        configDirty = false;
        updateSaveChrome();
        showToast(data.message || 'Config saved', 'success');
        await loadConfigCatalog(false);
    } catch (err) {
        showToast(err.message || 'Failed to save config', 'error');
    }
}
