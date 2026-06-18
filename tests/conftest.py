"""Pytest hooks shared across the test suite."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sia.utils.env_loader import load_project_dotenv

load_project_dotenv()
