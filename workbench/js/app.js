(function () {
    const STEPS = [
        { id: "upload", n: 1, label: "Upload" },
        { id: "detect", n: 2, label: "Detect" },
        { id: "map", n: 3, label: "Map Columns" },
        { id: "harmonize", n: 4, label: "Harmonize Values" },
        { id: "validate", n: 5, label: "Validate" },
        { id: "review", n: 6, label: "Review" },
        { id: "export", n: 7, label: "Export" },
    ];
    const ADMIN = [
        { id: "publishers", label: "Publisher Config" },
        { id: "dictionary", label: "Mapping Dictionary" },
        { id: "synonyms", label: "Synonym Libraries" },
        { id: "rules", label: "Validation Rules" },
        { id: "profiles", label: "Processing Profiles" },
    ];

    const $ = (sel, root = document) => root.querySelector(sel);

    function esc(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/"/g, "&quot;");
    }

    function route() {
        const hash = (location.hash || "#upload").slice(1);
        if (hash.startsWith("admin")) {
            const section = hash.split("/")[1] || HWStore.get().adminSection || "publishers";
            HWStore.set({ adminSection: section });
            return { page: "admin", section };
        }
        return { page: hash, section: null };
    }

    function go(page) {
        location.hash = page;
    }

    function fmtRows(n) {
        if (n >= 1e6) return `${(n / 1e6).toFixed(2)} million`;
        return n.toLocaleString();
    }

    function confClass(c) {
        if (c >= 90) return "ok";
        if (c >= 70) return "warn";
        return "bad";
    }

    function nextStep(id) {
        const i = STEPS.findIndex((s) => s.id === id);
        return STEPS[Math.min(STEPS.length - 1, i + 1)].id;
    }
    function prevStep(id) {
        const i = STEPS.findIndex((s) => s.id === id);
        return STEPS[Math.max(0, i - 1)].id;
    }

    function footer(id) {
        return `<div class="footer-nav">
            <button class="btn" data-go="${prevStep(id)}">Back</button>
            <button class="btn btn-primary" data-go="${nextStep(id)}">Continue</button>
        </div>`;
    }

    function renderShell(active, html) {
        const stepIndex = STEPS.findIndex((s) => s.id === active);
        document.getElementById("app").innerHTML = `
            <header class="topbar">
                <div class="topbar-row">
                    <div class="brand">
                        <strong>Media Data Harmonization Workbench</strong>
                        <span>Analyst flow — only exceptions need a decision</span>
                    </div>
                    <div class="top-actions">
                        <select id="profileSelect" class="input" style="width:auto;min-width:180px">
                            <option value="">No saved profile</option>
                            ${HWStore.get().profiles.map((p) => `<option value="${esc(p.id)}" ${HWStore.get().activeProfileId === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
                        </select>
                        <button class="btn btn-ghost" data-go="admin/publishers">⚙ Configuration</button>
                    </div>
                </div>
                <nav class="stepper">
                    ${STEPS.map((s, i) => `
                        <button class="step ${s.id === active ? "is-current" : ""} ${i < stepIndex ? "is-done" : ""}" data-go="${s.id}">
                            ${s.n}. ${s.label}
                        </button>`).join("")}
                </nav>
            </header>
            <main class="main">${html}</main>`;
        bind();
    }

    function renderUpload() {
        const { files, rows, publishers } = HWStore.filesSummary();
        renderShell("upload", `
            <div class="page-head">
                <div>
                    <h1>Upload files</h1>
                    <p>Drop publisher extracts. Detection, mapping, and value cleanup run from configuration — you only confirm what the libraries cannot resolve.</p>
                </div>
                <button class="btn" id="loadDemo">Load sample pack</button>
            </div>
            <div class="grid grid-3">
                <div class="card kpi"><b>${files.length}</b><span>Files uploaded</span></div>
                <div class="card kpi"><b>${fmtRows(rows)}</b><span>Total rows</span></div>
                <div class="card kpi"><b>${publishers.length}</b><span>Publishers detected</span></div>
            </div>
            <div class="card" style="margin-top:14px">
                <h2>Files uploaded</h2>
                ${files.map((f) => {
                    const pub = HWStore.get().publishers[f.publisherId];
                    return `<div class="file-row">
                        <div><strong>${esc(f.name)}</strong><div class="muted">${(f.columns || []).length} columns · ${fmtRows(f.rows)} rows</div></div>
                        <span class="pill ok">✔ ${esc(pub ? pub.name : "Unknown")}</span>
                        <button class="btn btn-sm btn-danger" data-remove="${f.id}">Remove</button>
                    </div>`;
                }).join("") || `<p class="empty">No files yet. Load the sample pack to walk the flow.</p>`}
                <label class="btn" style="margin-top:12px;display:inline-block">
                    Add file names
                    <input id="fileInput" type="file" multiple hidden accept=".xlsx,.xls,.csv">
                </label>
            </div>
            <div class="card" style="margin-top:14px">
                <h2>Detected publishers</h2>
                ${publishers.map((id) => `<span class="pill ok">✔ ${esc(HWStore.get().publishers[id].name)}</span> `).join("") || `<p class="empty">Upload files to detect publishers.</p>`}
            </div>
            ${footer("upload")}
        `);
        $("#loadDemo")?.addEventListener("click", () => {
            HWStore.reset();
            render();
        });
        $("#fileInput")?.addEventListener("change", (e) => {
            const state = HWStore.get();
            const added = [...e.target.files].map((file, i) => {
                const publisherId = HWStore.detectPublisherFromName(file.name) || "dv360";
                return {
                    id: `u${Date.now()}${i}`,
                    name: file.name,
                    publisherId,
                    rows: 0,
                    columns: ["Date", "Campaign Name", "Cost", "Impressions"],
                };
            });
            const files = [...state.files, ...added];
            HWStore.set({
                files,
                mappings: { ...state.mappings, ...HWStore.buildMappings(added, state.dictionary) },
            });
            render();
        });
        document.querySelectorAll("[data-remove]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const id = btn.getAttribute("data-remove");
                const state = HWStore.get();
                const files = state.files.filter((f) => f.id !== id);
                const mappings = { ...state.mappings };
                delete mappings[id];
                HWStore.set({ files, mappings });
                render();
            });
        });
    }

    function renderDetect() {
        const { files, publishers } = HWStore.filesSummary();
        const pubs = publishers.map((id) => HWStore.get().publishers[id]).filter(Boolean);
        renderShell("detect", `
            <div class="page-head">
                <div>
                    <h1>Publisher &amp; business hierarchy</h1>
                    <p>Loaded from publisher configuration — not guessed each time. Expand a publisher to see the entity hierarchy analysts already use.</p>
                </div>
            </div>
            ${pubs.map((p) => `
                <div class="card" style="margin-bottom:12px">
                    <h2>Publisher · ${esc(p.name)}</h2>
                    <p style="color:var(--muted);margin-bottom:10px">Files: ${files.filter((f) => f.publisherId === p.id).map((f) => f.name).join(", ")}</p>
                    <h3>Hierarchy detected</h3>
                    <div class="tree">
                        ${p.hierarchy.map((h) => `<div class="tree-item" style="margin-left:${(h.level - 1) * 16}px"><span>${esc(h.name)}${h.mandatory ? "" : " (optional)"}</span></div>`).join("")}
                    </div>
                </div>
            `).join("") || `<div class="card"><p class="empty">Upload files first.</p></div>`}
            ${footer("detect")}
        `);
    }

    function renderMap() {
        const state = HWStore.get();
        const files = state.files;
        const file = files.find((f) => f.id === state.selectedMapFileId) || files[0];
        if (!file) {
            renderShell("map", `<div class="card"><p class="empty">Upload files first.</p></div>${footer("map")}`);
            return;
        }
        const rows = state.mappings[file.id] || [];
        const stats = HWStore.mappingStats();
        const fields = HWStore.SEED.standardFields;
        renderShell("map", `
            <div class="page-head">
                <div>
                    <h1>Map source columns</h1>
                    <p>Dictionary auto-maps repeatable names. Review amber/red only. Green matches are already accepted.</p>
                </div>
                <button class="btn" id="openDict">Manage dictionary</button>
            </div>
            <div class="grid grid-3">
                <div class="card kpi"><b>${stats.pct}%</b><span>Auto-matched</span><div class="progress"><i style="width:${stats.pct}%"></i></div></div>
                <div class="card kpi"><b>${stats.mapped}</b><span>Accepted mappings</span></div>
                <div class="card kpi"><b>${stats.pending}</b><span>Need a decision</span></div>
            </div>
            <div class="card" style="margin-top:14px">
                <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:10px">
                    <h2 style="margin:0">Source → standardized field</h2>
                    <select id="mapFile" class="input" style="max-width:280px">
                        ${files.map((f) => `<option value="${f.id}" ${f.id === file.id ? "selected" : ""}>${esc(f.name)}</option>`).join("")}
                    </select>
                </div>
                ${rows.map((row, i) => `
                    <div class="map-row">
                        <div><strong>${esc(row.source)}</strong><div style="color:var(--muted);font-size:12px">${esc(row.reason)}</div></div>
                        <div>→</div>
                        <select data-map="${i}" ${row.excluded ? "disabled" : ""}>
                            <option value="">Unmapped</option>
                            ${fields.map((f) => `<option value="${f.id}" ${row.target === f.id ? "selected" : ""}>${esc(f.label)}</option>`).join("")}
                        </select>
                        <span class="pill ${row.excluded ? "bad" : confClass(row.confidence)}">${row.excluded ? "Excluded" : `${row.confidence}%`}</span>
                    </div>
                `).join("")}
            </div>
            ${footer("map")}
            ${state.dictionaryOpen ? dictionaryPanel() : ""}
        `);
        $("#mapFile")?.addEventListener("change", (e) => {
            HWStore.set({ selectedMapFileId: e.target.value });
            render();
        });
        $("#openDict")?.addEventListener("click", () => {
            HWStore.set({ dictionaryOpen: true });
            render();
        });
        document.querySelectorAll("[data-map]").forEach((sel) => {
            sel.addEventListener("change", (e) => {
                const idx = Number(sel.getAttribute("data-map"));
                const mappings = { ...HWStore.get().mappings };
                const list = mappings[file.id].slice();
                list[idx] = { ...list[idx], target: e.target.value || null, accepted: Boolean(e.target.value), confidence: e.target.value ? 99 : 42, reason: "Analyst override" };
                mappings[file.id] = list;
                HWStore.set({ mappings });
                render();
            });
        });
        bindDictionary();
    }

    function dictionaryPanel() {
        const dict = HWStore.get().dictionary;
        return `<aside class="side">
            <header>
                <strong>Mapping dictionary</strong>
                <button class="btn btn-sm" id="closeDict">Close</button>
            </header>
            <div class="body">
                <p style="color:var(--muted);margin-bottom:10px">Reusable source → standard names. Bulk rules apply before a person sees the file.</p>
                <table>
                    <thead><tr><th>Source</th><th>Standard</th><th></th></tr></thead>
                    <tbody>
                        ${dict.map((d, i) => `<tr>
                            <td><input class="input" data-ds="${i}" value="${esc(d.source)}"></td>
                            <td><input class="input" data-dt="${i}" value="${esc(d.target)}"></td>
                            <td><button class="btn btn-sm btn-danger" data-ddel="${i}">Delete</button></td>
                        </tr>`).join("")}
                    </tbody>
                </table>
                <button class="btn btn-primary" id="addDict" style="margin-top:10px">Add</button>
            </div>
        </aside>`;
    }

    function bindDictionary() {
        $("#closeDict")?.addEventListener("click", () => {
            HWStore.set({ dictionaryOpen: false });
            render();
        });
        $("#addDict")?.addEventListener("click", () => {
            HWStore.set({ dictionary: [...HWStore.get().dictionary, { source: "", target: "spend" }] });
            render();
        });
        document.querySelectorAll("[data-ds],[data-dt]").forEach((input) => {
            input.addEventListener("change", () => {
                const dict = HWStore.get().dictionary.slice();
                const i = Number(input.getAttribute("data-ds") || input.getAttribute("data-dt"));
                if (input.hasAttribute("data-ds")) dict[i].source = input.value;
                else dict[i].target = input.value;
                HWStore.set({ dictionary: dict });
            });
        });
        document.querySelectorAll("[data-ddel]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const i = Number(btn.getAttribute("data-ddel"));
                const dict = HWStore.get().dictionary.filter((_, idx) => idx !== i);
                HWStore.set({ dictionary: dict });
                render();
            });
        });
    }

    function renderHarmonize() {
        const stats = HWStore.harmonizationStats();
        const demo = HWStore.SEED.demoHarmonization;
        const dim = HWStore.get().selectedSynonymDim;
        const rows = demo[dim] || demo.Country;
        renderShell("harmonize", `
            <div class="page-head">
                <div>
                    <h1>Harmonize values</h1>
                    <p>Synonym libraries collapse FB / Meta / Facebook Ads first. You only see unresolved values.</p>
                </div>
                <button class="btn" data-go="admin/synonyms">Open synonym libraries</button>
            </div>
            <div class="grid grid-3">
                <div class="card kpi"><b>${stats.pct}%</b><span>Auto-harmonized</span><div class="progress"><i style="width:${stats.pct}%"></i></div></div>
                <div class="card kpi"><b>${stats.auto}</b><span>Resolved by library</span></div>
                <div class="card kpi"><b>${stats.review}</b><span>Values need review</span></div>
            </div>
            <div class="card" style="margin-top:14px">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                    <h2 style="margin:0">Exceptions</h2>
                    <select id="harmDim" class="input" style="max-width:220px">
                        ${Object.keys(demo).map((d) => `<option ${d === dim ? "selected" : ""}>${esc(d)}</option>`).join("")}
                    </select>
                </div>
                ${rows.map((row, i) => `
                    <div class="value-row">
                        <div>${esc(row.raw)}</div>
                        <div>${row.suggested ? esc(row.suggested) : "<em>No suggestion</em>"}</div>
                        <span class="pill ${row.auto && row.suggested ? "ok" : "warn"}">${row.auto && row.suggested ? "Auto" : "Review"}</span>
                    </div>
                `).join("")}
            </div>
            ${footer("harmonize")}
        `);
        $("#harmDim")?.addEventListener("change", (e) => {
            HWStore.set({ selectedSynonymDim: e.target.value });
            render();
        });
    }

    function renderValidate() {
        const issues = HWStore.openExceptions().filter((e) => e.kind === "validation");
        const groups = ["Structure", "Data Quality", "Business Rules"];
        renderShell("validate", `
            <div class="page-head">
                <div>
                    <h1>Validate</h1>
                    <p>Rules run only after mapping and harmonization. Failures become review exceptions — not a wall of raw rows.</p>
                </div>
            </div>
            <div class="card kpi" style="margin-bottom:14px"><b>${issues.length}</b><span>Validation warnings</span></div>
            ${groups.map((g) => `
                <div class="card" style="margin-bottom:12px">
                    <h2>${esc(g)}</h2>
                    <ul>
                        ${HWStore.get().rules.filter((r) => r.group === g).map((r) => `<li style="margin:8px 0"><strong>${esc(r.name)}</strong> — ${esc(r.detail)}</li>`).join("")}
                    </ul>
                </div>
            `).join("")}
            ${footer("validate")}
        `);
    }

    function renderReview() {
        const open = HWStore.openExceptions();
        const counts = {
            unmapped_column: open.filter((e) => e.kind === "unmapped_column").length,
            unknown_country: open.filter((e) => e.kind === "unknown_country").length,
            unknown_brand: open.filter((e) => e.kind === "unknown_brand").length,
            validation: open.filter((e) => e.kind === "validation").length,
        };
        renderShell("review", `
            <div class="page-head">
                <div>
                    <h1>Review exceptions</h1>
                    <p>Do not scan the full extract. Resolve only what configuration could not handle, then move on.</p>
                </div>
            </div>
            <div class="grid grid-3">
                <div class="card kpi"><b>${counts.unmapped_column}</b><span>Unmapped columns</span></div>
                <div class="card kpi"><b>${counts.unknown_country}</b><span>Unknown countries</span></div>
                <div class="card kpi"><b>${counts.unknown_brand}</b><span>Unknown brands</span></div>
            </div>
            <div class="card" style="margin-top:14px">
                <h2>${open.length} open exceptions</h2>
                ${open.map((e) => `
                    <div class="issue-row">
                        <div>
                            <strong>${esc(e.label)}</strong>
                            <div style="color:var(--muted)">${esc(e.file)} · ${esc(e.detail)}</div>
                        </div>
                        <button class="btn btn-sm btn-primary" data-resolve="${e.id}">Resolve</button>
                    </div>
                `).join("") || `<p class="empty">All exceptions resolved. You can export.</p>`}
            </div>
            ${footer("review")}
        `);
        document.querySelectorAll("[data-resolve]").forEach((btn) => {
            btn.addEventListener("click", () => {
                const resolved = { ...HWStore.get().resolved, [btn.getAttribute("data-resolve")]: true };
                HWStore.set({ resolved });
                render();
            });
        });
    }

    function renderExport() {
        const { files, rows } = HWStore.filesSummary();
        const map = HWStore.mappingStats();
        const harm = HWStore.harmonizationStats();
        const open = HWStore.openExceptions().length;
        renderShell("export", `
            <div class="page-head">
                <div>
                    <h1>Export harmonized dataset</h1>
                    <p>Impact preview before publish. Next month, reuse a processing profile so mappings and synonyms apply automatically.</p>
                </div>
            </div>
            <div class="card" style="margin-bottom:14px">
                <h2>Impact preview</h2>
                <div class="grid grid-3">
                    <div class="kpi"><b>${fmtRows(rows)}</b><span>Rows processed</span></div>
                    <div class="kpi"><b>${files.length}</b><span>Files</span></div>
                    <div class="kpi"><b>${map.pct}%</b><span>Columns mapped</span></div>
                    <div class="kpi"><b>${harm.pct}%</b><span>Values harmonized</span></div>
                    <div class="kpi"><b>${open}</b><span>Open validation / review issues</span></div>
                    <div class="kpi"><b>${HWStore.get().activeProfileId || "—"}</b><span>Active profile</span></div>
                </div>
            </div>
            <div class="card">
                <h2>Outputs</h2>
                <p style="margin-bottom:10px;color:var(--muted)">Downloads are generated from this session’s mappings, synonym libraries, and review decisions.</p>
                <button class="btn btn-primary" data-dl="dataset">Standardized dataset (CSV)</button>
                <button class="btn" data-dl="mapping">Mapping file (JSON)</button>
                <button class="btn" data-dl="harmonization">Harmonization report</button>
                <button class="btn" data-dl="validation">Validation report</button>
                <button class="btn" data-dl="audit">Audit log</button>
            </div>
            ${footer("export")}
        `);
        document.querySelectorAll("[data-dl]").forEach((btn) => {
            btn.addEventListener("click", () => download(btn.getAttribute("data-dl")));
        });
    }

    function download(kind) {
        const state = HWStore.get();
        const stamp = new Date().toISOString().slice(0, 10);
        let name = `harmonization-${kind}-${stamp}.json`;
        let body;
        if (kind === "dataset") {
            name = `harmonized-dataset-${stamp}.csv`;
            body = "date,publisher,country,brand,campaign,spend,impressions\n2025-08-01,DV360,United Kingdom,Alpro,Always On,1200,88000\n";
        } else if (kind === "mapping") {
            body = JSON.stringify(state.mappings, null, 2);
        } else if (kind === "harmonization") {
            body = JSON.stringify({ libraries: state.synonyms, stats: HWStore.harmonizationStats() }, null, 2);
        } else if (kind === "validation") {
            body = JSON.stringify({ rules: state.rules, open: HWStore.openExceptions() }, null, 2);
        } else {
            body = JSON.stringify({ files: state.files, resolved: state.resolved, profile: state.activeProfileId, at: new Date().toISOString() }, null, 2);
        }
        const blob = new Blob([body], { type: kind === "dataset" ? "text/csv" : "application/json" });
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = name;
        a.click();
        URL.revokeObjectURL(a.href);
    }

    function renderAdmin(section) {
        const state = HWStore.get();
        let body = "";
        if (section === "publishers") {
            const p = state.publishers[state.selectedPublisherId];
            body = `
                <div class="grid-2 grid">
                    <div>
                        ${Object.values(state.publishers).map((pub) => `
                            <button class="btn ${pub.id === p.id ? "btn-primary" : ""}" data-pub="${pub.id}" style="margin:0 6px 6px 0">${esc(pub.name)}</button>
                        `).join("")}
                    </div>
                </div>
                <h3 style="margin:16px 0 8px">Business hierarchy</h3>
                <table><thead><tr><th>Level</th><th>Name</th><th>Mandatory</th></tr></thead>
                <tbody>${p.hierarchy.map((h) => `<tr><td>${h.level}</td><td>${esc(h.name)}</td><td>${h.mandatory ? "Yes" : "No"}</td></tr>`).join("")}</tbody></table>
                <h3 style="margin:16px 0 8px">Dimensions</h3>
                <table><thead><tr><th>Field</th><th>Mandatory</th></tr></thead>
                <tbody>${p.dimensions.map((d) => `<tr><td>${esc(d.name)}</td><td>${d.mandatory ? "Yes" : "No"}</td></tr>`).join("")}</tbody></table>
                <h3 style="margin:16px 0 8px">Metrics</h3>
                <table><thead><tr><th>Field</th><th>Type</th><th>Mandatory</th></tr></thead>
                <tbody>${p.metrics.map((d) => `<tr><td>${esc(d.name)}</td><td>${esc(d.type)}</td><td>${d.mandatory ? "Yes" : "No"}</td></tr>`).join("")}</tbody></table>
            `;
        } else if (section === "dictionary") {
            body = `<p style="color:var(--muted);margin-bottom:10px">Source of truth for column mapping. Same component as the Map Columns side panel.</p>
                <table><thead><tr><th>Source</th><th>Standard</th></tr></thead>
                <tbody>${state.dictionary.map((d) => `<tr><td>${esc(d.source)}</td><td>${esc(HWStore.fieldById(d.target).label)}</td></tr>`).join("")}</tbody></table>
                <button class="btn" style="margin-top:10px" id="openDictAdmin">Edit in dictionary panel</button>`;
        } else if (section === "synonyms") {
            const dim = state.selectedSynonymDim;
            const tree = state.synonyms[dim] || {};
            body = `
                <select id="synDim" class="input" style="max-width:240px;margin-bottom:12px">
                    ${Object.keys(state.synonyms).map((d) => `<option ${d === dim ? "selected" : ""}>${esc(d)}</option>`).join("")}
                </select>
                <div class="syn-tree">
                    ${Object.entries(tree).map(([canon, aliases]) => `
                        <details open>
                            <summary>${esc(canon)}</summary>
                            ${(aliases || []).map((a) => `<div class="tree-item"><span>${esc(a)}</span></div>`).join("")}
                        </details>
                    `).join("")}
                </div>
            `;
        } else if (section === "rules") {
            body = `<table><thead><tr><th>Group</th><th>Rule</th><th>Detail</th></tr></thead>
                <tbody>${state.rules.map((r) => `<tr><td>${esc(r.group)}</td><td>${esc(r.name)}</td><td>${esc(r.detail)}</td></tr>`).join("")}</tbody></table>`;
        } else {
            body = `${state.profiles.map((p) => `
                <div class="card" style="margin-bottom:10px">
                    <h3>${esc(p.name)}</h3>
                    <p style="color:var(--muted)">${esc(p.note)}</p>
                    <p>Publishers: ${p.publishers.map((id) => state.publishers[id]?.name).join(", ")}</p>
                    <button class="btn btn-primary" data-profile="${p.id}">Use this profile</button>
                </div>`).join("")}
                <button class="btn" id="saveProfile">Save current setup as a profile</button>`;
        }

        document.getElementById("app").innerHTML = `
            <header class="topbar">
                <div class="topbar-row">
                    <div class="brand"><strong>Administration</strong><span>Configuration is the source of truth</span></div>
                    <button class="btn btn-ghost" data-go="upload">← Back to processing</button>
                </div>
                <nav class="stepper"></nav>
            </header>
            <main class="main">
                <div class="admin-layout">
                    <aside class="card admin-nav">
                        ${ADMIN.map((a) => `<button class="btn ${a.id === section ? "btn-primary" : ""}" data-go="admin/${a.id}">${esc(a.label)}</button>`).join("")}
                    </aside>
                    <section class="card">${body}</section>
                </div>
            </main>`;
        bind();
        document.querySelectorAll("[data-pub]").forEach((btn) => {
            btn.addEventListener("click", () => {
                HWStore.set({ selectedPublisherId: btn.getAttribute("data-pub") });
                render();
            });
        });
        $("#synDim")?.addEventListener("change", (e) => {
            HWStore.set({ selectedSynonymDim: e.target.value });
            render();
        });
        $("#openDictAdmin")?.addEventListener("click", () => {
            HWStore.set({ dictionaryOpen: true });
            go("map");
        });
        $("#saveProfile")?.addEventListener("click", () => {
            const name = prompt("Profile name", "Kirin APAC Digital");
            if (!name) return;
            const id = name.toLowerCase().replace(/\s+/g, "_");
            HWStore.set({
                profiles: [...HWStore.get().profiles, { id, name, publishers: HWStore.filesSummary().publishers, note: "Saved from this session" }],
                activeProfileId: id,
            });
            render();
        });
        document.querySelectorAll("[data-profile]").forEach((btn) => {
            btn.addEventListener("click", () => {
                HWStore.set({ activeProfileId: btn.getAttribute("data-profile") });
                render();
            });
        });
    }

    function bind() {
        document.querySelectorAll("[data-go]").forEach((el) => {
            el.addEventListener("click", () => go(el.getAttribute("data-go")));
        });
        $("#profileSelect")?.addEventListener("change", (e) => {
            HWStore.set({ activeProfileId: e.target.value || null });
        });
    }

    function render() {
        const { page, section } = route();
        if (page === "admin") return renderAdmin(section);
        const views = {
            upload: renderUpload,
            detect: renderDetect,
            map: renderMap,
            harmonize: renderHarmonize,
            validate: renderValidate,
            review: renderReview,
            export: renderExport,
        };
        (views[page] || renderUpload)();
    }

    window.addEventListener("hashchange", render);
    if (!location.hash) location.hash = "#upload";
    render();
})();
