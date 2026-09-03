/**
 * Settings Page Logic
 */

let hierarchyCatalogState = null;
let hierarchyAdminTab = 'enterprise';
let hierarchySelectedPublisherId = null;

document.addEventListener('DOMContentLoaded', () => {
    loadSettings();
    initHandlers();
});

async function loadSettings() {
    try {
        const config = await getConfig();

        // Model
        if (config.llm_model) {
            document.getElementById('modelSelect').value = config.llm_model;
        }


        // Azure Fields
        if (config.azure_endpoint) document.getElementById('azureEndpointInput').value = config.azure_endpoint;
        if (config.azure_api_version) document.getElementById('azureVersionInput').value = config.azure_api_version;
        if (config.azure_deployment) document.getElementById('azureDeploymentInput').value = config.azure_deployment;

        toggleAzureFields(config.llm_model);
        syncAzureDeploymentFromModel(config.llm_model);

        // API status
        const statusEl = document.getElementById('apiStatus');
        const params = new URLSearchParams(window.location.search);
        if (params.get('reason') === 'llm') {
            showToast('Configure and verify your LLM API key to continue.', 'warning');
        }

        if (config.api_key_set) {
            statusEl.innerHTML = `<span class="status-ok">✓ API key configured — verifying…</span>`;
            try {
                const verify = await verifyLlmConfig();
                if (verify.ok) {
                    statusEl.innerHTML = `<span class="status-ok">✓ ${verify.message || 'LLM verified'}</span>`;
                } else {
                    statusEl.innerHTML = `<span class="status-warn">⚠ ${verify.message || 'Verification failed'}</span>`;
                }
            } catch (e) {
                statusEl.innerHTML = `<span class="status-warn">⚠ Could not verify API key</span>`;
            }
        } else {
            statusEl.innerHTML = `<span class="status-warn">⚠ No API key set</span>`;
        }

        const returnUrl = params.get('return');
        if (returnUrl) {
            const back = document.createElement('p');
            back.className = 'help-text';
            back.innerHTML = `<a href="${decodeURIComponent(returnUrl)}">← Back to where you left off</a>`;
            statusEl.appendChild(back);
        }

        const judgeEl = document.getElementById('enableLlmJudgeToggle');
        if (judgeEl) {
            judgeEl.checked = config.enable_llm_judge === true;
        }

    } catch (e) {
        showToast('Failed to load settings', 'error');
    }
}

function toggleAzureFields(model) {
    const azureFields = document.getElementById('azureFields');
    if (model && model.startsWith('azure-')) {
        azureFields.classList.remove('hidden');
    } else {
        azureFields.classList.add('hidden');
    }
}

function initHandlers() {
    // Toggle key visibility (generic for all toggle buttons)
    document.querySelectorAll('.toggle-key').forEach(btn => {
        btn.addEventListener('click', (e) => {
            const targetId = e.target.dataset.target;
            const input = document.getElementById(targetId);
            if (input) {
                input.type = input.type === 'password' ? 'text' : 'password';
            }
        });
    });

    // Model select change
    document.getElementById('modelSelect').addEventListener('change', (e) => {
        const selectedModel = e.target.value;
        toggleAzureFields(selectedModel);
        syncAzureDeploymentFromModel(selectedModel);
    });

    // Save button
    document.getElementById('saveBtn').addEventListener('click', saveSettings);

    const judgeToggle = document.getElementById('enableLlmJudgeToggle');
    if (judgeToggle) {
        judgeToggle.addEventListener('change', async (e) => {
            try {
                const result = await saveConfig({ enable_llm_judge: e.target.checked });
                if (result.success !== false) {
                    showToast(e.target.checked ? 'LLM judge enabled' : 'LLM judge disabled', 'success');
                } else {
                    showToast(result.message || 'Save failed', 'warning');
                }
            } catch (err) {
                showToast('Failed to save judge setting', 'error');
            }
        });
    }
}

function syncAzureDeploymentFromModel(model) {
    const deploymentInput = document.getElementById('azureDeploymentInput');
    if (!deploymentInput) return;
    if (!model || !model.startsWith('azure-')) return;

    // Keep deployment name aligned with the selected Azure model by default.
    deploymentInput.value = model.replace(/^azure-/, '');
}

