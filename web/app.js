/**
 * Structure Inference Agent - Web Application
 * Frontend JavaScript for handling file uploads, processing, and results display
 */

// ===== State Management =====
const state = {
    currentJobId: null,
    apiKeySet: false
};

// ===== DOM Elements =====
const elements = {
    // Navigation
    navItems: document.querySelectorAll('.nav-item'),
    tabContents: document.querySelectorAll('.tab-content'),

    // Upload
    uploadZonesContainer: document.querySelector('.upload-zones-container'),
    dropZoneExcel: document.getElementById('dropZoneExcel'),
    dropZoneJson: document.getElementById('dropZoneJson'),
    fileInput: document.getElementById('fileInput'),
    fileInfo: document.getElementById('fileInfo'),
    fileName: document.getElementById('fileName'),
    fileSize: document.getElementById('fileSize'),
    removeFile: document.getElementById('removeFile'),
    processBtn: document.getElementById('processBtn'),

    // JSON Template
    templateInput: document.getElementById('templateInput'),
    templateFileCard: document.getElementById('templateFileCard'),
    templateName: document.getElementById('templateName'),
    removeTemplate: document.getElementById('removeTemplate'),

    // Results Tabs
    issueCountBadge: document.getElementById('issueCountBadge'),
    issuesTableContainer: document.getElementById('issuesTableContainer'),

    // Processing
    progressFill: document.getElementById('progressFill'),
    stepsTimeline: document.getElementById('stepsTimeline'),

    // Results
    resultsSummary: document.getElementById('resultsSummary'),
    statConfidence: document.getElementById('statConfidence'),
    statFields: document.getElementById('statFields'),
    statRows: document.getElementById('statRows'),
    statSheets: document.getElementById('statSheets'),
    schemaViewer: document.getElementById('schemaViewer'),
    dataTableContainer: document.getElementById('dataTableContainer'),
    traceViewer: document.getElementById('traceViewer'),
    downloadButtons: document.getElementById('downloadButtons'),
    downloadSchema: document.getElementById('downloadSchema'),
    downloadData: document.getElementById('downloadData'),
    innerTabs: document.querySelectorAll('.tab-btn'),
    innerContents: document.querySelectorAll('.inner-content'),
    nodeDetailsPanel: document.getElementById('nodeDetailsPanel'),

    // Settings
    apiKeyInput: document.getElementById('apiKeyInput'),
    toggleApiKey: document.getElementById('toggleApiKey'),
    saveApiKey: document.getElementById('saveApiKey'),
    apiKeyStatus: document.getElementById('apiKeyStatus'),
    apiStatus: document.getElementById('apiStatus'),
    modelSelect: document.getElementById('modelSelect'),
    customModelGroup: document.getElementById('customModelGroup'),
    customModelInput: document.getElementById('customModelInput'),
    debugModeToggle: document.getElementById('debugModeToggle'),
    enableLlmJudgeToggle: document.getElementById('enableLlmJudgeToggle'),

    // Review
    reviewBadge: document.getElementById('reviewBadge'),
    pendingCount: document.getElementById('pendingCount'),
    approvedCount: document.getElementById('approvedCount'),
    rejectedCount: document.getElementById('rejectedCount'),
    reviewList: document.getElementById('reviewList'),
    reviewDetails: document.getElementById('reviewDetails'),

    // Mapping
    mappingWorkspace: document.getElementById('mappingWorkspace'),
    colCount: document.getElementById('colCount'),
    keepCount: document.getElementById('keepCount'),
    discardCount: document.getElementById('discardCount'),
    saveMappingBtn: document.getElementById('saveMappingBtn'),

    // Toast
    toastContainer: document.getElementById('toastContainer'),

    // Tests
    runAllTests: document.getElementById('runAllTests'),
    testGrid: document.getElementById('testGrid'),
    testLogContainer: document.getElementById('testLogContainer'),
    testLog: document.getElementById('testLog'),
    closeTestLog: document.getElementById('closeTestLog')
};

// ===== Initialization =====
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();
    initUpload();
    initSettings();
    initInnerTabs();
    initSidebar();
    initReview(); // New Review module
    initTests(); // New Tests module
    initMapping(); // New Mapping module
    loadConfig();
    updateReviewBadge(); // Initial fetch
});

// ===== Sidebar =====
function initSidebar() {
    const sidebarToggle = document.getElementById('sidebarToggle');
    const sidebar = document.querySelector('.sidebar');
    const mainContent = document.querySelector('.main-content');

    if (sidebarToggle && sidebar && mainContent) {
        sidebarToggle.addEventListener('click', () => {
            sidebar.classList.toggle('collapsed');
            mainContent.classList.toggle('expanded');
            sidebarToggle.classList.toggle('active');

            if (sidebar.classList.contains('collapsed')) {
                sidebarToggle.title = "Show Sidebar";
            } else {
                sidebarToggle.title = "Hide Sidebar";
            }
        });
    }
}


// ===== Navigation =====
function initNavigation() {
    elements.navItems.forEach(item => {
        item.addEventListener('click', () => {
            const tabName = item.dataset.tab;
            switchTab(tabName);
        });
    });
}

function switchTab(tabName) {
    // Update nav
    elements.navItems.forEach(item => {
        item.classList.toggle('active', item.dataset.tab === tabName);
    });

    // Update content
    elements.tabContents.forEach(content => {
        content.classList.toggle('active', content.id === `tab-${tabName}`);
    });

    // Refresh data if needed
    if (tabName === 'results' && state.currentJobId) {
        fetchResults();
    } else if (tabName === 'review') {
        fetchPendingReviews();
    } else if (tabName === 'mapping') {
        if (state.currentJobId) {
            proposeMapping(state.currentJobId);
        }
    } else if (tabName === 'tests') {
        fetchTests();
    }
}

// ===== Upload =====
function initUpload() {
    const dropZoneExcel = elements.dropZoneExcel;
    const dropZoneJson = elements.dropZoneJson;

    // --- Excel Zone Logic ---
    if (dropZoneExcel) {
        dropZoneExcel.addEventListener('click', () => elements.fileInput.click());

        dropZoneExcel.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZoneExcel.classList.add('dragover');
        });

        dropZoneExcel.addEventListener('dragleave', () => {
            dropZoneExcel.classList.remove('dragover');
        });

        dropZoneExcel.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZoneExcel.classList.remove('dragover');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                handleFile(files[0]);
            }
        });
    }

    // --- JSON Zone Logic ---
    if (dropZoneJson) {
        dropZoneJson.addEventListener('click', () => elements.templateInput.click());

        dropZoneJson.addEventListener('dragover', (e) => {
            e.preventDefault();
            dropZoneJson.classList.add('dragover');
        });

        dropZoneJson.addEventListener('dragleave', () => {
            dropZoneJson.classList.remove('dragover');
        });

        dropZoneJson.addEventListener('drop', (e) => {
            e.preventDefault();
            dropZoneJson.classList.remove('dragover');
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                handleTemplateFile(files[0]);
            }
        });
    }

    // File inputs change
    elements.fileInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleFile(e.target.files[0]);
        }
    });

    elements.templateInput.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            handleTemplateFile(e.target.files[0]);
        }
    });

    // Remove buttons
    elements.removeFile.addEventListener('click', (e) => {
        e.stopPropagation();
        resetExcel();
    });

    elements.removeTemplate.addEventListener('click', (e) => {
        e.stopPropagation();
        resetTemplate();
    });

    // Process button
    elements.processBtn.addEventListener('click', () => {
        processFile();
    });
}

