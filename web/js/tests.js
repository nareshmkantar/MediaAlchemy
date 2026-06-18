/**
 * Tests Page - Schema Agent
 */

document.addEventListener('DOMContentLoaded', () => {
    initTests();
    fetchTests();
});

function initTests() {
    const runAllBtn = document.getElementById('runAllTests');
    if (runAllBtn) {
        runAllBtn.addEventListener('click', runAllTests);
    }
    const closeLogBtn = document.getElementById('closeTestLog');
    if (closeLogBtn) {
        closeLogBtn.addEventListener('click', () => {
            document.getElementById('testLogContainer').classList.add('hidden');
        });
    }
}

async function fetchTests() {
    try {
        const data = await fetchJson('/api/tests/list');
        if (data.success) {
            renderTestCards(data.tests);
        }
    } catch (error) {
        console.error('Error fetching tests:', error);
        showToast('Failed to load tests', 'error');
    }
}

function renderTestCards(tests) {
    const grid = document.getElementById('testGrid');
    if (!grid) return;

    grid.innerHTML = tests.map(test => `
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
    const logContainer = document.getElementById('testLogContainer');
    const log = document.getElementById('testLog');

    badge.className = 'test-badge running';
    badge.textContent = 'Running';
    btn.disabled = true;

    // Clear and show log
    log.textContent = `[${new Date().toLocaleTimeString()}] Starting test: ${testId}...\n`;
    logContainer.classList.remove('hidden');

    try {
        const data = await fetchJson(`/api/tests/run/${testId}`, { method: 'POST' });

        if (data.success) {
            badge.className = `test-badge ${data.status}`;
            badge.textContent = data.status;
            resultSummary.textContent = `${data.rows_extracted || 0} rows | ${data.duration || 0}s`;

            log.textContent += `[${new Date().toLocaleTimeString()}] Result: ${data.status.toUpperCase()}\n`;
            log.textContent += `[${new Date().toLocaleTimeString()}] Message: ${data.message}\n`;
            log.textContent += `[${new Date().toLocaleTimeString()}] Confidence: ${(data.confidence * 100).toFixed(1)}%\n`;
            log.textContent += `[${new Date().toLocaleTimeString()}] Tools: ${(data.tools_used || []).join(', ')}\n`;
        } else {
            badge.className = 'test-badge error';
            badge.textContent = 'Error';
            resultSummary.textContent = 'Failed';
            log.textContent += `[${new Date().toLocaleTimeString()}] ERROR: ${data.error || 'Unknown error'}\n`;
        }
    } catch (error) {
        console.error('Error running test:', error);
        badge.className = 'test-badge error';
        badge.textContent = 'Error';
        log.textContent += `[${new Date().toLocaleTimeString()}] FATAL ERROR: ${error.message}\n`;
    } finally {
        btn.disabled = false;
        log.scrollTop = log.scrollHeight;
    }
}

async function runAllTests() {
    const btns = document.querySelectorAll('.run-single-test');
    const runAllBtn = document.getElementById('runAllTests');
    runAllBtn.disabled = true;
    runAllBtn.textContent = 'Running All...';

    for (const btn of btns) {
        await runSingleTest(btn.dataset.id);
    }

    runAllBtn.disabled = false;
    runAllBtn.textContent = 'Run All Tests';
}
