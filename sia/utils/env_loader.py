"""Load and update project .env for local secrets (never committed to git)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

PROVIDER_ENV_MAP = {
    "openai": "OPENAI_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}

SECRET_CONFIG_KEYS = frozenset({"api_key", "keys"})


def provider_env_var(provider: str) -> Optional[str]:
    return PROVIDER_ENV_MAP.get(provider)


def load_project_dotenv() -> None:
    """Load .env from project root if python-dotenv is installed."""
    if not ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ENV_FILE, override=True)


def update_dotenv(updates: Dict[str, str]) -> None:
    """Merge secrets into local .env and apply them to the current process."""
    filtered = {k: v for k, v in updates.items() if k and v}
    if not filtered:
        return

    existing: Dict[str, str] = {}
    preserved_lines: list[str] = []

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                preserved_lines.append(line)
                continue
            key, _, value = stripped.partition("=")
            existing[key.strip()] = value.strip()

    existing.update(filtered)
    for key, value in filtered.items():
        os.environ[key] = value

    ordered_keys = [
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GROQ_API_KEY",
        "SIA_ENABLE_LLM_JUDGE",
    ]
    written: set[str] = set()
    lines: list[str] = []

    for key in ordered_keys:
        if key in existing:
            lines.append(f"{key}={existing[key]}")
            written.add(key)

    for key in sorted(existing):
        if key not in written:
            lines.append(f"{key}={existing[key]}")

    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