function handleFile(file) {
    const validExtensions = ['.xlsx', '.xls', '.csv'];
    const ext = '.' + file.name.split('.').pop().toLowerCase();

    if (!validExtensions.includes(ext)) {
        showToast('Invalid file type. Please upload .xlsx, .xls, or .csv', 'error');
        return;
    }

    elements.fileName.textContent = file.name;
    elements.fileSize.textContent = formatFileSize(file.size);
    elements.fileInfo.classList.remove('hidden');

    // Hide upload zones if primary file is selected
    if (elements.uploadZonesContainer) {
        elements.uploadZonesContainer.classList.add('hidden');
    }

    state.selectedFile = file;
}

function handleTemplateFile(file) {
    if (!file.name.toLowerCase().endsWith('.json')) {
        showToast('Please upload a .json file for the template', 'error');
        return;
    }

    elements.templateName.textContent = file.name;
    elements.templateFileCard.classList.remove('hidden');
    elements.fileInfo.classList.remove('hidden');

    state.selectedTemplate = file;
    showToast('JSON template attached', 'info');
}

function resetExcel() {
    state.selectedFile = null;
    elements.fileInput.value = '';
    elements.fileName.textContent = '-';

    // If template is also gone, show upload zones back
    if (!state.selectedTemplate) {
        elements.fileInfo.classList.add('hidden');
        if (elements.uploadZonesContainer) {
            elements.uploadZonesContainer.classList.remove('hidden');
        }
    } else {
        // Just hide the excel card if possible (though currently it's hardcoded in HTML)
        // For simplicity, if they remove Excel, we might want to let them re-upload
        // Let's show the upload zones again so they can pick another Excel
        if (elements.uploadZonesContainer) {
            elements.uploadZonesContainer.classList.remove('hidden');
        }
    }
}

function resetTemplate() {
    state.selectedTemplate = null;
    elements.templateInput.value = '';
    elements.templateFileCard.classList.add('hidden');

    // If primary file is also gone, hide info
    if (!state.selectedFile) {
        elements.fileInfo.classList.add('hidden');
        if (elements.uploadZonesContainer) {
            elements.uploadZonesContainer.classList.remove('hidden');
        }
    }
}

async function processFile() {
    if (!state.selectedFile) {
        showToast('Please select a file first', 'warning');
        return;
    }

    if (!state.apiKeySet) {
        showToast('Please set your API key in Settings first', 'warning');
        switchTab('settings');
        return;
    }

    elements.processBtn.disabled = true;
    elements.processBtn.textContent = 'Uploading...';
    console.log('[SIA] Starting file processing:', state.selectedFile.name);

    try {
        // Upload file
        console.log('[SIA] Uploading file(s)...');
        const formData = new FormData();
        formData.append('file', state.selectedFile);

        // Optional JSON template
        if (state.selectedTemplate) {
            formData.append('template', state.selectedTemplate);
            console.log('[SIA] Including JSON template:', state.selectedTemplate.name);
        }

        let uploadRes;
        try {
            uploadRes = await fetch('/api/upload', {
                method: 'POST',
                body: formData
            });
        } catch (networkError) {
            console.error('[SIA] Network error during upload:', networkError);
            throw new Error(`Network error during upload: ${networkError.message}. Is the server running?`);
        }

        if (!uploadRes.ok) {
            console.error('[SIA] Upload response not OK:', uploadRes.status, uploadRes.statusText);
            throw new Error(`Upload failed: HTTP ${uploadRes.status} ${uploadRes.statusText}`);
        }

        const uploadData = await uploadRes.json();
        console.log('[SIA] Upload response:', uploadData);

        if (!uploadData.success) {
            throw new Error(uploadData.error || 'Upload failed');
        }

        state.currentJobId = uploadData.job_id;
        console.log('[SIA] Job ID:', state.currentJobId);
        showToast('File uploaded! Starting processing...', 'success');

        // Switch to processing tab
        switchTab('processing');
        resetProcessingView();
        clearPreviousResults(); // NEW: Clear stale results from Results and Debug tabs

        // Start processing
        elements.processBtn.textContent = 'Processing...';
        console.log('[SIA] Starting processing for job:', state.currentJobId);

        let processRes;
        try {
            processRes = await fetch(`/api/process/${state.currentJobId}`, {
                method: 'POST'
            });
        } catch (networkError) {
            console.error('[SIA] Network error during processing:', networkError);
            // Try to fetch status to see what happened
            await fetchResults();
            throw new Error(`Network error during processing: ${networkError.message}. Check console for details.`);
        }

        console.log('[SIA] Process response status:', processRes.status);

        if (!processRes.ok) {
            const errorText = await processRes.text();
            console.error('[SIA] Process response error:', errorText);
            try {
                const errorJson = JSON.parse(errorText);
                throw new Error(errorJson.error || `Processing failed: HTTP ${processRes.status}`);
            } catch (e) {
                throw new Error(`Processing failed: HTTP ${processRes.status} - ${errorText.substring(0, 200)}`);
            }
        }

        const processData = await processRes.json();
        console.log('[SIA] Process result:', processData);

        if (processData.error) {
            throw new Error(processData.error);
        }

        if (processData.async && processData.status === 'processing') {
            showToast('Processing started — watch the Processing tab for live steps.', 'info');
            const terminal = (s) =>
                ['completed', 'error', 'awaiting_review', 'awaiting_approval'].includes(s);
            for (let i = 0; i < 7200 && state.currentJobId; i++) {
                await new Promise((r) => setTimeout(r, 1000));
                const stRes = await fetch(`/api/status/${state.currentJobId}`);
                if (!stRes.ok) break;
                const st = await stRes.json();
                updateProcessingView(st);
                if (terminal(st.status)) {
                    await fetchResults();
                    if (st.status === 'completed') {
                        const conf = (st.trace && st.trace.overall_confidence) || st.overall_confidence || 0;
                        showToast(`Processing complete! Confidence: ${(Number(conf) * 100).toFixed(0)}%`, 'success');
                        switchTab('results');
                    } else if (st.status === 'awaiting_review' || st.status === 'awaiting_approval') {
                        showToast('Processing paused for review.', 'warning');
                        switchTab('review');
                    } else if (st.status === 'error') {
                        showToast(st.error_details || 'Processing failed', 'error');
                    }
                    updateReviewBadge();
                    break;
                }
            }
            return;
        }

        // Fetch final results (synchronous process response — legacy path)
        await fetchResults();
        showToast(`Processing complete! Confidence: ${(processData.overall_confidence * 100).toFixed(0)}%`, 'success');

        // Switch to results
        switchTab('results');

    } catch (error) {
        console.error('[SIA] Processing error:', error);
        showToast(`Error: ${error.message}`, 'error');
        // Try to fetch any partial results
        if (state.currentJobId) {
            await fetchResults();
        }
    } finally {
        elements.processBtn.disabled = false;
        elements.processBtn.innerHTML = '<span class="btn-icon-left">🚀</span> Process File';
        updateReviewBadge(); // Check if new item was added to review queue
    }
}