async function saveSettings() {
    const model = document.getElementById('modelSelect').value;
    const apiKey = document.getElementById('apiKeyInput').value;

    // Azure fields
    const azureEndpoint = document.getElementById('azureEndpointInput').value;
    const azureVersion = document.getElementById('azureVersionInput').value;
    const azureDeployment = document.getElementById('azureDeploymentInput').value;

    try {
        const payload = {
            llm_model: model,
            api_key: apiKey || undefined,
            azure_endpoint: azureEndpoint,
            azure_api_version: azureVersion,
            azure_deployment: azureDeployment
        };

        const result = await saveConfig(payload);

        if (result.success) {
            showToast(result.message || 'Settings saved', 'success');
            loadSettings(); // Refresh status
        } else {
            showToast(result.message || 'Save failed', 'warning');
        }
    } catch (e) {
        showToast('Failed to save: ' + e.message, 'error');
    }
}

/* ===== Media hierarchy catalog admin ===== */

function initHierarchyAdmin() {
    document.querySelectorAll('.hierarchy-admin-tab').forEach((btn) => {
        btn.addEventListener('click', () => {
            hierarchyAdminTab = btn.dataset.hierTab || 'enterprise';
            document.querySelectorAll('.hierarchy-admin-tab').forEach((b) => b.classList.toggle('active', b === btn));
            renderHierarchyAdmin();
        });
    });
    document.getElementById('hierarchyReloadBtn')?.addEventListener('click', () => loadHierarchyCatalog(true));
    document.getElementById('hierarchySaveCatalogBtn')?.addEventListener('click', saveHierarchyCatalog);
    const body = document.getElementById('hierarchyAdminBody');
    if (body && body.dataset.bound !== '1') {
        body.dataset.bound = '1';
        body.addEventListener('input', onHierarchyAdminInput);
        body.addEventListener('change', onHierarchyAdminInput);
        body.addEventListener('click', onHierarchyAdminClick);
    }
    loadHierarchyCatalog(false);
}

async function loadHierarchyCatalog(toastOk) {
    const body = document.getElementById('hierarchyAdminBody');
    try {
        const data = await fetchJson('/api/hierarchy/catalog');
        hierarchyCatalogState = {
            enterprise_hierarchy: data.enterprise_hierarchy || { planning: [], delivery: [], creative_detail: [] },
            cross_cut_dimensions: data.cross_cut_dimensions || [],
            standard_fields: data.standard_fields || [],
            mapping_dictionary: data.mapping_dictionary || [],
            publishers: Array.isArray(data.publishers) ? data.publishers : [],
            detection_order: data.detection_order || [],
            combined_field_delimiters: data.combined_field_delimiters,
            value_token_hints: data.value_token_hints,
            _version: data._version,
            _description: data._description,
        };
        if (!hierarchySelectedPublisherId && hierarchyCatalogState.publishers[0]) {
            hierarchySelectedPublisherId = hierarchyCatalogState.publishers[0].id;
        }
        renderHierarchyAdmin();
        if (toastOk) showToast('Hierarchy catalog reloaded', 'success');
    } catch (err) {
        if (body) body.innerHTML = `<p class="help-text">Failed to load catalog: ${escapeHtml(err.message || err)}</p>`;
        showToast(err.message || 'Failed to load hierarchies', 'error');
    }
}

function renderHierarchyAdmin() {
    const body = document.getElementById('hierarchyAdminBody');
    if (!body || !hierarchyCatalogState) return;
    if (hierarchyAdminTab === 'enterprise') {
        body.innerHTML = renderEnterpriseEditor();
    } else if (hierarchyAdminTab === 'publishers') {
        body.innerHTML = renderPublishersEditor();
    } else {
        body.innerHTML = renderCrossCutsEditor();
    }
}

function renderLevelRows(levels, options = {}) {
    const { allowNativeAlias = false, pathPrefix = '' } = options;
    const rows = (levels || []).map((lv, idx) => {
        const native = allowNativeAlias
            ? `<input class="form-input hierarchy-admin-input" data-hier-field="native_alias" data-path="${escapeAttr(pathPrefix)}" data-idx="${idx}"
                value="${escapeAttr(lv.native_alias || '')}" placeholder="Native alias (optional)">`
            : '';
        return `<div class="hierarchy-admin-level-row" data-idx="${idx}">
            <span class="hierarchy-admin-level-badge">L${escapeHtml(lv.level ?? idx + 1)}</span>
            <input class="form-input hierarchy-admin-input" data-hier-field="name" data-path="${escapeAttr(pathPrefix)}" data-idx="${idx}"
                value="${escapeAttr(lv.name || '')}" placeholder="Canonical name">
            <input class="form-input hierarchy-admin-input" data-hier-field="id" data-path="${escapeAttr(pathPrefix)}" data-idx="${idx}"
                value="${escapeAttr(lv.id || '')}" placeholder="id">
            ${native}
            <label class="hierarchy-admin-check"><input type="checkbox" data-hier-field="mandatory" data-path="${escapeAttr(pathPrefix)}" data-idx="${idx}" ${lv.mandatory ? 'checked' : ''}> Mandatory</label>
            <button type="button" class="btn-icon" data-hier-action="remove-level" data-path="${escapeAttr(pathPrefix)}" data-idx="${idx}" title="Remove">✕</button>
        </div>`;
    }).join('');
    return `${rows}
        <button type="button" class="btn-secondary btn-sm" data-hier-action="add-level" data-path="${escapeAttr(pathPrefix)}">+ Add level</button>`;
}

