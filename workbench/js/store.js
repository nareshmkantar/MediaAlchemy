/**
 * Workbench state. Dictionaries and decisions persist in localStorage.
 * Repeatable work is applied from config; only exceptions need a person.
 */
(function (global) {
    const STORAGE_KEY = "hw-workbench-v1";
    const SEED = global.HW_SEED;

    function clone(value) {
        return JSON.parse(JSON.stringify(value));
    }

    function detectPublisherFromName(fileName) {
        const n = String(fileName || "").toLowerCase();
        const pubs = SEED.publishers;
        const order = ["amazon_dsp", "retail_media", "google_ads", "dv360", "cm360", "facebook", "tiktok", "linkedin"];
        for (const id of order) {
            const pub = pubs[id];
            if (pub.aliases.some((a) => n.includes(a.toLowerCase())) || n.includes(id.replace("_", ""))) {
                return id;
            }
        }
        if (n.includes("amazon")) return "amazon_dsp";
        if (n.includes("google")) return "google_ads";
        return null;
    }

    function fieldById(id) {
        return SEED.standardFields.find((f) => f.id === id) || { id, label: id };
    }

    function normalizeKey(value) {
        return String(value || "")
            .toLowerCase()
            .replace(/[_-]+/g, " ")
            .replace(/\s+/g, " ")
            .trim();
    }

    function suggestMapping(sourceCol, dictionary) {
        const key = normalizeKey(sourceCol);
        const exact = dictionary.find((d) => normalizeKey(d.source) === key);
        if (exact) return { target: exact.target, confidence: 98, reason: "Dictionary exact match" };
        const partial = dictionary.find((d) => key.includes(normalizeKey(d.source)) || normalizeKey(d.source).includes(key));
        if (partial) return { target: partial.target, confidence: 86, reason: "Dictionary partial match" };
        const fields = SEED.standardFields;
        const byLabel = fields.find((f) => normalizeKey(f.label) === key || f.id === key);
        if (byLabel) return { target: byLabel.id, confidence: 94, reason: "Standard field name" };
        return { target: null, confidence: 42, reason: "No dictionary match" };
    }

    function buildMappings(files, dictionary) {
        const byFile = {};
        files.forEach((file) => {
            byFile[file.id] = (file.columns || []).map((col) => {
                const suggestion = suggestMapping(col, dictionary);
                return {
                    source: col,
                    target: suggestion.target,
                    confidence: suggestion.confidence,
                    reason: suggestion.reason,
                    accepted: suggestion.confidence >= 70,
                    excluded: false,
                };
            });
        });
        return byFile;
    }

    function defaultState() {
        const files = clone(SEED.demoFiles);
        const dictionary = clone(SEED.mappingDictionary);
        return {
            files,
            mappings: buildMappings(files, dictionary),
            dictionary,
            synonyms: clone(SEED.synonymLibraries),
            publishers: clone(SEED.publishers),
            rules: clone(SEED.validationRules),
            profiles: clone(SEED.profiles),
            activeProfileId: null,
            decisions: {},
            resolved: {},
            adminSection: "publishers",
            selectedPublisherId: "dv360",
            selectedSynonymDim: "Country",
            selectedMapFileId: files[0] ? files[0].id : null,
            dictionaryOpen: false,
        };
    }

    function load() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) return defaultState();
            const parsed = JSON.parse(raw);
            return { ...defaultState(), ...parsed };
        } catch {
            return defaultState();
        }
    }

    let state = load();

    function persist() {
        const copy = { ...state };
        localStorage.setItem(STORAGE_KEY, JSON.stringify(copy));
    }

    function get() {
        return state;
    }

    function set(patch) {
        state = { ...state, ...patch };
        persist();
        return state;
    }

    function reset() {
        state = defaultState();
        persist();
        return state;
    }

    function filesSummary() {
        const files = state.files || [];
        const rows = files.reduce((sum, f) => sum + (Number(f.rows) || 0), 0);
        const pubs = [...new Set(files.map((f) => f.publisherId).filter(Boolean))];
        return { files, rows, publishers: pubs };
    }

    function mappingStats() {
        const rows = Object.values(state.mappings || {}).flat();
        const mapped = rows.filter((r) => r.target && !r.excluded && r.accepted);
        const pending = rows.filter((r) => !r.excluded && (!r.target || !r.accepted || r.confidence < 70));
        return {
            total: rows.length,
            mapped: mapped.length,
            pending: pending.length,
            pct: rows.length ? Math.round((mapped.length / rows.length) * 100) : 0,
        };
    }

    function harmonizationStats() {
        const libs = state.synonyms || {};
        const demo = SEED.demoHarmonization;
        let auto = 0;
        let review = 0;
        Object.keys(demo).forEach((dim) => {
            (demo[dim] || []).forEach((row) => {
                if (state.resolved[`${dim}:${row.raw}`]) return;
                if (row.auto && row.suggested) auto += 1;
                else review += 1;
            });
        });
        const total = auto + review;
        return { auto, review, total, pct: total ? Math.round((auto / total) * 100) : 100, libraries: Object.keys(libs) };
    }

    function openExceptions() {
        return (SEED.demoIssues || []).filter((e) => !state.resolved[e.id]);
    }

    global.HWStore = {
        SEED,
        get,
        set,
        reset,
        persist,
        clone,
        detectPublisherFromName,
        fieldById,
        normalizeKey,
        suggestMapping,
        buildMappings,
        filesSummary,
        mappingStats,
        harmonizationStats,
        openExceptions,
    };
})(window);