// ===== Processing View =====
function resetProcessingView() {
    console.log('[SIA] Resetting processing view');

    // Reset log timeline
    const timeline = document.getElementById('stepsTimeline');
    if (timeline) {
        timeline.innerHTML = '<div class="log-empty">Starting the schema inference pipeline... 🔎</div>';
    }
}

/** Load job snapshot from the server and refresh Results / Processing views. */
async function fetchResults() {
    if (!state.currentJobId) return;
    try {
        const res = await fetch(`/api/status/${state.currentJobId}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.error) return;
        updateProcessingView(data);
        if (data.status === 'completed' && data.schema) {
            updateResultsView(data);
        }
    } catch (e) {
        console.error('[SIA] fetchResults error:', e);
    }
}

function clearPreviousResults() {
    console.log('[SIA] Clearing previous results');

    // Clear Schema Viewer
    if (elements.schemaViewer) {
        elements.schemaViewer.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">📋</div>
                <h3>Processing...</h3>
                <p>Schema will appear here when ready</p>
            </div>
        `;
    }

    // Clear Data Table
    if (elements.dataTableContainer) {
        elements.dataTableContainer.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">📊</div>
                <h3>Processing...</h3>
                <p>Data preview will appear here when ready</p>
            </div>
        `;
    }

    // Clear Trace Viewer
    if (elements.traceViewer) {
        elements.traceViewer.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">🕷️</div>
                <h3>Processing...</h3>
                <p>Trace will appear here when ready</p>
            </div>
        `;
    }

    // Hide download buttons
    if (elements.downloadButtons) {
        elements.downloadButtons.classList.add('hidden');
    }
}

function updateProcessingView(data) {
    const timeline = elements.stepsTimeline || document.getElementById('stepsTimeline');
    if (!timeline) return;

    const steps = data.steps || [];
    const esc = (s) =>
        String(s ?? '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');

    const rows = steps.map((step) => {
        const ts = step.timestamp ? new Date(step.timestamp).toLocaleTimeString() : '';
        const lvl = step.step === 'Error' ? 'ERROR' : 'INFO';
        const msg = step.message + (step.confidence ? ` (${(step.confidence * 100).toFixed(0)}% conf)` : '');
        const line = [ts, lvl, step.step, msg].filter(Boolean).join(' | ');
        return `<div class="log-entry log-entry--terminal ${step.step === 'Error' ? 'error' : ''}">${esc(line)}</div>`;
    });

    if (data.status === 'awaiting_approval') {
        rows.push(
            `<div class="log-entry log-entry--terminal">${esc('Processing paused for approval of destructive operations.')}</div>`
        );
        updateReviewBadge();
    }

    if (data.status === 'completed') {
        rows.push(`<div class="log-entry log-entry--terminal success">${esc('Processing complete.')}</div>`);
    }

    timeline.innerHTML =
        rows.join('') ||
        '<div class="log-empty">Starting the schema inference pipeline... 🔎</div>';
}

function updateResultsView(data) {
    // Store data in state for other functions (like downloadFile)
    state.lastJobResults = data;

    // Show download buttons
    elements.downloadButtons.classList.remove('hidden');

    const schema = data.schema || {};
    const trace = data.trace || {};

    // Render schema
    renderSchema(schema);

    // Render data preview
    renderDataPreview(data.data_preview || [], data.data_preview_column_order);

    // Render trace (now in Debug tab)
    renderTrace(trace);

    // Render issues if any
    const issues = trace.verifier_issues || [];
    renderIssuesTable(issues);

    // Setup download buttons
    elements.downloadSchema.onclick = () => downloadFile('schema');
    elements.downloadData.onclick = () => downloadFile('data');
}

function renderIssuesTable(issues) {
    if (!elements.issuesTableContainer) return;

    if (!issues || issues.length === 0) {
        elements.issuesTableContainer.innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">✅</div>
                <h3>No Issues Found</h3>
                <p>Verifier has not detected any structural issues</p>
            </div>
        `;
        if (elements.issueCountBadge) elements.issueCountBadge.classList.add('hidden');
        return;
    }

    // Update badge
    if (elements.issueCountBadge) {
        elements.issueCountBadge.textContent = issues.length;
        elements.issueCountBadge.classList.remove('hidden');
    }

    let html = `
        <table class="issues-table">
            <thead>
                <tr>
                    <th style="width: 120px;">Type</th>
                    <th>Description</th>
                    <th style="width: 180px;">Suggested Tool</th>
                </tr>
            </thead>
            <tbody>
    `;

    issues.forEach(issue => {
        const type = (issue.issue_type || 'warning').toLowerCase();
        const typeClass = type === 'critical' ? 'type-critical' : (type === 'info' ? 'type-info' : 'type-warning');

        html += `
            <tr>
                <td><span class="issue-type-tag ${typeClass}">${type}</span></td>
                <td>${issue.description || 'N/A'}</td>
                <td>${issue.suggested_tool ? `<span class="suggested-tool">${issue.suggested_tool}</span>` : '<span style="color: grey;">N/A</span>'}</td>
            </tr>
        `;
    });

    html += `
            </tbody>
        </table>
    `;

    elements.issuesTableContainer.innerHTML = html;
}

function renderSchema(schema) {
    const fields = schema.fields || [];

    if (fields.length === 0) {
        elements.schemaViewer.innerHTML = '<div class="empty-state"><div class="empty-icon">📋</div><h3>No Schema</h3></div>';
        return;
    }

    elements.schemaViewer.innerHTML = `
        <div class="schema-header" style="margin-bottom: 16px;">
            <h4 style="color: var(--primary-light);">${schema.schema_name || 'Inferred Schema'}</h4>
            <p style="color: var(--text-secondary); font-size: 13px;">
                Created: ${new Date(schema.created_at).toLocaleString()}
            </p>
        </div>
        ${fields.map(field => `
            <div class="field-item">
                <span class="field-name">${field.name}</span>
                <span class="field-badge ${field.role}">${field.role}</span>
                <span class="field-type">${field.type}</span>
                <span class="field-confidence">${(field.confidence * 100).toFixed(0)}%</span>
            </div>
        `).join('')}
    `;
}

function renderDataPreview(data, columnOrder) {
    if (!data || data.length === 0) {
        elements.dataTableContainer.innerHTML = '<div class="empty-state"><div class="empty-icon">📊</div><h3>No Data</h3></div>';
        return;
    }

    const columns = (Array.isArray(columnOrder) && columnOrder.length)
        ? columnOrder
        : Object.keys(data[0]);

    elements.dataTableContainer.innerHTML = `
        <table class="data-table">
            <thead>
                <tr>${columns.map(col => `<th>${col}</th>`).join('')}</tr>
            </thead>
            <tbody>
                ${data.slice(0, 20).map(row => `
                    <tr>${columns.map(col => `<td title="${row[col] ?? ''}">${row[col] ?? ''}</td>`).join('')}</tr>
                `).join('')}
            </tbody>
        </table>
        <p style="color: var(--text-secondary); font-size: 12px; margin-top: 12px;">
            Showing first ${Math.min(data.length, 20)} of ${data.length} rows
        </p>
    `;
}

function renderTrace(trace) {
    let html = `<div class="trace-section">
        <h4>📋 Processing Steps</h4>
        <div class="trace-steps-list">
            ${(trace.steps || []).map(step => `
                <div class="trace-step-item">
                    <span class="step-name">${step.step}</span>
                    <span class="step-msg">${step.message}</span>
                    ${step.confidence ? `<span class="step-conf">${(step.confidence * 100).toFixed(0)}%</span>` : ''}
                </div>
            `).join('')}
        </div>
    </div>`;

    if (trace.debug_events && trace.debug_events.length > 0) {
        const roots = buildEventTree(trace.debug_events);

        // Store events map for quick access
        window.debugEventMap = {};
        trace.debug_events.forEach(e => window.debugEventMap[e.event_id] = e);

        html += `<div class="trace-section debug-events">
            <h4>🔧 Debug Trace (Tree View)</h4>
            <div class="debug-tree-container">
                ${roots.map(node => renderEventNode(node)).join('')}
            </div>
            <div class="debug-legend">
                <span class="legend-item"><span class="dot type-span"></span> Span</span>
                <span class="legend-item"><span class="dot type-llm"></span> LLM Call</span>
                <span class="legend-item"><span class="dot type-function"></span> Function</span>
                <span class="legend-item"><span class="dot type-data_load"></span> Data Load</span>
            </div>
        </div>`;
    }

    if (trace.errors && trace.errors.length > 0) {
        html += `<div class="trace-section errors">
            <h4>❌ Errors</h4>
            <pre>${JSON.stringify(trace.errors, null, 2)}</pre>
        </div>`;
    }

    if (trace.warnings && trace.warnings.length > 0) {
        html += `<div class="trace-section warnings">
            <h4>⚠️ Warnings</h4>
            <pre>${JSON.stringify(trace.warnings, null, 2)}</pre>
        </div>`;
    }

    elements.traceViewer.innerHTML = html;

    // Add event listeners for toggling
    // Add event listeners for toggling
    elements.traceViewer.querySelectorAll('.tree-toggle').forEach(toggle => {
        toggle.addEventListener('click', (e) => {
            e.stopPropagation();
            const node = toggle.closest('.tree-node');
            node.classList.toggle('collapsed');
            toggle.textContent = node.classList.contains('collapsed') ? '▶' : '▼';
        });
    });

    // Add event listeners for node selection
    elements.traceViewer.querySelectorAll('.tree-node .node-header').forEach(header => {
        header.addEventListener('click', (e) => {
            // Avoid triggering when clicking toggle
            if (e.target.classList.contains('tree-toggle')) return;

            e.stopPropagation();
            const nodeEl = header.closest('.tree-node');
            const eventId = nodeEl.dataset.eventId;
            selectNode(eventId, nodeEl);
        });
    });
}

function buildEventTree(events) {
    // Sort by timestamp first
    events.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));

    const nodeMap = {};
    const roots = [];

    // Create nodes
    events.forEach(event => {
        nodeMap[event.event_id] = { ...event, children: [] };
    });

    // Link parents/children
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

function renderEventNode(node) {
    const hasChildren = node.children && node.children.length > 0;
    const isSpan = node.process_type === 'span';
    const duration = node.duration_ms ? `${node.duration_ms.toFixed(0)}ms` : '';

    let content = '';

    // Header
    const toggleIcon = hasChildren ? '▼' : '●';
    const typeClass = `type-${node.process_type}`;

    content += `
        <div class="tree-node ${isSpan ? 'node-span' : 'node-leaf'} ${hasChildren ? '' : 'no-children'}" data-event-id="${node.event_id}">
            <div class="node-header">
                <span class="tree-toggle ${hasChildren ? 'interactive' : ''}">${toggleIcon}</span>
                <span class="node-type-badge ${typeClass}">${node.process_type}</span>
                <span class="node-title">${node.module}.${node.operation}</span>
                ${duration ? `<span class="node-duration">${duration}</span>` : ''}
            </div>
            
            <div class="node-body">
                <div class="node-details">
                    ${node.input_summary ? `<div class="detail-row"><span class="label">In:</span> ${node.input_summary}</div>` : ''}
                    ${node.output_summary ? `<div class="detail-row"><span class="label">Out:</span> ${node.output_summary}</div>` : ''}
                    
                    ${node.prompt ? `
                        <details class="prompt-details">
                            <summary>Show Prompt</summary>
                            <pre>${node.prompt}</pre>
                        </details>
                    ` : ''}
                    
                    ${node.response ? `
                        <details class="response-details">
                            <summary>Show Response</summary>
                            <pre>${node.response}</pre>
                        </details>
                    ` : ''}
                </div>
                
                ${hasChildren ? `
                    <div class="node-children">
                        ${node.children.map(child => renderEventNode(child)).join('')}
                    </div>
                ` : ''}
            </div>
        </div>
    `;

    return content;
}

function selectNode(eventId, nodeEl) {
    if (!window.debugEventMap || !window.debugEventMap[eventId]) return;

    // UI Selection state
    document.querySelectorAll('.tree-node').forEach(n => n.classList.remove('selected'));
    if (nodeEl) nodeEl.classList.add('selected');

    const event = window.debugEventMap[eventId];
    renderNodeDetails(event);
}

function renderNodeDetails(node) {
    const panels = elements.nodeDetailsPanel;
    if (!panels) return;

    // Helper for formatting JSON/Code
    const formatCode = (content) => {
        if (typeof content === 'object') return JSON.stringify(content, null, 2);
        return content;
    };

    let html = `
        <div class="detail-section">
            <div class="detail-header">
                <span>📍 ${node.module}.${node.operation}</span>
                <span class="node-type-badge type-${node.process_type}">${node.process_type}</span>
            </div>
            <div class="detail-content">
                <p><strong>Duration:</strong> ${node.duration_ms ? node.duration_ms.toFixed(0) + 'ms' : 'N/A'}</p>
                <p><strong>Timestamp:</strong> ${new Date(node.timestamp).toLocaleTimeString()}</p>
            </div>
        </div>
    `;

    // 1. Context / Input
    if (node.input_data || node.input_summary) {
        html += `
            <div class="detail-section">
                <div class="detail-header">📥 Context & Input</div>
                <div class="detail-content">
                    ${node.input_summary ? `<p><strong>Summary:</strong> ${node.input_summary}</p>` : ''}
                    ${node.input_data ? `<div class="detail-pre">${formatCode(node.input_data)}</div>` : ''}
                </div>
            </div>
        `;
    }

    // 2. Prompt (LLM Only)
    if (node.prompt) {
        html += `
            <div class="detail-section">
                <div class="detail-header">🤖 LLM Prompt</div>
                <div class="detail-content">
                     <div class="detail-pre" style="white-space: pre-wrap;">${node.prompt}</div>
                </div>
            </div>
        `;
    }

    // 3. Output / Action
    if (node.output_data || node.response || node.output_summary) {
        html += `
            <div class="detail-section">
                <div class="detail-header">📤 Output / Action</div>
                <div class="detail-content">
                    ${node.output_summary ? `<p><strong>Summary:</strong> ${node.output_summary}</p>` : ''}
                    ${node.response ? `
                        <p><strong>Raw LLM Response:</strong></p>
                        <div class="detail-pre">${node.response}</div>
                    ` : ''}
                    ${node.output_data ? `
                        <p><strong>Structured Output:</strong></p>
                        <div class="detail-pre">${formatCode(node.output_data)}</div>
                    ` : ''}
                </div>
            </div>
        `;
    }

    panels.innerHTML = html;
}

async function downloadFile(type) {
    if (!state.currentJobId) return;

    try {
        let downloadUrl = `/api/download/${state.currentJobId}/${type}`;
        let downloadName = type === 'schema' ? 'schema.json' : 'data.csv';

        // Enhanced Logic: Use exported Excel if available for 'data' type
        if (type === 'data' && state.lastJobResults && state.lastJobResults.trace && state.lastJobResults.trace.output_file) {
            const filePath = state.lastJobResults.trace.output_file;
            // The path is server-side (e.g., outputs/sia_output_abc.xlsx)
            // We just need the filename for the API
            const filename = filePath.split(/[\\/]/).pop();
            downloadUrl = `/api/download/${filename}`;
            downloadName = filename;
            console.log('[SIA] Using exported Excel file for download:', filename);
        }

        const res = await fetch(downloadUrl);
        if (!res.ok) throw new Error(`Download failed: ${res.statusText}`);

        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = downloadName;
        document.body.appendChild(a);
        a.click();
        a.remove();
        window.URL.revokeObjectURL(url);

        showToast(`Download started: ${downloadName}`, 'success');
    } catch (error) {
        console.error('[SIA] Download error:', error);
        showToast(`Download failed: ${error.message}`, 'error');
    }
}

// ===== Inner Tabs =====
function initInnerTabs() {
    elements.innerTabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const tabName = tab.dataset.innerTab;

            elements.innerTabs.forEach(t => t.classList.toggle('active', t === tab));
            elements.innerContents.forEach(c => c.classList.toggle('active', c.id === `inner-${tabName}`));
        });
    });
}

// ===== Settings =====
function initSettings() {
    // Toggle API key visibility
    elements.toggleApiKey.addEventListener('click', () => {
        const input = elements.apiKeyInput;
        const isPassword = input.type === 'password';
        input.type = isPassword ? 'text' : 'password';
        elements.toggleApiKey.textContent = isPassword ? '🙈' : '👁️';
    });

    // Model selection
    if (elements.modelSelect) {
        elements.modelSelect.addEventListener('change', (e) => {
            if (e.target.value === 'custom') {
                elements.customModelGroup.classList.remove('hidden');
            } else {
                elements.customModelGroup.classList.add('hidden');
            }
        });
    }

    // Save API key & Settings
    elements.saveApiKey.addEventListener('click', saveSettings);

    // Output format
    document.querySelectorAll('input[name="outputFormat"]').forEach(radio => {
        radio.addEventListener('change', async (e) => {
            await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ output_format: e.target.value })
            });
            showToast('Output format saved', 'success');
        });
    });

    // Debug mode toggle
    if (elements.debugModeToggle) {
        elements.debugModeToggle.addEventListener('change', async (e) => {
            await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ debug_enabled: e.target.checked })
            });
            showToast(e.target.checked ? 'Debug mode enabled' : 'Debug mode disabled', 'success');
        });
    }

    if (elements.enableLlmJudgeToggle) {
        elements.enableLlmJudgeToggle.addEventListener('change', async (e) => {
            await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enable_llm_judge: e.target.checked })
            });
            showToast(e.target.checked ? 'LLM judge enabled' : 'LLM judge disabled', 'success');
        });
    }
}

async function loadConfig() {
    try {
        const res = await fetch('/api/config');
        const config = await res.json();

        state.apiKeySet = config.api_key_set;
        updateApiStatus(config.api_key_set);

        if (config.output_format) {
            const radio = document.querySelector(`input[name="outputFormat"][value="${config.output_format}"]`);
            if (radio) radio.checked = true;
        }

        // Load model setting
        if (config.llm_model && elements.modelSelect) {
            const options = Array.from(elements.modelSelect.options).map(o => o.value);
            if (options.includes(config.llm_model)) {
                elements.modelSelect.value = config.llm_model;
            } else {
                elements.modelSelect.value = 'custom';
                elements.customModelGroup.classList.remove('hidden');
                elements.customModelInput.value = config.llm_model;
            }
        }

        // Load debug mode setting
        if (elements.debugModeToggle) {
            elements.debugModeToggle.checked = config.debug_enabled || false;
        }

        if (elements.enableLlmJudgeToggle) {
            elements.enableLlmJudgeToggle.checked = config.enable_llm_judge === true;
        }

    } catch (error) {
        console.error('Error loading config:', error);
    }
}

async function saveSettings() {
    const apiKey = elements.apiKeyInput.value.trim();
    let model = elements.modelSelect ? elements.modelSelect.value : 'gemini-3-flash-preview';

    if (model === 'custom') {
        model = elements.customModelInput.value.trim();
        if (!model) {
            showToast('Please enter a custom model name', 'warning');
            return;
        }
    }

    // Prepare payload
    const payload = { llm_model: model };
    if (apiKey) {
        payload.api_key = apiKey;
    }

    elements.saveApiKey.disabled = true;
    elements.saveApiKey.textContent = 'Saving...';

    try {
        const res = await fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        const data = await res.json();

        elements.apiKeyStatus.textContent = data.message;
        elements.apiKeyStatus.className = 'api-key-status ' + (data.success ? 'success' : 'error');

        if (data.success) {
            if (apiKey) {
                state.apiKeySet = true;
                updateApiStatus(true);
                elements.apiKeyInput.value = ''; // Clear input for security
            }
            showToast('Settings saved successfully!', 'success');
        } else {
            showToast(data.message, 'warning');
        }

    } catch (error) {
        elements.apiKeyStatus.textContent = 'Connection failed';
        elements.apiKeyStatus.className = 'api-key-status error';
        showToast('Failed to save settings', 'error');
    } finally {
        elements.saveApiKey.disabled = false;
        elements.saveApiKey.textContent = 'Save Settings';
    }
}

function updateApiStatus(connected) {
    const statusEl = elements.apiStatus;
    const dot = statusEl.querySelector('.status-dot');
    const text = statusEl.querySelector('.status-text');

    dot.classList.toggle('connected', connected);
    text.textContent = connected ? 'API Connected' : 'API Not Set';
}

// ===== Utilities =====
function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function showToast(message, type = 'info') {
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;

    elements.toastContainer.appendChild(toast);

    setTimeout(() => {
        toast.style.animation = 'slideInRight 0.3s ease reverse';
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}
// ===== HITL Review Module =====
function initReview() {
    // Stats are local to the session for now
    state.reviewStats = {
        approved: 0,
        rejected: 0
    };
}

async function updateReviewBadge() {
    try {
        const res = await fetch('/api/review/count');
        const data = await res.json();

        if (elements.reviewBadge) {
            if (data.pending_count > 0) {
                elements.reviewBadge.textContent = data.pending_count;
                elements.reviewBadge.classList.remove('hidden');
            } else {
                elements.reviewBadge.classList.add('hidden');
            }
        }

        if (elements.pendingCount) {
            elements.pendingCount.textContent = data.pending_count;
        }
    } catch (e) {
        console.error('[SIA] Error fetching review count:', e);
    }
}

async function fetchPendingReviews() {
    try {
        const res = await fetch('/api/review/pending');
        const data = await res.json();
        renderReviewList(data.items || []);
        updateReviewStats();
    } catch (e) {
        console.error('[SIA] Error fetching pending reviews:', e);
        showToast('Failed to fetch review queue', 'error');
    }
}

function updateReviewStats() {
    if (elements.approvedCount) elements.approvedCount.textContent = state.reviewStats.approved;
    if (elements.rejectedCount) elements.rejectedCount.textContent = state.reviewStats.rejected;
}

function renderReviewList(items) {
    const tableBody = document.getElementById('reviewTableBody');
    if (!tableBody) return;

    if (items.length === 0) {
        tableBody.innerHTML = `
            <tr class="empty-row">
                <td colspan="6">
                    <div class="empty-state">
                        <h3>No Pending Reviews</h3>
                        <p>All items have been reviewed or approved</p>
                    </div>
                </td>
            </tr>
        `;
        return;
    }

    tableBody.innerHTML = items.flatMap(item => {
        const rows = [];

        // 1. Add Destructive Previews
        if (item.previews && item.previews.length > 0) {
            item.previews.forEach((preview, idx) => {
                rows.push(`
                    <tr class="review-row type-destructive">
                        <td><span class="status-badge risk-high">High Risk</span></td>
                        <td>${item.filename}</td>
                        <td>${preview.tool_name}</td>
                        <td>
                            <div class="confidence-wrapper">
                                <div class="confidence-bar critical"></div>
                                <span>Critical</span>
                            </div>
                        </td>
                        <td class="details-cell">${preview.reason || preview.tool_description} (${preview.rows_to_delete || 0} rows)</td>
                        <td>
                            <div class="action-buttons">
                                <button class="btn-action approve" onclick="handleReviewAction('${item.job_id}', 'approve')">Approve</button>
                                <button class="btn-action reject" onclick="handleReviewAction('${item.job_id}', 'reject')">Reject</button>
                            </div>
                        </td>
                    </tr>
                `);
            });
        }

        // 2. Add Low Confidence Items
        if (item.low_confidence_items && item.low_confidence_items.length > 0) {
            item.low_confidence_items.forEach(low_conf => {
                const confScore = (low_conf.confidence * 100).toFixed(0);
                const confClass = low_conf.confidence > 0.6 ? 'medium' : 'low';

                rows.push(`
                    <tr class="review-row type-confidence">
                        <td><span class="status-badge review">Review</span></td>
                        <td>${item.filename}</td>
                        <td>${low_conf.tool || 'Planner'}</td>
                        <td>
                            <div class="confidence-wrapper">
                                <div class="confidence-bar ${confClass}" style="width: ${confScore}%"></div>
                                <span>${confScore}%</span>
                            </div>
                        </td>
                        <td class="details-cell">${low_conf.details}</td>
                        <td>
                            <div class="action-buttons">
                                <button class="btn-action approve" onclick="handleReviewAction('${item.job_id}', 'approve')">Approve</button>
                                <button class="btn-action reject" onclick="handleReviewAction('${item.job_id}', 'reject')">Reject</button>
                            </div>
                        </td>
                    </tr>
                `);
            });
        }

        // Fallback for items with overall low confidence
        if (rows.length === 0) {
            const confScore = (item.confidence * 100).toFixed(0);
            const confClass = item.confidence > 0.6 ? 'medium' : 'low';
            const stepName = item.current_step || 'Orchestrator';

            rows.push(`
                <tr class="review-row type-confidence">
                    <td><span class="status-badge review">Review</span></td>
                    <td>${item.filename}</td>
                    <td>${stepName}</td>
                    <td>
                        <div class="confidence-wrapper">
                            <div class="confidence-bar ${confClass}" style="width: ${confScore}%"></div>
                            <span>${confScore}%</span>
                        </div>
                    </td>
                    <td class="details-cell">${item.reason || 'Manual review required for this job.'}</td>
                    <td>
                        <div class="action-buttons">
                            <button class="btn-action approve" onclick="handleReviewAction('${item.job_id}', 'approve')">Approve</button>
                            <button class="btn-action reject" onclick="handleReviewAction('${item.job_id}', 'reject')">Reject</button>
                        </div>
                    </td>
                </tr>
            `);
        }

        return rows;
    }).join('');
}

async function showReviewDetail(jobId) {
    // UI state
    document.querySelectorAll('.review-item').forEach(el => el.classList.remove('active'));
    document.getElementById(`review-item-${jobId}`)?.classList.add('active');

    elements.reviewDetails.innerHTML = '<div class="loading">Loading details...</div>';

    try {
        const res = await fetch(`/api/review/${jobId}`);
        const data = await res.json();

        if (data.type === 'destructive_approval') {
            renderDestructiveApprovalContent(data);
        } else {
            renderReviewDetailContent(data);
        }
    } catch (e) {
        console.error('[SIA] Error fetching review details:', e);
        elements.reviewDetails.innerHTML = '<div class="error">Failed to load details</div>';
    }
}

function renderDestructiveApprovalContent(data) {
    const previews = data.previews || [];
    const confClass = data.confidence > 0.9 ? 'confidence-high' :
        (data.confidence > 0.6 ? 'confidence-medium' : 'confidence-low');

    elements.reviewDetails.innerHTML = `
        <div class="review-detail-header">
            <h2>${data.filename}</h2>
            <div style="display: flex; gap: 8px; align-items: center;">
                <span class="confidence-tag type-destructive" style="font-size: 14px; padding: 4px 10px;">
                    🛑 Approval Required
                </span>
                <span style="color: var(--text-secondary); font-size: 12px;">Job ID: ${data.job_id}</span>
            </div>
        </div>

        <div class="warning-box" style="margin-bottom: 20px; padding: 16px; border-radius: var(--radius-md); background: rgba(239, 68, 68, 0.1); border: 1px solid var(--danger);">
            <p style="color: var(--danger); font-weight: 600; margin-bottom: 4px;">⚠️ Destructive Operations Detected</p>
            <p style="font-size: 13px; color: var(--text-secondary);">The agent wants to perform the following operations that will delete or modify data. Please review each and approve if correct.</p>
        </div>

        <div class="deletion-previews-list">
            ${previews.map((preview, idx) => {
        const columns = preview.sample_deleted_data?.length > 0 ? Object.keys(preview.sample_deleted_data[0]) : [];
        const rowsCount = preview.rows_to_delete?.length || 0;
        const colsCount = preview.columns_to_delete?.length || 0;

        return `
                <div class="preview-section deletion-preview-card" style="border: 1px solid var(--border); border-radius: var(--radius-md); padding: 16px; margin-bottom: 16px;">
                    <div style="display: flex; align-items: flex-start; gap: 12px; margin-bottom: 12px;">
                        <input type="checkbox" class="tool-approval-checkbox" data-index="${idx}" checked style="margin-top: 4px; width: 18px; height: 18px;">
                        <div style="flex: 1;">
                            <h4 style="margin: 0; color: var(--primary-light);">${preview.tool_name}</h4>
                            <p style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">${preview.reason || preview.tool_description}</p>
                            
                            <div style="display: flex; gap: 12px; margin-bottom: 12px;">
                                <div class="mini-stat">
                                    <span style="font-weight: 700; color: var(--danger);">${rowsCount}</span> 
                                    <span style="font-size: 11px; color: var(--text-muted);">Rows to delete</span>
                                </div>
                                ${colsCount > 0 ? `
                                <div class="mini-stat">
                                    <span style="font-weight: 700; color: var(--danger);">${colsCount}</span> 
                                    <span style="font-size: 11px; color: var(--text-muted);">Cols to delete</span>
                                </div>` : ''}
                                <button class="btn-secondary" onclick="exportDeletionPreview('${data.job_id}', ${idx})" style="padding: 4px 8px; font-size: 11px; margin-left: auto;">
                                    <span>📥</span> Export Full Data
                                </button>
                            </div>

                            ${preview.sample_deleted_data ? `
                            <div class="mini-data-preview" style="max-height: 150px;">
                                <table style="font-size: 11px;">
                                    <thead>
                                        <tr>${columns.slice(0, 5).map(c => `<th>${c}</th>`).join('')}${columns.length > 5 ? '<th>...</th>' : ''}</tr>
                                    </thead>
                                    <tbody>
                                        ${preview.sample_deleted_data.slice(0, 3).map(row => `
                                            <tr>
                                                ${columns.slice(0, 5).map(c => `<td>${row[c] ?? ''}</td>`).join('')}
                                                ${columns.length > 5 ? '<td>...</td>' : ''}
                                            </tr>
                                        `).join('')}
                                    </tbody>
                                </table>
                                ${preview.sample_deleted_data.length > 3 ? `<p style="font-size: 10px; color: var(--text-muted); text-align: center; margin-top: 4px;">... and ${preview.sample_deleted_data.length - 3} more sample rows ...</p>` : ''}
                            </div>
                            ` : ''}
                        </div>
                    </div>
                </div>
                `;
    }).join('')}
        </div>

        <div class="review-actions" style="margin-top: 24px; border-top: 1px solid var(--border); padding-top: 20px;">
            <button class="btn-primary btn-approve" onclick="handleApproveDeletions('${data.job_id}')" style="background: linear-gradient(135deg, var(--success), #059669);">
                Approve Selected & Resume
            </button>
            <button class="btn-secondary btn-reject" onclick="handleRejectDeletions('${data.job_id}')">
                Reject All & Next Step
            </button>
            <button class="btn-secondary" onclick="switchTab('debug'); state.currentJobId='${data.job_id}'; fetchResults();" style="margin-left: auto;">
                Inspect Trace
            </button>
        </div>
    `;
}

async function exportDeletionPreview(jobId, index) {
    window.open(`/api/jobs/${jobId}/deletion-preview/${index}/export`, '_blank');
}

async function handleApproveDeletions(jobId) {
    const btn = event.currentTarget;
    const originalContent = btn.innerHTML;

    // Get approved indices
    const checkboxes = document.querySelectorAll('.tool-approval-checkbox');
    const approvedIndices = [];
    checkboxes.forEach(cb => {
        if (cb.checked) {
            approvedIndices.push(parseInt(cb.dataset.index));
        }
    });

    if (approvedIndices.length === 0) {
        if (!confirm('No operations selected. This will effectively reject all deletions. Continue?')) {
            return;
        }
    }

    btn.disabled = true;
    btn.textContent = 'Processing...';

    try {
        const res = await fetch(`/api/jobs/${jobId}/approve-deletions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ approved_indices: approvedIndices })
        });
        const result = await res.json();

        if (result.success) {
            showToast('Deletions approved. Resuming processing...', 'success');

            // Resume processing
            await fetch(`/api/jobs/${jobId}/resume`, { method: 'POST' });

            // Refresh
            fetchPendingReviews();
            updateReviewBadge();
            switchTab('processing');
        } else {
            throw new Error(result.error || 'Approval failed');
        }
    } catch (e) {
        console.error('[SIA] Error approving deletions:', e);
        showToast(`Failed to approve: ${e.message}`, 'error');
        btn.disabled = false;
        btn.innerHTML = originalContent;
    }
}

