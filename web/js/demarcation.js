/**
 * Schema Agent - Demarcation Page
 * Identifying logical regions in the spreadsheet grid
 */

// ===== State =====
let currentBlocks = [];
let jobId = null;

const elements = {
    workspace: document.getElementById('demarcationWorkspace'),
    blockCount: document.getElementById('blockCount'),
    finalizeBtn: document.getElementById('finalizeDemarcationBtn')
};

// ===== Initialization =====
document.addEventListener('DOMContentLoaded', async () => {
    const urlParams = new URLSearchParams(window.location.search);
    const currentJob = getCurrentJob();
    jobId = urlParams.get('job_id') || (currentJob && currentJob.jobId);
    const sheetName = urlParams.get('sheet_name') || (currentJob && currentJob.sheetName);

    if (jobId) {
        proposeDemarcation(jobId, sheetName);
    } else {
        showNoJobState();
    }

    if (elements.finalizeBtn) {
        elements.finalizeBtn.addEventListener('click', submitDemarcation);
    }
});

async function proposeDemarcation(jobId, sheetName) {
    if (!elements.workspace) return;

    elements.workspace.innerHTML = `
        <div class="loading-state">
            <div class="loading-spinner"></div>
            <p>Scanning grid for logical regions... </p>
        </div>
    `;

    try {
        let url = `/api/demarcation/propose/${jobId}`;
        if (sheetName) {
            url += `?sheet_name=${encodeURIComponent(sheetName)}`;
        }

        const data = await fetchJson(url);

        if (data.proposal && data.proposal.blocks) {
            currentBlocks = data.proposal.blocks;
            renderBlocks(data.proposal.blocks);
            updateStats();
        } else {
            throw new Error('No blocks identified by AI');
        }
    } catch (e) {
        console.error('[SIA] Demarcation error:', e);
        elements.workspace.innerHTML = `
            <div class="error-state">
                <div class="error-icon">❌</div>
                <h3>Failed to identify regions</h3>
                <p>${e.message}</p>
                <button class="btn-secondary" onclick="location.reload()">Retry</button>
            </div>
        `;
    }
}

function renderBlocks(blocks) {
    if (!blocks || blocks.length === 0) {
        elements.workspace.innerHTML = '<div class="empty-state">No logical blocks detected.</div>';
        return;
    }

    elements.workspace.innerHTML = '';

    blocks.forEach((block, idx) => {
        const coords = block.coordinates;
        const blockEl = document.createElement('div');
        blockEl.className = 'demarcation-block-card';

        // Define category options with their display names
        const categories = {
            'Main Data': 'Main Data Table',
            'Metadata': 'Report Headers / Metadata',
            'Footnote': 'Footnotes / Source Info',
            'Comment': 'Floating Comment',
            'Noise': 'Noise / Decorative'
        };

        blockEl.innerHTML = `
            <div class="block-info">
                <div class="block-header">
                    <span class="block-name">${block.name}</span>
                    <span class="block-range">${coords.start_row}:${coords.end_row}, ${coords.start_col}:${coords.end_col}</span>
                </div>
                <p class="block-summary">${block.summary}</p>
            </div>
            
            <div class="block-actions">
                <div class="action-row">
                    <label>Category</label>
                    <select onchange="updateBlockCategory(${idx}, this.value)">
                        ${Object.entries(categories).map(([val, label]) => `
                            <option value="${val}" ${block.category === val ? 'selected' : ''}>${label}</option>
                        `).join('')}
                    </select>
                </div>
                
                <div class="action-row">
                    <label>Human Context / Instructions</label>
                    <input type="text" placeholder="e.g. 'This contains Org ID'" 
                           value="${block.comment || ''}"
                           onchange="updateBlockComment(${idx}, this.value)">
                </div>
            </div>
        `;

        elements.workspace.appendChild(blockEl);
    });
}

function updateStats() {
    if (elements.blockCount) {
        elements.blockCount.textContent = currentBlocks.length;
    }
}

window.updateBlockCategory = function (idx, category) {
    currentBlocks[idx].category = category;
};

window.updateBlockComment = function (idx, comment) {
    currentBlocks[idx].comment = comment;
};

async function submitDemarcation() {
    if (!jobId) return;

    elements.finalizeBtn.disabled = true;
    elements.finalizeBtn.textContent = 'Saving...';

    try {
        const response = await fetchJson(`/api/demarcation/submit/${jobId}`, {
            method: 'POST',
            body: JSON.stringify({ blocks: currentBlocks })
        });

        if (response.success) {
            showToast('Demarcation finalized!', 'success');
            // Navigate to mapping with same job/sheet
            const urlParams = new URLSearchParams(window.location.search);
            const sheet = urlParams.get('sheet_name');
            let nextUrl = `mapping.html?job_id=${jobId}`;
            if (sheet) nextUrl += `&sheet_name=${encodeURIComponent(sheet)}`;

            setTimeout(() => {
                window.location.href = nextUrl;
            }, 800);
        } else {
            throw new Error(response.error || 'Submission failed');
        }
    } catch (e) {
        console.error('[SIA] Submission error:', e);
        showToast('Error: ' + e.message, 'error');
        elements.finalizeBtn.disabled = false;
        elements.finalizeBtn.textContent = 'Finalize Demarcation';
    }
}

function showNoJobState() {
    if (!elements.workspace) return;
    elements.workspace.innerHTML = `
        <div class="empty-state">
            <div class="empty-icon">📤</div>
            <h3>No Active File</h3>
            <p>Please upload a file first.</p>
            <button class="btn-primary" onclick="navigateTo('upload')">Go to Upload</button>
        </div>
    `;
}
