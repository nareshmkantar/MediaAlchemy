/**
 * Schema Agent - Mapping Page
 * Column classification and MCWT creation
 */

// ===== State =====
let currentMapping = [];
const elements = {
    mappingWorkspace: document.getElementById('mappingWorkspace'),
    colCount: document.getElementById('colCount'),
    keepCount: document.getElementById('keepCount'),
    discardCount: document.getElementById('discardCount'),
    saveMappingBtn: document.getElementById('saveMappingBtn')
};

// ===== Initialization =====
document.addEventListener('DOMContentLoaded', () => {
    const urlParams = new URLSearchParams(window.location.search);
    const currentJob = getCurrentJob();
    const jobId = urlParams.get('job_id') || (currentJob && currentJob.jobId);
    const sheetName = urlParams.get('sheet_name') || (currentJob && currentJob.sheetName);

    if (jobId) {
        proposeMapping(jobId, sheetName);
    } else {
        showNoJobState();
    }

    if (elements.saveMappingBtn) {
        elements.saveMappingBtn.addEventListener('click', saveMapping);
    }
});

function showNoJobState() {
    const workspace = document.getElementById('mappingWorkspace');
    if (!workspace) return;
    workspace.innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">📤</div>
            <h3>No Active File Found</h3>
            <p>You haven't uploaded a data file yet, or your previous session has expired.</p>
            <p style="margin-top: 8px; font-size: 13px; color: var(--text-secondary);">
                Please go to the <strong>Upload</strong> page to select a file before starting Schema Mapping.
            </p>
            <button class="btn-primary" style="margin-top: 20px;" onclick="navigateTo('upload')">
                Go to Uploads
            </button>
        </div>
    `;
}

async function proposeMapping(jobId, sheetName) {
    if (!elements.mappingWorkspace) return;

    elements.mappingWorkspace.innerHTML = `
        <div class="loading-state">
            <div class="loading-spinner"></div>
            <p>Analyzing file schema${sheetName ? ` (Sheet: ${sheetName})` : ''}... </p>
        </div>
    `;

    try {
        let url = `/api/mapping/propose/${jobId}`;
        if (sheetName) {
            url += `?sheet_name=${encodeURIComponent(sheetName)}`;
        }

        const data = await fetchJson(url);
        if (data.error) {
            throw new Error(data.error);
        }
        if (Array.isArray(data.mapping)) {
            currentMapping = data.mapping;
            renderMappingWorkspace(data.mapping);
            updateMappingStats();

            // Render Sheet Bar if sheets available
            if (data.sheets && data.sheets.length > 1) {
                renderSheetBar(data.sheets, data.current_sheet || data.sheets[0]);
            }
        } else {
            throw new Error(data.error || 'Failed to generate proposal');
        }
    } catch (e) {
        console.error('[SIA] Mapping error:', e);

        if (e.message.includes('404')) {
            showNoJobState();
            return;
        }

        elements.mappingWorkspace.innerHTML = `
            <div class="error-state">
                <div class="error-icon">❌</div>
                <h3>Failed to load mapping</h3>
                <p>${e.message}</p>
                <div style="margin-top: 16px; display: flex; gap: 10px; justify-content: center;">
                    <button class="btn-secondary" onclick="location.reload()">Retry</button>
                    <button class="btn-primary" onclick="navigateTo('upload')">Go to Uploads</button>
                </div>
            </div>
        `;
    }
}

function renderSheetBar(sheets, currentSheet) {
    const bar = document.getElementById('sheetSelectionBar');
    const list = document.getElementById('sheetTabList');
    if (!bar || !list) return;

    bar.classList.remove('hidden');
    list.innerHTML = sheets.map(s => `
        <div class="sheet-tab ${s === currentSheet ? 'active' : ''}" onclick="switchSheet('${s}')">${s}</div>
    `).join('');
}

window.switchSheet = function (sheetName) {
    const currentJob = getCurrentJob();
    const jobId = currentJob && currentJob.jobId;
    if (!jobId) return;

    // Update URL without reload
    const url = new URL(window.location);
    url.searchParams.set('sheet_name', sheetName);
    window.history.pushState({}, '', url);

    // Re-propose for new sheet
    proposeMapping(jobId, sheetName);
};

function updateMappingStats() {
    if (!elements.colCount) return;

    const total = currentMapping.length;
    const keep = currentMapping.filter(m => m.decision === 'Keep').length;
    const discard = total - keep;

    elements.colCount.textContent = total;
    elements.keepCount.textContent = keep;
    elements.discardCount.textContent = discard;
}

function renderMappingWorkspace(mapping) {
    if (!elements.mappingWorkspace) return;

    if (mapping.length === 0) {
        elements.mappingWorkspace.innerHTML = '<div class="empty-state">No columns found. Check selected sheet/layout demarcation and retry.</div>';
        return;
    }

    let html = `
        <table class="mapping-table">
            <thead>
                <tr>
                    <th>Source Column</th>
                    <th>Classification</th>
                    <th>Action</th>
                    <th>LLM Reasoning</th>
                </tr>
            </thead>
            <tbody>
    `;

    mapping.forEach((item, idx) => {
        const isKeep = item.decision === 'Keep';
        html += `
            <tr class="mapping-row">
                <td class="col-name">${item.column_name}</td>
                <td>
                    <select class="class-select" onchange="updateClassification(${idx}, this.value)">
                        <option value="Delivery Metrics" ${item.classification === 'Delivery Metrics' ? 'selected' : ''}>Delivery Metrics</option>
                        <option value="Cost Metrics" ${item.classification === 'Cost Metrics' ? 'selected' : ''}>Cost Metrics</option>
                        <option value="Engagement Primitives" ${item.classification === 'Engagement Primitives' ? 'selected' : ''}>Engagement Primitives</option>
                        <option value="State / Governance" ${item.classification === 'State / Governance' ? 'selected' : ''}>State / Governance</option>
                        <option value="Structural Metadata" ${item.classification === 'Structural Metadata' ? 'selected' : ''}>Structural Metadata</option>
                        <option value="Derived" ${item.classification === 'Derived' ? 'selected' : ''}>Derived (Discard)</option>
                    </select>
                </td>
                <td>
                    <div class="decision-toggle">
                        <button class="btn-toggle keep ${isKeep ? 'active' : ''}" onclick="updateDecision(${idx}, 'Keep')">Keep</button>
                        <button class="btn-toggle discard ${!isKeep ? 'active' : ''}" onclick="updateDecision(${idx}, 'Discard')">Discard</button>
                    </div>
                </td>
                <td>
                    <div class="reason-tooltip" title="${item.reasoning}">
                        <span class="reason-text">${item.reasoning.substring(0, 60)}${item.reasoning.length > 60 ? '...' : ''}</span>
                    </div>
                </td>
            </tr>
        `;
    });

    html += `</tbody></table>`;
    elements.mappingWorkspace.innerHTML = html;
}

window.updateClassification = function (idx, value) {
    currentMapping[idx].classification = value;
    // Auto-update decision based on class if it's "Derived"
    if (value === 'Derived') {
        currentMapping[idx].decision = 'Discard';
    } else {
        currentMapping[idx].decision = 'Keep';
    }
    renderMappingWorkspace(currentMapping);
    updateMappingStats();
};

window.updateDecision = function (idx, decision) {
    currentMapping[idx].decision = decision;
    renderMappingWorkspace(currentMapping);
    updateMappingStats();
};

async function saveMapping() {
    const currentJob = getCurrentJob();
    const jobId = currentJob && currentJob.jobId;
    if (!jobId) return;

    const btn = elements.saveMappingBtn;
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const data = await fetchJson(`/api/mapping/submit/${jobId}`, {
            method: 'POST',
            body: JSON.stringify({ mapping: currentMapping })
        });

        if (data.success) {
            showToast('Schema Mapping finalized! Proceeding to transformation...', 'success');
            setTimeout(() => {
                navigateTo('processing');
            }, 1000);
        } else {
            throw new Error(data.error || 'Failed to save mapping');
        }
    } catch (e) {
        console.error('[SIA] Save mapping error:', e);
        showToast(`Error: ${e.message}`, 'error');
        btn.disabled = false;
        btn.textContent = 'Finalize Mapping';
    }
}
