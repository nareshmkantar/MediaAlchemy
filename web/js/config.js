/**
 * Config module — edit media_hierarchies.json via /api/hierarchy/catalog.
 * Clean settings-style UI: side nav + tables / forms.
 */
let configState = null;
let configTab = 'enterprise';
let configDirty = false;

document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.config-side-link').forEach((btn) => {
        btn.addEventListener('click', () => {
            configTab = btn.dataset.configTab || 'enterprise';
            document.querySelectorAll('.config-side-link').forEach((b) => b.classList.toggle('active', b === btn));
            renderConfigBody();
        });
    });
    document.getElementById('configReloadBtn')?.addEventListener('click', () => loadConfigCatalog(true));
    document.getElementById('configSaveBtn')?.addEventListener('click', saveConfigCatalog);
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
            common_attributes: Array.isArray(data.common_attributes) ? data.common_attributes.map((a) => ({ ...a })) : [],
            standard_fields: Array.isArray(data.standard_fields) ? data.standard_fields : [],
            mapping_dictionary: Array.isArray(data.mapping_dictionary) ? data.mapping_dictionary.map((m) => ({ ...m })) : [],
            publishers: Array.isArray(data.publishers) ? data.publishers.map((p) => ({ ...p, aliases: [...(p.aliases || [])] })) : [],
            detection_order: Array.isArray(data.detection_order) ? [...data.detection_order] : [],
            combined_field_delimiters: data.combined_field_delimiters || [],
            value_token_hints: data.value_token_hints || {},
            _version: data._version || '3.0',
            _description: data._description || '',
        };
        configDirty = false;
        renderConfigBody();
        if (toastOk) showToast('Config reloaded', 'success');
    } catch (err) {
        if (body) body.innerHTML = `<p class="help-text">Failed to load: ${escapeHtml(err.message || err)}</p>`;
        showToast(err.message || 'Failed to load config', 'error');
    }
}

function renderConfigBody() {
    const body = document.getElementById('configBody');
    if (!body || !configState) return;
    const map = {
        enterprise: renderEnterpriseTab,
        additional: renderAdditionalTab,
        hierarchy: renderHierarchyTab,
        attributes: renderAttributesTab,
        mapping: renderMappingTab,
        publishers: renderPublishersTab,
    };
    body.innerHTML = (map[configTab] || renderEnterpriseTab)();
}