function renderEnterpriseEditor() {
    const ent = hierarchyCatalogState.enterprise_hierarchy || {};
    const layers = [
        { key: 'planning', title: 'Planning (business identity)' },
        { key: 'delivery', title: 'Delivery (publisher grain)' },
        { key: 'creative_detail', title: 'Creative detail' },
    ];
    return layers.map((layer) => `
        <div class="hierarchy-admin-card">
            <h3>${escapeHtml(layer.title)}</h3>
            <div class="hierarchy-admin-levels" data-layer="${escapeAttr(layer.key)}">
                ${renderLevelRows(ent[layer.key] || [], { pathPrefix: `enterprise.${layer.key}` })}
            </div>
        </div>
    `).join('');
}

function renderPublishersEditor() {
    const pubs = hierarchyCatalogState.publishers || [];
    if (!pubs.length) return '<p class="help-text">No publishers in catalog.</p>';
    const selected = pubs.find((p) => p.id === hierarchySelectedPublisherId) || pubs[0];
    hierarchySelectedPublisherId = selected.id;
    const tabs = pubs.map((p) => `
        <button type="button" class="hierarchy-pub-chip ${p.id === selected.id ? 'active' : ''}"
            data-hier-action="select-publisher" data-publisher-id="${escapeAttr(p.id)}">${escapeHtml(p.name)}</button>
    `).join('');
    const pubIdx = pubs.findIndex((p) => p.id === selected.id);
    return `
        <div class="hierarchy-pub-chips">${tabs}</div>
        <div class="hierarchy-admin-card">
            <div class="hierarchy-admin-pub-meta">
                <div class="form-group">
                    <label>Publisher name</label>
                    <input class="form-input hierarchy-admin-input" data-hier-field="pub-name" data-pub-idx="${pubIdx}" value="${escapeAttr(selected.name || '')}">
                </div>
                <div class="form-group">
                    <label>Filename aliases (comma-separated)</label>
                    <input class="form-input hierarchy-admin-input" data-hier-field="pub-aliases" data-pub-idx="${pubIdx}"
                        value="${escapeAttr((selected.aliases || []).join(', '))}">
                </div>
            </div>
            <h3>Delivery hierarchy (canonical names)</h3>
            <p class="help-text">Use standard names. Fill <em>Native alias</em> only when this platform’s file columns use a different word (e.g. Amazon “Order” → Insertion Order).</p>
            <div class="hierarchy-admin-levels">
                ${renderLevelRows(selected.hierarchy || [], { allowNativeAlias: true, pathPrefix: `publisher.${pubIdx}` })}
            </div>
        </div>
    `;
}

function renderCrossCutsEditor() {
    const dims = hierarchyCatalogState.cross_cut_dimensions || [];
    const rows = dims.map((d, idx) => `
        <div class="hierarchy-admin-level-row">
            <input class="form-input hierarchy-admin-input" data-hier-field="cc-name" data-idx="${idx}" value="${escapeAttr(d.name || '')}">
            <input class="form-input hierarchy-admin-input" data-hier-field="cc-id" data-idx="${idx}" value="${escapeAttr(d.id || '')}">
            <label class="hierarchy-admin-check"><input type="checkbox" data-hier-field="cc-mandatory" data-idx="${idx}" ${d.mandatory ? 'checked' : ''}> Mandatory</label>
            <button type="button" class="btn-icon" data-hier-action="remove-cc" data-idx="${idx}">✕</button>
        </div>
    `).join('');
    return `<div class="hierarchy-admin-card">
        <h3>Cross-cut dimensions</h3>
        <p class="help-text">Not strict parents — attributes that cut across delivery grain (channel, audience, device, …).</p>
        ${rows}
        <button type="button" class="btn-secondary btn-sm" data-hier-action="add-cc">+ Add dimension</button>
    </div>`;
}

