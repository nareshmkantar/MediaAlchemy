"""Pytest hooks shared across the test suite."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Pipeline tests exercise ingest, which learns aliases and values. Send those
# writes to a scratch file so the suite never edits config/learned_mappings.json.
os.environ.setdefault(
    "SIA_LEARNED_MAPPINGS_PATH",
    str(Path(tempfile.gettempdir()) / "sia_test_learned_mappings.json"),
)

from sia.utils.env_loader import load_project_dotenv

load_project_dotenv()
