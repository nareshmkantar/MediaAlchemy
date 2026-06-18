# SchemaAgent (SIA)

**Structure Inference Agent** - An LLM-powered tool for automatically extracting clean, flat tables from messy spreadsheets.

---

## Overview

SchemaAgent analyzes complex Excel files with merged cells, hierarchical headers, summary rows, and irregular layouts. It uses Large Language Models (LLMs) to understand the structure and intelligently extract normalized, analysis-ready data.

### Key Features

- **🔍 Automatic Structure Detection** - Identifies data blocks, headers, and table boundaries
- **🧹 Smart Data Cleaning** - Removes summary rows, fills merged cells, and normalizes dates
- **🤖 LLM-Powered Analysis** - Uses Gemini/Groq models for intelligent decision-making
- **🔄 Iterative Verification** - Self-corrects using a verification loop
- **👁️ Human-in-the-Loop** - Flags sensitive operations for review
- **🌐 Web Interface** - Easy-to-use UI for file upload and results inspection

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Web Interface                             │
│                    (Upload → Process → Review)                   │
└─────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│                     StructureInferenceAgent                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   Loader    │→ │  Analyzer   │→ │    PlanGenerator        │  │
│  │ (Excel/CSV) │  │ (LLM-based) │  │ (Creates Extraction Plan)│  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
│                                              │                   │
│                                              ▼                   │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    Tool Executor                             ││
│  │  • extract_data_block    • filter_summary_rows               ││
│  │  • fill_merged_cells     • densify_dataframe                 ││
│  │  • normalize_dates       • unpivot_columns                   ││
│  └─────────────────────────────────────────────────────────────┘│
│                                              │                   │
│                                              ▼                   │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                   OutputVerifier (LLM)                       ││
│  │         Checks if output is flat → Suggests fixes            ││
│  │              (Max 3 iteration loop)                          ││
│  └─────────────────────────────────────────────────────────────┘│
│                                              │                   │
│                                              ▼                   │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                      Schema + Data                           ││
│  │            (InferredSchema, DataFrame, Trace)                ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

---

## Installation

### Prerequisites

- Python 3.10+
- pip

### Setup

```bash
# Clone the repository
cd SchemaAgent

# Install dependencies
pip install -r requirements.txt

# Set up API Key (choose one)
# Option 1: Gemini (recommended)
# Get key from: https://aistudio.google.com/app/apikey

# Option 2: Groq (if Gemini blocked)
# Get key from: https://console.groq.com
```

---

## Usage

### Web Interface (Recommended)

```bash
# Start the server
python web_server.py

# Open browser to: http://localhost:5000
```

**Steps:**
1. Go to **Settings** → Enter API Key → Select Model → Save
2. Go to **Upload** → Drop your Excel file
3. Go to **Processing** → Click "Process File"
4. Review results in **Debug** tab

### Command Line

```bash
python main.py path/to/your/file.xlsx
```

---

## Configuration

Configuration is stored in `config/user_config.json`:

```json
{
  "api_key": "YOUR_API_KEY",
  "llm_model": "gemini-2.5-flash",
  "output_format": "csv",
  "debug_enabled": true,
  "confidence_thresholds": {
    "auto_approve": 0.9,
    "flag_for_review": 0.6
  }
}
```

### Supported Models

| Provider | Model | Quota (Free Tier) | Notes |
|----------|-------|-------------------|-------|
| Google | `gemini-2.5-flash` | ~500/day | **Recommended** |
| Google | `gemini-2.0-flash-exp` | ~1500/day | Experimental |
| Google | `gemini-3-flash-preview` | 20/day | Very low limit |
| Groq | `groq-llama3-70b-8192` | High | Requires network access to api.groq.com |

---

## Project Structure

```
SchemaAgent/
├── web_server.py          # Flask web server (main entry point)
├── main.py                # CLI entry point
├── requirements.txt       # Python dependencies
│
├── config/
│   ├── user_config.json   # User settings (API keys, model)
│   ├── target_template.json
│   └── synonyms.json
│
├── runtime/
│   ├── uploads/
│   ├── outputs/
│   ├── snapshots/
│   └── logs/
│
├── scripts/
│   └── create_sample_excel.py
│
├── sia/                   # Core library
│   ├── agent/
│   │   ├── orchestrator.py    # Main pipeline coordinator
│   │   └── planner.py         # LLM components (Analyzer, PlanGenerator, Verifier)
│   ├── loaders/
│   │   └── excel_loader.py    # Excel/CSV file parsing
│   ├── tools/
│   │   └── transformation_tools.py  # Data transformation functions
│   ├── models/
│   │   ├── schema.py          # InferredSchema dataclass
│   │   ├── confidence.py      # ProcessingTrace for observability
│   │   └── cell.py            # Cell and DataBlock models
│   └── debug/
│       └── llm_observer.py    # LLM call tracing
│
├── prompts/               # Externalized LLM prompts
│   ├── structure_analyzer.md
│   ├── plan_generator.md
│   └── output_verifier.md
│
├── web/                   # Frontend assets
│   ├── index.html
│   ├── styles.css
│   └── app.js
│
├── config/
│   └── semantic_config.yaml   # Domain knowledge configuration
│
└── docs/                  # Documentation (this folder)
    └── README.md
```

---

## Key Concepts

### 1. Structure Analysis
The `StructureAnalyzer` LLM examines the spreadsheet grid and identifies:
- Table boundaries (start_row, end_row, start_col, end_col)
- Header rows
- Merged cell regions
- Summary/total rows to skip

### 2. Extraction Plan
The `PlanGenerator` creates a sequence of tool calls to transform raw data:
```json
{
  "tool_calls": [
    {"tool": "extract_data_block", "params": {"start_row": 3, "end_row": 50}},
    {"tool": "fill_merged_cells", "params": {"columns": ["Brand"]}},
    {"tool": "filter_summary_rows", "params": {"keywords": ["Total", "Sum"]}}
  ]
}
```

### 3. Transformation Tools
Available tools for data cleaning:
- `extract_data_block` - Slice a region from the grid
- `fill_merged_cells` - Forward-fill empty cells in dimension columns
- `filter_summary_rows` - Remove aggregate rows
- `filter_header_rows` - Remove repeated header artifacts
- `densify_dataframe` - Fill sparse dimension columns
- `normalize_dates` - Standardize date formats
- `unpivot_columns` - Convert wide format to long format

### 4. Verification Loop
After extraction, the `OutputVerifier` checks if the result is a proper flat table. If issues remain (e.g., unfilled parent values), it suggests additional tool calls. This loop runs up to 3 times.

### 5. Human-in-the-Loop
Certain "sensitive" tools (those that remove data) trigger a review flag:
- `filter_summary_rows`
- `filter_header_rows`
- `densify_dataframe`

When triggered, the UI shows "Requires Review" so users can verify no critical data was removed.

---

## Troubleshooting

### Common Issues

| Error | Cause | Solution |
|-------|-------|----------|
| `429 Quota Exceeded` | Hit daily API limit | Switch to a model with higher quota |
| `APIConnectionError` | Network blocking Groq | Use Gemini instead |
| `404 Model not found` | Model ID invalid | Run `python list_models.py` to see available models |
| `0 rows` in output | Summary rows removed | Check `filter_summary_rows` logic |

### Debug Mode
Enable `debug_enabled: true` in settings to:
- See detailed LLM prompts and responses in the Debug tab
- View execution trace with all tool calls
- Export debug data to Excel

---

## License

Internal tool - Kantar

---

## Support

For issues, contact the development team or check the Debug tab for detailed error traces.
