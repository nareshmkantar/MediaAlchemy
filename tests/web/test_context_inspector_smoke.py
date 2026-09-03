"""
Phase 10.4: context inspector smoke — API contract + optional JS render.

Validates debug payload shape for ``renderContextInspectorHtml`` and, when Node.js
is available, executes the browser module against a CP-07-style fixture.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sia.context.debug import build_source_context_debug_payload

from tests.fixtures.cp07_frames import cp07_multisheet_job, radio_de_context_packet

REPO_ROOT = Path(__file__).resolve().parents[2]
RENDER_JS = REPO_ROOT / "web" / "js" / "pipeline-evals-render.js"


def _cp07_radio_debug_payload() -> dict:
    job = cp07_multisheet_job()
    radio_sid = job["source_registry"][1]["source_id"]
    job.update(radio_de_context_packet(source_id=radio_sid))
    job["context_block_snippets"] = [
        {
            "block_label": "Top meta",
            "text_preview": ["Market: Germany", "Channel: radio"],
        }
    ]
    job["interpreted_context"] = radio_de_context_packet(source_id=radio_sid)["interpreted_context"]
    return build_source_context_debug_payload(job, radio_sid)


def test_context_inspector_payload_contract():
    """Fields required by Debug tab context inspector."""
    payload = _cp07_radio_debug_payload()
    assert payload["sheet_name"] == "Radio_DE"
    assert payload.get("context_fingerprint")
    assert isinstance(payload.get("scoped_fields"), list)
    assert payload["scoped_fields"]
    market = next(
        (r for r in payload["scoped_fields"] if (r.get("field") or r.get("name")) == "market"),
        None,
    )
    assert market is not None
    assert market.get("value") in ("DE", "Germany")
    assert payload["local_context"].get("market")
    evidence = payload.get("interpreted_context", {}).get("evidence") or []
    assert isinstance(evidence, list)


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js not installed")
def test_context_inspector_js_renders_market_provenance():
    """Smoke: PipelineEvalsRender.renderContextInspectorHtml includes scoped market."""
    payload = _cp07_radio_debug_payload()
    script = f"""
    function escapeHtml(t) {{
      return String(t ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}
    const payload = {json.dumps(payload)};
    {RENDER_JS.read_text(encoding='utf-8')}
    const html = global.PipelineEvalsRender.renderContextInspectorHtml(payload);
    if (!html.includes('Scoped fields')) throw new Error('missing scoped section');
    if (!html.includes('market') && !html.includes('DE') && !html.includes('Germany')) {{
      throw new Error('market not visible in inspector html');
    }}
    if (!html.includes('Evidence')) throw new Error('missing evidence section');
    console.log('ok');
    """
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
