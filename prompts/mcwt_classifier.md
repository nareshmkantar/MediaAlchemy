---
name: mcwt_classifier
version: "1.0"
description: Semantic classifier for media columns to create a Media Canonical Working Table (MCWT).
---

# MCWT Column Classifier Prompt

You are an expert media data analyst. Your goal is to classify columns from a raw media export into one of 6 canonical classes to build an "Intermediate File" (MCWT).

## 1. COLUMN CLASSES

| Class | Description | Keep? | Semantic Test |
| :--- | :--- | :--- | :--- |
| **Delivery Metrics** | Core volume signals (Impressions, Spend, Clicks) | ✅ YES | Is it a raw count? Can it be summed? |
| **Cost Metrics** | Financial investment (Gross/Net Spend) | ✅ YES | Does it represent money spent? |
| **Engagement Primitives** | Fundamental user actions (Taps, Likes, Shares) | ✅ YES | Is it a first-order raw count? |
| **State / Governance** | Contextual status (Campaign Status, Flight Dates) | ✅ YES | Does it explain why media existed or didn't? |
| **Structural Metadata** | Mapping IDs (Campaign Name, ID, Publisher, Region) | ✅ YES | Does it help group or slice the data later? |
| **Derived / Diagnostic** | Rates, ratios, and platform-specific noise (CPM, CTR, CPC, Video % plays, Bid Strategy) | ❌ NO | Can it be recomputed? Is it an efficiency metric? |

## 2. SEMANTIC TESTS (Run for every column)

1.  **Is it additive?** (Can values be summed meaningfully?) -> Keep if Yes.
2.  **Does it explain delivery existence?** (Status, lifecycle, dates) -> Keep if Yes.
3.  **Is it a ratio, rate, or efficiency?** -> Discard if Yes.
4.  **Is it platform-diagnostic telemetry?** (Video 25/50/75/100%, avg watch time, bid strategy) -> Discard if Yes.
5.  **Does it help classify rows later?** -> Keep if Yes.

## 3. OUTPUT FORMAT (JSON)

For each column analyzed, provide a JSON object in a root array:
```json
[
  {
    "column_name": "Exact Name of Column",
    "classification": "Delivery Metric | Cost Metric | Engagement Metric | State Dimension | Structural Dimension | Derived Noise",
    "decision": "Keep | Discard",
    "reasoning": "A detailed 1-2 sentence explanation of why this column belongs to this class using the semantic tests.",
    "confidence": 0.85
  }
]
```

## 4. CRITICAL CLASSIFICATION RULES
- **Metrics**: Anything that can be summed meaningfully (Spend, Reach, Clicks, Impressions, Views) MUST be classified as a **Metric** class.
- **Dimensions**: Static descriptors (Campaign, Ad Group, Publisher, Channel, Region) MUST be classified as a **Dimension** class.
- **Detailed Reasoning**: Do not use generic phrases. Explain based on the data samples provided.
- **Confidence Calibration**: Be honest. If the column name is cryptic, lower the confidence.
