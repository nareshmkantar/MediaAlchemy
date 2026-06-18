# Prompts Directory

This directory contains externalized system prompts for the LLM components.

## Files

| File | Component | Purpose |
|------|-----------|---------|
| `structure_analyzer.md` | StructureAnalyzer | Analyzes spreadsheet grid structure |
| `plan_generator.md` | PlanGenerator | Creates extraction plans |

## Format

Each prompt file uses YAML frontmatter + markdown body:

```markdown
---
version: "2.0"
component: structure_analyzer
---

[Actual system prompt content here]
```

## Editing Prompts

1. Edit the markdown file directly
2. The prompt is reloaded on each agent run
3. Use proper markdown formatting for readability
4. Test after making changes

## Version History

- v2.0: Added comprehensive domain knowledge from legacy heuristics
- v1.0: Initial prompts