async function handleRejectDeletions(jobId) {
    if (!confirm('Are you sure you want to reject ALL deletions? The agent will continue but skip these steps.')) {
        return;
    }

    const btn = event.currentTarget;
    const originalContent = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Processing...';

    try {
        const res = await fetch(`/api/jobs/${jobId}/reject-deletions`, { method: 'POST' });
        const result = await res.json();

        if (result.success) {
            showToast('Deletions rejected. Resuming processing...', 'success');

            // Resume processing
            await fetch(`/api/jobs/${jobId}/resume`, { method: 'POST' });

            // Refresh
            fetchPendingReviews();
            updateReviewBadge();
            switchTab('processing');
        } else {
            throw new Error(result.error || 'Rejection failed');
        }
    } catch (e) {
        console.error('[SIA] Error rejecting deletions:', e);
        showToast(`Failed to reject: ${e.message}`, 'error');
        btn.disabled = false;
        btn.innerHTML = originalContent;
    }
}

function renderReviewDetailContent(data) {
    const fields = data.schema_preview?.fields || [];
    const previewData = data.data_preview || [];
    const columns = previewData.length > 0 ? Object.keys(previewData[0]) : [];

    const confClass = data.confidence > 0.9 ? 'confidence-high' :
        (data.confidence > 0.6 ? 'confidence-medium' : 'confidence-low');

    elements.reviewDetails.innerHTML = `
        <div class="review-detail-header">
            <h2>${data.filename}</h2>
            <div style="display: flex; gap: 8px; align-items: center;">
                <span class="confidence-tag ${confClass}" style="font-size: 14px; padding: 4px 10px;">
                    ${(data.confidence * 100).toFixed(0)}% Confidence
                </span>
                <span style="color: var(--text-secondary); font-size: 12px;">Job ID: ${data.job_id}</span>
            </div>
        </div>

        <div class="reason-box">
            <p><strong>Review Reason:</strong> ${data.reason}</p>
        </div>

        <div class="preview-section">
            <h4>📋 Inferred Schema Preview</h4>
            <div class="field-list" style="max-height: 200px; overflow-y: auto;">
                ${fields.map(f => `
                    <div class="field-item" style="padding: 8px 12px; margin-bottom: 4px; font-size: 12px;">
                        <span style="font-weight: 600; width: 120px; display: inline-block;">${f.name}</span>
                        <span class="field-badge ${f.role}" style="font-size: 10px; padding: 1px 6px;">${f.role}</span>
                        <span style="color: var(--text-muted); margin-left: auto;">${f.type}</span>
                    </div>
                `).join('')}
            </div>
        </div>

        <div class="preview-section">
            <h4>📊 Data Preview (Top 5 rows)</h4>
            <div class="mini-data-preview">
                <table>
                    <thead>
                        <tr>${columns.map(c => `<th>${c}</th>`).join('')}</tr>
                    </thead>
                    <tbody>
                        ${previewData.map(row => `
                            <tr>${columns.map(c => `<td>${row[c] ?? ''}</td>`).join('')}</tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="preview-section">
            <h4>🔍 Processing Stats</h4>
            <div class="trace-summary-grid">
                <div class="trace-summary-item">
                    <span class="trace-summary-val">${data.trace_summary.steps}</span>
                    <span class="trace-summary-lab">Steps</span>
                </div>
                <div class="trace-summary-item">
                    <span class="trace-summary-val" style="color: var(--danger)">${data.trace_summary.errors}</span>
                    <span class="trace-summary-lab">Errors</span>
                </div>
                <div class="trace-summary-item">
                    <span class="trace-summary-val" style="color: var(--warning)">${data.trace_summary.warnings}</span>
                    <span class="trace-summary-lab">Warnings</span>
                </div>
            </div>
        </div>

        <div class="review-actions">
            <button class="btn-primary btn-approve" onclick="handleReviewAction('${data.job_id}', 'approve')">
                Approve Results
            </button>
            <button class="btn-secondary btn-reject" onclick="handleReviewAction('${data.job_id}', 'reject')">
                Reject & Discard
            </button>
            <button class="btn-secondary" onclick="switchTab('debug'); state.currentJobId='${data.job_id}'; fetchResults();" style="margin-left: auto;">
                Inspect Trace
            </button>
        </div>
    `;
}

async function handleReviewAction(jobId, action) {
    const btn = event.currentTarget;
    const originalContent = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = 'Processing...';

    try {
        const res = await fetch(`/api/review/${jobId}/${action}`, {
            method: 'POST'
        });
        const result = await res.json();

        if (result.success) {
            showToast(`Item ${action === 'approve' ? 'approved' : 'rejected'} successfully`, 'success');

            // Update local stats
            if (action === 'approve') state.reviewStats.approved++;
            else state.reviewStats.rejected++;

            // Refresh list
            fetchPendingReviews();
            updateReviewBadge();

            // Reset detail view
            elements.reviewDetails.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">👈</div>
                    <h3>Select an Item</h3>
                    <p>Click on a pending item to review</p>
                </div>
            `;
        } else {
            throw new Error(result.error || 'Action failed');
        }
    } catch (e) {
        console.error(`[SIA] Error in ${action}:`, e);
        showToast(`Failed to ${action} item: ${e.message}`, 'error');
        btn.disabled = false;
        btn.innerHTML = originalContent;
    }
}