function resolveLevelArray(path) {
    if (!hierarchyCatalogState) return null;
    if (path.startsWith('enterprise.')) {
        const key = path.slice('enterprise.'.length);
        const ent = hierarchyCatalogState.enterprise_hierarchy || (hierarchyCatalogState.enterprise_hierarchy = {});
        if (!Array.isArray(ent[key])) ent[key] = [];
        return ent[key];
    }
    if (path.startsWith('publisher.')) {
        const idx = Number(path.split('.')[1]);
        const pub = hierarchyCatalogState.publishers[idx];
        if (!pub) return null;
        if (!Array.isArray(pub.hierarchy)) pub.hierarchy = [];
        return pub.hierarchy;
    }
    return null;
}

function renumberLevels(levels) {
    levels.forEach((lv, i) => { lv.level = i + 1; });
}

function onHierarchyAdminInput(e) {
    const el = e.target;
    const field = el.dataset.hierField;
    if (!field || !hierarchyCatalogState) return;

    if (field === 'pub-name') {
        const pub = hierarchyCatalogState.publishers[Number(el.dataset.pubIdx)];
        if (pub) pub.name = el.value;
        return;
    }
    if (field === 'pub-aliases') {
        const pub = hierarchyCatalogState.publishers[Number(el.dataset.pubIdx)];
        if (pub) pub.aliases = String(el.value || '').split(',').map((s) => s.trim()).filter(Boolean);
        return;
    }
    if (field === 'cc-name' || field === 'cc-id' || field === 'cc-mandatory') {
        const dims = hierarchyCatalogState.cross_cut_dimensions;
        const idx = Number(el.dataset.idx);
        if (!dims[idx]) return;
        if (field === 'cc-name') dims[idx].name = el.value;
        if (field === 'cc-id') dims[idx].id = el.value;
        if (field === 'cc-mandatory') dims[idx].mandatory = el.checked;
        return;
    }

    const levels = resolveLevelArray(el.dataset.path || '');
    const idx = Number(el.dataset.idx);
    if (!levels || !levels[idx]) return;
    if (field === 'mandatory') levels[idx].mandatory = el.checked;
    else levels[idx][field] = el.value;
}

function onHierarchyAdminClick(e) {
    const btn = e.target.closest('[data-hier-action]');
    if (!btn || !hierarchyCatalogState) return;
    const action = btn.dataset.hierAction;

    if (action === 'select-publisher') {
        hierarchySelectedPublisherId = btn.dataset.publisherId;
        renderHierarchyAdmin();
        return;
    }
    if (action === 'add-level') {
        const levels = resolveLevelArray(btn.dataset.path || '');
        if (!levels) return;
        levels.push({
            level: levels.length + 1,
            id: `level_${levels.length + 1}`,
            name: 'New level',
            mandatory: false,
        });
        renumberLevels(levels);
        renderHierarchyAdmin();
        return;
    }
    if (action === 'remove-level') {
        const levels = resolveLevelArray(btn.dataset.path || '');
        const idx = Number(btn.dataset.idx);
        if (!levels) return;
        levels.splice(idx, 1);
        renumberLevels(levels);
        renderHierarchyAdmin();
        return;
    }
    if (action === 'add-cc') {
        hierarchyCatalogState.cross_cut_dimensions.push({
            id: 'new_dimension',
            name: 'New dimension',
            mandatory: false,
        });
        renderHierarchyAdmin();
        return;
    }
    if (action === 'remove-cc') {
        hierarchyCatalogState.cross_cut_dimensions.splice(Number(btn.dataset.idx), 1);
        renderHierarchyAdmin();
    }
}

async function saveHierarchyCatalog() {
    if (!hierarchyCatalogState) return;
    try {
        const payload = {
            _version: hierarchyCatalogState._version || '2.0',
            _description: hierarchyCatalogState._description,
            enterprise_hierarchy: hierarchyCatalogState.enterprise_hierarchy,
            cross_cut_dimensions: hierarchyCatalogState.cross_cut_dimensions,
            standard_fields: hierarchyCatalogState.standard_fields,
            mapping_dictionary: hierarchyCatalogState.mapping_dictionary,
            publishers: hierarchyCatalogState.publishers,
            detection_order: hierarchyCatalogState.detection_order
                || hierarchyCatalogState.publishers.map((p) => p.id),
            combined_field_delimiters: hierarchyCatalogState.combined_field_delimiters,
            value_token_hints: hierarchyCatalogState.value_token_hints,
        };
        const res = await fetch('/api/hierarchy/catalog', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || res.statusText);
        showToast(data.message || 'Hierarchies saved', 'success');
        await loadHierarchyCatalog(false);
    } catch (err) {
        showToast(err.message || 'Failed to save hierarchies', 'error');
    }
}
