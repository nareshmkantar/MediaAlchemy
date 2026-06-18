import os
import json
import time
from pathlib import Path
from typing import List, Dict, Any
from sia.agent.orchestrator import StructureInferenceAgent

class TestScenario:
    def __init__(self, id: str, name: str, description: str, file_path: str, expected_tools: List[str] = None):
        self.id = id
        self.name = name
        self.description = description
        self.file_path = file_path
        self.expected_tools = expected_tools or []

class TestRunner:
    def __init__(self, api_key: str = None):
        self.api_key = api_key
        self.scenarios = [
            TestScenario(
                "merged_cells",
                "Merged Cells Recovery",
                "Tests the 'fill_merged_cells' tool using a real-world messy header slice.",
                "samples/chaos/merged_cells_test.xlsx",
                ["fill_merged_cells"]
            ),
            TestScenario(
                "unpivot_pivot",
                "Pivot/Unpivot Transformation",
                "Tests if the agent identifies and correctly unpivots column-based dimensions.",
                "samples/chaos/pivot_test.xlsx",
                ["transform.unpivot"]
            ),
            TestScenario(
                "multi_block",
                "Multi-Block Layout",
                "Verifies the agent can detect and merge separate data blocks.",
                "samples/chaos/multi_block_test.xlsx",
                ["extract_data_block", "merge_blocks"]
            ),
            TestScenario(
                "real_world_full",
                "Full Real-World Pipeline",
                "Complete processing of the Test_2023 dataset with multiple transformation steps.",
                "samples/chaos/real_world_2023.xlsx"
            )
        ]

    def get_scenarios(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": s.id,
                "name": s.name,
                "description": s.description,
                "file_path": s.file_path,
                "expected_tools": s.expected_tools
            } for s in self.scenarios
        ]

    def run_test(self, scenario_id: str) -> Dict[str, Any]:
        scenario = next((s for s in self.scenarios if s.id == scenario_id), None)
        if not scenario:
            return {"success": False, "error": f"Scenario {scenario_id} not found"}

        file_path = Path(scenario.file_path)
        if not file_path.exists():
            return {"success": False, "error": f"File not found: {scenario.file_path}"}

        start_time = time.time()
        try:
            agent = StructureInferenceAgent(api_key=self.api_key, debug_enabled=True)
            schema, df, trace = agent.process_file(str(file_path))
            
            duration = time.time() - start_time
            
            # Analyze tools used in trace
            tools_used = []
            if trace and hasattr(trace, 'steps'):
                for step in trace.steps:
                    # In our current trace, tool usage is often mentioned in the output summary
                    output = step.get("output", "").lower()
                    for potential_tool in ["transform.fill_merged", "transform.unpivot", "layout.extract", "layout.stack"]:
                        if potential_tool.lower() in output:
                            tools_used.append(potential_tool)
                    
                    # Also check for explicit tool key if added in future
                    if "tool" in step:
                        tools_used.append(step["tool"])
            
            # Simple validation: did it run successfully?
            status = "passed" if df is not None and not df.empty else "failed"
            
            # Check if expected tools were used
            tool_match = True
            missing_tools = []
            for tool in scenario.expected_tools:
                if tool not in tools_used:
                    tool_match = False
                    missing_tools.append(tool)
            
            if tool_match and status == "passed":
                message = "Test passed: Data extracted and expected tools utilized."
            elif status == "passed":
                message = f"Partially passed: Data extracted but missing tools: {', '.join(missing_tools)}"
            else:
                message = "Test failed: No data extracted."

            return {
                "success": True,
                "scenario_id": scenario_id,
                "status": status,
                "duration": round(duration, 2),
                "tools_used": list(set(tools_used)),
                "rows_extracted": len(df) if df is not None else 0,
                "cols_extracted": len(df.columns) if df is not None else 0,
                "message": message,
                "confidence": getattr(trace, "overall_confidence", 0) if trace else 0
            }

        except Exception as e:
            return {
                "success": False,
                "scenario_id": scenario_id,
                "status": "error",
                "error": str(e)
            }