// ===== Tests Module =====
function initTests() {
    if (elements.runAllTests) {
        elements.runAllTests.addEventListener('click', runAllTests);
    }
    if (elements.closeTestLog) {
        elements.closeTestLog.addEventListener('click', () => {
            elements.testLogContainer.classList.add('hidden');
        });
    }
}

async function fetchTests() {
    try {
        const res = await fetch('/api/tests/list');
        const data = await res.json();
        if (data.success) {
            renderTestCards(data.tests);
        }
    } catch (error) {
        console.error('Error fetching tests:', error);
    }
}

function renderTestCards(tests) {
    elements.testGrid.innerHTML = tests.map(test => `
        <div class="test-card" id="test-card-${test.id}">
            <div class="test-header">
                <h3 class="test-title">${test.name}</h3>
                <span class="test-badge pending">Pending</span>
            </div>
            <p class="test-description">${test.description}</p>
            <div class="test-meta">
                <div class="test-meta-item">📁 ${test.file_path.split('/').pop()}</div>
                ${test.expected_tools.length ? `<div class="test-meta-item">🛠️ ${test.expected_tools.join(', ')}</div>` : ''}
            </div>
            <div class="test-footer">
                <div class="test-result-summary">-</div>
                <button class="btn-secondary btn-small run-single-test" data-id="${test.id}">Run Test</button>
            </div>
        </div>
    `).join('');

    // Add event listeners to buttons
    document.querySelectorAll('.run-single-test').forEach(btn => {
        btn.addEventListener('click', () => runSingleTest(btn.dataset.id));
    });
}

