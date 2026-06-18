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
};

/** Last `sources` array passed to `renderSourceSelector`. */
let lastRenderedSources = [];

/** Show compact “Jump to” dropdown when there are many sources (all pills still render). */
const SOURCE_DROPDOWN_THRESHOLD = 6;

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
    setupSchemaZone();
    initUploadSourcePanelDelegation();

    document.getElementById('mappingBtn')?.addEventListener('click', () => goToSetup());
    refreshUploadUxStepper();
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
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        const [file] = Array.from(e.dataTransfer.files || []);
        if (file) handleSchemaFile(file);
    });
    fileInput.addEventListener('change', (e) => {
        const [file] = Array.from(e.target.files || []);
        if (file) handleSchemaFile(file);
    });
    removeFile?.addEventListener('click', (e) => {
        e.stopPropagation();
        uploadState.selectedSchemaFile = null;
        fileInfo.classList.add('hidden');
        dropZone.style.display = 'block';
        updateProcessButton();
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
    } catch (e) {
        console.error(e);
        if (sourceList) {
            sourceList.innerHTML = '<p style="font-size: 11px; color: var(--text-secondary);">Upload stopped — fix the issue and try again.</p>';
        }
    }
}

function handleSchemaFile(file) {
    uploadState.selectedSchemaFile = file;
    document.getElementById('fileNameSchema').textContent = file.name;
    document.getElementById('fileSizeSchema').textContent = formatFileSize(file.size);
    document.getElementById('fileInfoSchema').classList.remove('hidden');
    document.getElementById('dropZoneSchema').style.display = 'none';
    updateProcessButton();
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
    const hasTargetTemplate = Boolean(uploadState.selectedSchemaFile);
    const canContinue = hasDataSource && hasTargetTemplate;
    const mappingBtn = document.getElementById('mappingBtn');
    if (mappingBtn) {
        mappingBtn.disabled = !canContinue;
        mappingBtn.title = canContinue
            ? 'Open Guided Setup'
            : 'Upload at least one data file and one JSON target scope template first.';
    }
}

async function ensureSchemaUploaded(jobId) {
    if (!uploadState.selectedSchemaFile) return;
    const schemaForm = new FormData();
    schemaForm.append('file', uploadState.selectedSchemaFile);
    schemaForm.append('type', 'schema');
    schemaForm.append('job_id', jobId);
    const schemaRes = await fetch('/api/upload', { method: 'POST', body: schemaForm });
    if (!schemaRes.ok) throw new Error('Schema upload failed');
}

async function goToSetup() {
    if (!uploadState.jobId || !uploadState.selectedSourceId) {
        showToast('Upload at least one data file first.', 'warning');
        return;
    }
    if (!uploadState.selectedSchemaFile) {
        showToast('Upload a JSON target scope template before starting Guided Setup.', 'warning');
        return;
    }
    const mappingBtn = document.getElementById('mappingBtn');
    mappingBtn.disabled = true;
    mappingBtn.innerHTML = '<span class="spinner"></span> Preparing Setup...';

    try {
        await ensureSchemaUploaded(uploadState.jobId);
        setCurrentJob(uploadState.jobId, uploadState.selectedSheet || null, uploadState.selectedSourceId || null);
        showToast('Guided setup workspace ready.', 'success');
        const sourceQuery = uploadState.selectedSourceId ? `&source_id=${encodeURIComponent(uploadState.selectedSourceId)}` : '';
        const sheetQuery = uploadState.selectedSheet ? `&sheet_name=${encodeURIComponent(uploadState.selectedSheet)}` : '';
        window.location.href = `setup.html?job_id=${uploadState.jobId}${sheetQuery}${sourceQuery}`;
    } catch (e) {
        showToast(`Error: ${e.message}`, 'error');
        mappingBtn.disabled = false;
        mappingBtn.innerHTML = '<span>🧭</span> Start Guided Setup';
    }
}

async function loadRecentJobs() {
    try {
        const payload = await fetchJson('/api/jobs');
        const jobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
        const list = document.getElementById('jobsList');

        if (!list) return;

        if (jobs.length === 0) {
            list.innerHTML = '<p class="empty-state">No recent jobs</p>';
            return;
        }

        list.innerHTML = jobs.slice(0, 5).map(job => `
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