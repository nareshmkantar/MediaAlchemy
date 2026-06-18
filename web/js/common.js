/**
 * Schema Agent - Common Utilities
 * Shared functions for all pages
 */

const API_BASE = '';

// ===== State =====
const appState = {
    currentJobId: localStorage.getItem('currentJobId') || null,
    currentSheet: localStorage.getItem('currentSheet') || null,
    currentSourceId: localStorage.getItem('currentSourceId') || null,
    apiKeySet: false
};

// ===== API Helpers =====
async function fetchJson(url, options = {}) {
    const res = await fetch(API_BASE + url, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    return res.json();
}

async function getJobStatus(jobId) {
    return fetchJson(`/api/status/${jobId}`);
}

async function patchJobUxState(jobId, body) {
    const res = await fetch(API_BASE + `/api/jobs/${encodeURIComponent(jobId)}/ux-state`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
}

async function getConfig() {
    return fetchJson('/api/config');
}

async function saveConfig(config) {
    return fetchJson('/api/config', {
        method: 'POST',
        body: JSON.stringify(config)
    });
}

// ===== Navigation =====
function navigateTo(page) {
    window.location.href = `/pages/${page}.html`;
}

function routeJobByState(jobState = {}) {
    const jid = jobState.job_id || jobState.id;
    if (jid) setCurrentJob(String(jid));

    const status = jobState.status || '';
    const requiresReview = Boolean(jobState.requires_review);

    if (status === 'awaiting_review' || status === 'awaiting_approval' || requiresReview) {
        const path = (typeof window !== 'undefined' && window.location && window.location.pathname) || '';
        if (!path.endsWith('review.html')) {
            navigateTo('review');
        }
        return;
    }
    if (status === 'processing' || status === 'queued') {
        navigateTo('processing');
        return;
    }
    if (status === 'completed') {
        navigateTo('processing');
        return;
    }
    navigateTo('debug');
}

function setCurrentJob(jobId, sheetName = null, sourceId = null) {
    appState.currentJobId = jobId;
    localStorage.setItem('currentJobId', jobId);
    if (sheetName) {
        appState.currentSheet = sheetName;
        localStorage.setItem('currentSheet', sheetName);
    }
    if (sourceId) {
        appState.currentSourceId = sourceId;
        localStorage.setItem('currentSourceId', sourceId);
    }
}

function getCurrentJob() {
    return {
        jobId: appState.currentJobId || localStorage.getItem('currentJobId'),
        sheetName: appState.currentSheet || localStorage.getItem('currentSheet'),
        sourceId: appState.currentSourceId || localStorage.getItem('currentSourceId')
    };
}

/** Enable job download buttons when paths exist (shared by Results / legacy callers). */
function refreshJobExportButtons(job) {
    const da = (job && job._download_availability) || {};
    const out = (job && job.output_files) || {};
    const art = (job && job.artifact_files) || {};
    const preList = Array.isArray(art.pre_transform_files) ? art.pre_transform_files : [];
    const excelPath =
        job.full_excel_path || job.output_file || out.excel || out.xlsx || '';

    const setBtn = (id, enabled, title) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.disabled = !enabled;
        if (title !== undefined) el.title = title;
    };

    const dataReady = da.data !== undefined ? da.data : Boolean(out.data);
    const excelReady = da.excel !== undefined ? da.excel : Boolean(excelPath);

    setBtn(
        'previewResultBtn',
        dataReady,
        dataReady ? 'Show a table preview of the exported data.' : 'No table preview until processing produces rows.'
    );
    setBtn(
        'downloadExcelBtn',
        excelReady,
        excelReady
            ? 'Final processed table as .xlsx.'
            : excelPath
              ? 'Path is recorded but the file is not on the server (export may have failed or output folder moved).'
              : 'Excel was not written (e.g. empty result or write error).'
    );

    const prePathOk = Boolean((preList.length === 1 && preList[0]) || art.pre_transform_bundle);
    const preReady = da.pre_transform !== undefined ? da.pre_transform : prePathOk;
    setBtn(
        'downloadPreTransformBtn',
        preReady,
        preReady
            ? 'Mapped + template-pruned view before planner transforms.'
            : 'No pre-transform file on disk (scoped load failed, prune removed all columns, or export error — see server logs).'
    );

    const postOnly = art.post_transform_file;
    const postDistinctPath = Boolean(postOnly && excelPath && String(postOnly) !== String(excelPath));
    const postReady = da.post_transform !== undefined ? da.post_transform : postDistinctPath;
    setBtn(
        'downloadPostTransformBtn',
        postReady,
        postReady
            ? 'Post-planner workbook (distinct from main Excel when both exist).'
            : postOnly
              ? 'Same path as main Excel or file missing on server.'
              : 'No separate post-transform workbook; use Download Excel.'
    );

    const bundlePathOk = Boolean(art.artifacts_bundle);
    const bundleReady = da.artifacts_bundle !== undefined ? da.artifacts_bundle : bundlePathOk;
    setBtn(
        'downloadArtifactsZipBtn',
        bundleReady,
        bundleReady ? 'Zip of pre-transform file(s) and post-transform if present.' : 'No artifact bundle on disk.'
    );
}

// ===== UI Helpers =====
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(() => toast.remove(), 4000);
}