async function runSingleTest(testId) {
    const card = document.getElementById(`test-card-${testId}`);
    const badge = card.querySelector('.test-badge');
    const resultSummary = card.querySelector('.test-result-summary');
    const btn = card.querySelector('.run-single-test');

    badge.className = 'test-badge running';
    badge.textContent = 'Running';
    btn.disabled = true;

    // Clear and show log
    elements.testLog.textContent = `[${new Date().toLocaleTimeString()}] Starting test: ${testId}...\n`;
    elements.testLogContainer.classList.remove('hidden');

    try {
        const res = await fetch(`/api/tests/run/${testId}`, { method: 'POST' });
        const data = await res.json();

        if (data.success) {
            badge.className = `test-badge ${data.status}`;
            badge.textContent = data.status;
            resultSummary.textContent = `${data.rows_extracted} rows | ${data.duration}s`;

            elements.testLog.textContent += `[${new Date().toLocaleTimeString()}] Result: ${data.status.toUpperCase()}\n`;
            elements.testLog.textContent += `[${new Date().toLocaleTimeString()}] Message: ${data.message}\n`;
            elements.testLog.textContent += `[${new Date().toLocaleTimeString()}] Confidence: ${(data.confidence * 100).toFixed(1)}%\n`;
            elements.testLog.textContent += `[${new Date().toLocaleTimeString()}] Tools: ${data.tools_used.join(', ')}\n`;
        } else {
            badge.className = 'test-badge error';
            badge.textContent = 'Error';
            resultSummary.textContent = 'Failed';
            elements.testLog.textContent += `[${new Date().toLocaleTimeString()}] ERROR: ${data.error || 'Unknown error'}\n`;
        }
    } catch (error) {
        console.error('Error running test:', error);
        badge.className = 'test-badge error';
        badge.textContent = 'Error';
        elements.testLog.textContent += `[${new Date().toLocaleTimeString()}] FATAL ERROR: ${error.message}\n`;
    } finally {
        btn.disabled = false;
        elements.testLog.scrollTop = elements.testLog.scrollHeight;
    }
}

