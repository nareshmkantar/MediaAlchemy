"""
Structure Inference Agent - Main Entry Point
Provides CLI and file dialog interface for processing Excel files.
"""
import argparse
import logging
import sys
import os
from pathlib import Path
from typing import Optional

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from sia.utils.env_loader import load_project_dotenv

load_project_dotenv()

from sia.agent.orchestrator import StructureInferenceAgent
from sia.utils.logger import setup_logging


def open_file_dialog() -> Optional[str]:
    """Open a file dialog to select an Excel file."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()  # Hide the main window
        root.attributes('-topmost', True)  # Bring dialog to front
        
        file_path = filedialog.askopenfilename(
            title="Select Excel File",
            filetypes=[
                ("Excel files", "*.xlsx *.xls"),
                ("CSV files", "*.csv"),
                ("All files", "*.*")
            ]
        )
        
        root.destroy()
        return file_path if file_path else None
        
    except ImportError:
        print("Tkinter not available for file dialog. Please provide file path as argument.")
        return None
    except Exception as e:
        print(f"File dialog error: {e}")
        return None


def main():
    """Main entry point for the SIA."""
    parser = argparse.ArgumentParser(
        description="Structure Inference Agent - Extract schemas from messy Excel files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                           # Open file dialog
  python main.py sales_report.xlsx         # Process specific file
  python main.py data.xlsx -o runtime/outputs/  # Specify output directory
  python main.py data.xlsx --format csv    # Output as CSV instead of Parquet
        """
    )
    
    parser.add_argument(
        "file",
        nargs="?",
        help="Path to Excel file (opens file dialog if not provided)"
    )
    
    parser.add_argument(
        "-o", "--output",
        default="runtime/outputs",
        help="Output directory (default: runtime/outputs)"
    )
    
    parser.add_argument(
        "--format",
        choices=["parquet", "csv", "json"],
        default="parquet",
        help="Output data format (default: parquet)"
    )
    
    parser.add_argument(
        "--config",
        default="config/semantic_config.yaml",
        help="Path to semantic config file"
    )
    
    parser.add_argument(
        "--api-key",
        default=os.environ.get("GEMINI_API_KEY", ""),
        help="Gemini API key (default: from GEMINI_API_KEY env var)"
    )

    parser.add_argument(
        "--model",
        default=None,
        help="LLM model name to use (e.g. 'azure-gpt-4o', 'gpt-4o', or 'gemini-3-flash-preview'). Overrides config file."
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode to capture detailed pipeline events"
    )
    
    parser.add_argument(
        "--create-sample",
        action="store_true",
        help="Create a sample messy Excel file for testing"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    setup_logging(level=log_level)
    logger = logging.getLogger("sia.main")
    
    # Create sample file if requested
    if args.create_sample:
        from scripts.create_sample_excel import create_messy_sample
        sample_path = create_messy_sample()
        print(f"\nCreated sample file: {sample_path}")
        print("Run again with this file to process it.")
        return 0
    
    # Get file path
    file_path = args.file
    if not file_path:
        print("\n=== Structure Inference Agent ===")
        print("No file provided. Opening file dialog...")
        file_path = open_file_dialog()
        
        if not file_path:
            print("No file selected. Exiting.")
            return 1
    
    # Validate file exists
    if not Path(file_path).exists():
        print(f"Error: File not found: {file_path}")
        return 1
    
    print(f"\n=== Processing: {file_path} ===\n")
    
    # Initialize agent
    config_path = args.config if Path(args.config).exists() else None
    agent = StructureInferenceAgent(
        config_path=config_path,
        api_key=args.api_key,
        model_name=args.model,
        debug_enabled=args.debug
    )
    
    # Process file
    try:
        schema, df, trace = agent.process_file(file_path)
        
        # Print results
        print("\n=== Processing Results ===")
        print(f"Trace ID: {trace.trace_id}")
        print(f"Overall Confidence: {trace.overall_confidence:.2%}")
        
        if trace.warnings:
            print(f"\nWarnings ({len(trace.warnings)}):")
            for w in trace.warnings:
                print(f"  - {w}")
        
        if trace.errors:
            print(f"\nErrors ({len(trace.errors)}):")
            for e in trace.errors:
                print(f"  - {e}")
        
        print(f"\n=== Inferred Schema: {schema.schema_name} ===")
        print(f"Fields: {len(schema.fields)}")
        for field in schema.fields:
            print(f"  - {field.name}: {field.data_type.value} ({field.role.value}) [conf: {field.confidence:.2f}]")
        
        print(f"\n=== Data Preview ===")
        print(f"Total Rows: {len(df)}")
        print(df.head(10).to_string())
        
        # Save results
        outputs = agent.save_results(schema, df, args.output, format=args.format)
        print(f"\n=== Saved Output ===")
        print(f"Schema: {outputs['schema']}")
        print(f"Data: {outputs['data']}")
        
        # Show debug events if enabled
        if args.debug and trace.debug_enabled and trace.debug_events:
            print(f"\n=== Debug Events ({len(trace.debug_events)} events) ===")
            for i, event in enumerate(trace.debug_events):
                print(f"\n--- Event {i+1}: {event.get('module', 'unknown')}.{event.get('operation', 'unknown')} ---")
                print(f"Type: {event.get('process_type', 'unknown')}")
                print(f"Input: {event.get('input_summary', 'N/A')}")
                print(f"Output: {event.get('output_summary', 'N/A')}")
                if event.get('prompt'):
                    prompt_preview = event['prompt'][:200] + '...' if len(event.get('prompt', '')) > 200 else event.get('prompt', '')
                    print(f"Prompt (preview): {prompt_preview}")
                if event.get('response'):
                    response_preview = event['response'][:200] + '...' if len(event.get('response', '')) > 200 else event.get('response', '')
                    print(f"Response (preview): {response_preview}")
        
        # HITL decision
        from sia.models.confidence import HITLDecision
        decision = agent.get_hitl_decision(trace.overall_confidence)
        
        if decision == HITLDecision.AUTO_APPROVE:
            print("\n[OK] High confidence - Results auto-approved")
        elif decision == HITLDecision.FLAG_FOR_REVIEW:
            print("\n[REVIEW] Medium confidence - Recommended for human review")
        else:
            print("\n[MANUAL] Low confidence - Manual review required")
        
        return 0
        
    except Exception as e:
        logger.exception(f"Processing failed: {e}")
        print(f"\nError: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