function renderFieldBlock(field, listKey, idx) {
    const opts = field.options || [];
    const optRows = opts.map((o, oi) => `
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
            <button type="button" class="cfg-text-btn danger" data-cfg-action="remove-field" data-list="${escapeAttr(listKey)}" data-idx="${idx}">Delete field</button>
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
            <h2>Enterprise info</h2>
            <p>Market, Category, and Brand on Upload. Each row is a many-to-one harmonization: several known values map to one standard name shown to users.</p>
        </div>
        <h3 class="cfg-subhead">Required fields</h3>
        ${(ei.mandatory_fields || []).map((f, i) => renderFieldBlock(f, 'mandatory_fields', i)).join('') || '<p class="cfg-empty">None</p>'}
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-field" data-list="mandatory_fields">Add required field</button>
        <h3 class="cfg-subhead">Optional fields</h3>
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
            <h2>Additional columns</h2>
            <p>Constants such as Advertiser or Publisher that analysts can enable on Upload and fill with a default value.</p>
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
            <p>Shared spine for grain selection: Publisher → Campaign → Ad Group → Ad → Creative.</p>
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
            <h2>Common attributes</h2>
            <p>Destination attributes available in Schema Mapping (Device Type, Campaign Objective, …).</p>
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

function renderMappingTab() {
    const rows = (configState.mapping_dictionary || []).map((m, idx) => `
        <tr>
            <td><input class="cfg-input" data-cfg-field="map-source" data-idx="${idx}" value="${escapeAttr(m.source || '')}" placeholder="Source synonym"></td>
            <td class="cfg-arrow-cell">→</td>
            <td><input class="cfg-input cfg-input--mono" data-cfg-field="map-target" data-idx="${idx}" value="${escapeAttr(m.target || '')}" placeholder="Target id"></td>
            <td class="cfg-td-action"><button type="button" class="cfg-text-btn" data-cfg-action="remove-map" data-idx="${idx}">Remove</button></td>
        </tr>`).join('');
    return `
        <div class="cfg-panel-head">
            <h2>Mapping dictionary</h2>
            <p>Column name synonyms → standard ids (including market codes like UK when present in source files).</p>
        </div>
        <div class="cfg-table-wrap">
            <table class="cfg-table">
                <thead><tr><th>Source synonym</th><th></th><th>Target id</th><th></th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
        <button type="button" class="btn-secondary btn-sm" data-cfg-action="add-map" style="margin-top:12px">Add mapping</button>
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
            <p>Detection aliases only. All publishers share the same media hierarchy spine.</p>
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
    return configState.enterprise_info[listKey];
}

function onConfigInput(e) {
    const el = e.target;
    const field = el.dataset?.cfgField;
    if (!field || !configState) return;
    const idx = Number(el.dataset.idx);
    const listKey = el.dataset.list;
    const oi = Number(el.dataset.oi);

    if (listKey && (field === 'name' || field === 'id' || field === 'default')) {
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
    } else {
        return;
    }
    configDirty = true;
}

function onConfigKeydown(e) {
    if (e.key !== 'Enter') return;
    const el = e.target;
    if (!el.dataset?.cfgTemp) return;
    e.preventDefault();
    const btn = el.closest('.cfg-add-row')?.querySelector('[data-cfg-action="add-opt"]');
    btn?.click();
}

function onConfigClick(e) {
    const btn = e.target.closest('[data-cfg-action]');
    if (!btn || !configState) return;
    const action = btn.dataset.cfgAction;
    const idx = Number(btn.dataset.idx);
    const listKey = btn.dataset.list;
    const oi = Number(btn.dataset.oi);

    if (action === 'add-field' && listKey) {
        fieldList(listKey).push({ id: '', name: '', default: '', options: [] });
        configDirty = true;
        renderConfigBody();
    } else if (action === 'remove-field' && listKey) {
        fieldList(listKey).splice(idx, 1);
        configDirty = true;
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
        configDirty = true;
        renderConfigBody();
    } else if (action === 'remove-opt' && listKey) {
        fieldList(listKey)[idx]?.options?.splice(oi, 1);
        configDirty = true;
        renderConfigBody();
    } else if (action === 'add-ac') {
        configState.enterprise_info.additional_columns.push({
            id: '', name: '', help: '', default_value: '', default_source_column: '', default_mode: 'fixed',
        });
        configDirty = true;
        renderConfigBody();
    } else if (action === 'remove-ac') {
        configState.enterprise_info.additional_columns.splice(idx, 1);
        configDirty = true;
        renderConfigBody();
    } else if (action === 'add-mh') {
        const n = configState.media_hierarchy.length + 1;
        configState.media_hierarchy.push({ level: n, id: '', name: '', mandatory: false });
        configDirty = true;
        renderConfigBody();
    } else if (action === 'remove-mh') {
        configState.media_hierarchy.splice(idx, 1);
        configState.media_hierarchy.forEach((lv, i) => { lv.level = i + 1; });
        configDirty = true;
        renderConfigBody();
    } else if (action === 'add-ca') {
        configState.common_attributes.push({ id: '', name: '', group: '' });
        configDirty = true;
        renderConfigBody();
    } else if (action === 'remove-ca') {
        configState.common_attributes.splice(idx, 1);
        configDirty = true;
        renderConfigBody();
    } else if (action === 'add-map') {
        configState.mapping_dictionary.push({ source: '', target: '' });
        configDirty = true;
        renderConfigBody();
    } else if (action === 'remove-map') {
        configState.mapping_dictionary.splice(idx, 1);
        configDirty = true;
        renderConfigBody();
    } else if (action === 'add-pub') {
        configState.publishers.push({ id: '', name: '', aliases: [] });
        configDirty = true;
        renderConfigBody();
    } else if (action === 'remove-pub') {
        configState.publishers.splice(idx, 1);
        configDirty = true;
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
        common_attributes: configState.common_attributes || [],
        standard_fields: configState.standard_fields || [],
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
        showToast(data.message || 'Config saved', 'success');
        await loadConfigCatalog(false);
    } catch (err) {
        showToast(err.message || 'Failed to save config', 'error');
    }
}
