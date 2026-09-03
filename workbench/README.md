# Media Data Harmonization & Standardization Workbench

Analyst-focused onboarding for heterogeneous publisher files (Facebook, Google Ads, DV360, CM360, Amazon DSP, TikTok, LinkedIn, Retail Media) into one harmonized dataset for MMM, attribution, and reporting.

This folder is a **separate product surface** from Schema Agent. It reuses mapping/synonym ideas from `config/synonyms.json` and `config/target_template.json` but does not follow the agent stepper (Upload → Schema Mapping → Console).

## Workflow

1. **Upload** — files, row counts, detected publishers  
2. **Detect** — publisher + **business / entity hierarchy** (not “data level”)  
3. **Map Columns** — dictionary auto-match with confidence (green / amber / red)  
4. **Harmonize Values** — synonym libraries first; only unresolved values go to people  
5. **Validate** — structure, data quality, business rules  
6. **Review** — exceptions only  
7. **Export** — dataset, mapping file, reports, audit log + impact preview  

**Configuration** (separate from the run): Publisher config, mapping dictionary, synonym libraries, validation rules, reusable processing profiles (`Kirin APAC Digital`, `BT UK MMM`).

## Open it

With the project server running:

[http://127.0.0.1:5000/workbench/](http://127.0.0.1:5000/workbench/)

Or open `workbench/index.html` directly in a browser (localStorage still works).

Click **Load sample pack** on Upload to walk Amazon DSP, Google Ads, DV360, and Facebook through the stages.

## What is automatic vs human

| Automatic (config / dictionary / synonyms) | Human judgment |
| --- | --- |
| Publisher detection from file name | Unmapped columns |
| Hierarchy from publisher JSON | Unknown countries / brands |
| Column suggestions from the dictionary | Validation failures (credits, bad dates, CTR) |
| Value collapse (FB → Facebook, US → USA) | Confirm or add a new synonym |

## Files

| Path | Role |
| --- | --- |
| `index.html` | Shell |
| `css/workbench.css` | Analyst UI (light, stage-based) |
| `js/data.js` | Publisher hierarchies, dictionary, synonym trees, demo extracts |
| `js/store.js` | Session + localStorage |
| `js/app.js` | Stages, Configuration, export downloads |