async function runAllTests() {
    const btns = document.querySelectorAll('.run-single-test');
    elements.runAllTests.disabled = true;
    elements.runAllTests.textContent = 'Running All...';

    for (const btn of btns) {
        await runSingleTest(btn.dataset.id);
    }

    elements.runAllTests.disabled = false;
    elements.runAllTests.textContent = 'Run All Tests';
}
// ===== Mapping Module =====
let currentMapping = [];

function initMapping() {
    if (elements.saveMappingBtn) {
        elements.saveMappingBtn.addEventListener('click', saveMapping);
    }
}

async function proposeMapping(jobId) {
    if (!elements.mappingWorkspace) return;

    elements.mappingWorkspace.innerHTML = '<div class="loading">Analyzing file schema... 🤖</div>';

    try {
        const res = await fetch(`/api/mapping/propose/${jobId}`);
        const data = await res.json();

        if (data.mapping) {
            currentMapping = data.mapping;
            renderMappingWorkspace(data.mapping);
            updateMappingStats();
        } else {
            throw new Error(data.error || 'Failed to generate proposal');
        }
    } catch (e) {
        console.error('[SIA] Mapping error:', e);
        elements.mappingWorkspace.innerHTML = `<div class="error">Failed to load mapping: ${e.message}</div>`;
    }
}

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
        elements.mappingWorkspace.innerHTML = '<div class="empty-state">No columns found</div>';
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
                <td>${item.column_name}</td>
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
                    <div class="reason-tooltip">
                        <span class="ai-icon" title="${item.reasoning}">🤖</span>
                        <span>${item.reasoning.substring(0, 50)}${item.reasoning.length > 50 ? '...' : ''}</span>
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
    if (!state.currentJobId) return;

    const btn = elements.saveMappingBtn;
    btn.disabled = true;
    btn.textContent = 'Saving...';

    try {
        const res = await fetch(`/api/mapping/submit/${state.currentJobId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mapping: currentMapping })
        });
        const data = await res.json();

        if (data.success) {
            showToast('Schema Mapping finalized! Proceeding to transformation...', 'success');
            // Move to processing tab to start Step-1
            switchTab('processing');
            // Potentially trigger processing if not started
            processFile(state.currentJobId);
        } else {
            throw new Error(data.error || 'Failed to save mapping');
        }
    } catch (e) {
        console.error('[SIA] Save mapping error:', e);
        showToast(`Error: ${e.message}`, 'error');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Finalize Mapping';
    }
}