function formatTime(isoString) {
    return new Date(isoString).toLocaleTimeString();
}

function formatDuration(ms) {
    if (ms < 1000) return `${ms.toFixed(0)}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
}

function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

/** Escape for HTML attribute values (title, data-*, etc.). */
function escapeAttr(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

// ===== Shared Components =====
function renderNavbar(activePage) {
    const nav = document.getElementById('mainNav');
    if (!nav) return;

    const pages = [
        { id: 'upload', icon: '📤', label: '1 · Upload' },
        { id: 'setup', icon: '🧭', label: '2 · Schema Mapping' },
        { id: 'review', icon: '👁️', label: '3 · Review' },
        { id: 'debug', icon: '📥', label: '4 · Results' },
        { id: 'evals', icon: '⚖️', label: '5 · Evals' },
        { id: 'tests', icon: '🧪', label: '6 · Tests' },
        { id: 'processing', icon: '⏳', label: '7 · Console' },
    ];

    nav.innerHTML = `
        <div class="nav-brand">
            <span class="brand-icon">🔍</span>
            <span class="brand-text">Schema Agent 🚀</span>
        </div>
        <div class="nav-links">
            ${pages.map(p => `
                <a href="/pages/${p.id}.html" class="nav-link ${activePage === p.id ? 'active' : ''}" id="nav-link-${p.id}">
                    <span class="nav-icon">${p.icon}</span>
                    <span>${p.label}</span>
                    ${p.id === 'review' ? '<span class="nav-badge hidden" id="reviewBadge">0</span>' : ''}
                </a>
            `).join('')}
        </div>
        <div class="nav-trailing">
            <div class="nav-status" id="navStatus">
                <span class="status-dot"></span>
                <span class="status-text">Loading...</span>
            </div>
            <a href="/pages/settings.html" class="nav-link nav-link--icon-only nav-link--settings-end ${activePage === 'settings' ? 'active' : ''}" id="nav-link-settings" aria-label="Settings" title="Settings">
                <span class="nav-icon" aria-hidden="true">⚙️</span>
            </a>
        </div>
    `;

    // Update API status
    updateNavStatus();
}

async function updateNavStatus() {
    try {
        const config = await getConfig();
        const statusEl = document.getElementById('navStatus');
        if (statusEl) {
            const dot = statusEl.querySelector('.status-dot');
            const text = statusEl.querySelector('.status-text');
            if (config.api_key_set) {
                dot.classList.add('connected');
                text.textContent = config.llm_model || 'Connected';
            } else {
                dot.classList.remove('connected');
                text.textContent = 'API Not Set';
            }
        }

        // Update Review Badge
        const reviewData = await fetchJson('/api/review/count');
        const badge = document.getElementById('reviewBadge');
        if (badge) {
            if (reviewData.pending_count > 0) {
                badge.textContent = reviewData.pending_count;
                badge.classList.remove('hidden');
            } else {
                badge.classList.add('hidden');
            }
        }
    } catch (e) {
        console.error('Failed to update nav status:', e);
    }
}

// ===== Debug Tree Helpers (shared) =====
function buildEventTree(events) {
    if (!events || events.length === 0) return [];
    events.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

    const nodeMap = {};
    const roots = [];

    events.forEach(event => {
        nodeMap[event.event_id] = { ...event, children: [] };
    });

    events.forEach(event => {
        const node = nodeMap[event.event_id];
        if (event.parent_event_id && nodeMap[event.parent_event_id]) {
            nodeMap[event.parent_event_id].children.push(node);
        } else {
            roots.push(node);
        }
    });

    return roots;
}

// ===== Init =====
document.addEventListener('DOMContentLoaded', () => {
    // Auto-render navbar if element exists
    const nav = document.getElementById('mainNav');
    if (nav && nav.dataset.page) {
        renderNavbar(nav.dataset.page);
    }
});
