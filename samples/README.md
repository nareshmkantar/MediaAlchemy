# Sample inputs

Place your test files here. Nothing in this folder is loaded automatically — use the **Upload** page in the web UI (or drag files from this folder).

## Folder layout

```
samples/
  excel/       ← data workbooks (.xlsx, .xls, .csv)
  templates/   ← target schema JSON (optional, for guided mapping)
  chaos/       ← optional stress-test workbooks (used by in-app test runner)
```

## Web UI

1. Start the server: `python web_server.py`
2. Open **Upload**
3. **Excel zone** — pick a file from `samples/excel/`
4. **JSON zone** (optional) — pick a target template from `samples/templates/`

Supported data formats: `.xlsx`, `.xls`, `.csv`  
Template format: `.json` (target schema with `x_scope`, `properties`, etc.)

## Optional filenames (for automated tests)

If you want `pytest tests/test_golden_files.py` to find inputs, put workbooks in **`samples/`** (root) with these names:

| File | Scenario |
|------|----------|
| `scenario_1_ideal.xlsx` | Simple flat table |
| `scenario_3_pivot.xlsx` | Pivot / crosstab |
| `scenario_4_merged.xlsx` | Merged cells |
| `scenario_5_multi.xlsx` | Multiple blocks on one sheet |
| `scenario_6_messy.xlsx` | Messy headers + subtotals |

For the in-app **Tests** tab / `TestRunner`, use **`samples/chaos/`**:

| File |
|------|
| `merged_cells_test.xlsx` |
| `pivot_test.xlsx` |
| `multi_block_test.xlsx` |
| `real_world_2023.xlsx` |

## Templates

Copy or adapt from `config/target_template.json` for the shape of a valid target JSON.  
Do not commit real client data unless your repo policy allows it.
