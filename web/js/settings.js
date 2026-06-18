/**
 * Settings Page Logic
 */

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
        if (config.api_key_set) {
            statusEl.innerHTML = `<span class="status-ok">✓ API Key configured</span>`;
        } else {
            statusEl.innerHTML = `<span class="status-warn">⚠ No API key set</span>`;
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
