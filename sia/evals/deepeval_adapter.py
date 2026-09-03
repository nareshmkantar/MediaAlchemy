"""Optional DeepEval integration.

DeepEval is used as the LLM-as-judge *scoring engine* for agentic metrics
(Task Completion, Plan Quality). Everything here is defensive: if ``deepeval``
is not installed, no model is available, or a metric call fails, callers get
``None`` and fall back to the homegrown deterministic scores in
``sia.evals.quality_score``.

We deliberately use ``GEval`` (stable across DeepEval versions and works on a
plain ``LLMTestCase``) rather than the trace-only ``TaskCompletionMetric`` so we
do not have to thread DeepEval's ``@observe`` tracing through the LangGraph
pipeline.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Keep DeepEval fully local: opt out of Confident AI telemetry / anonymized
# usage tracking so no data leaves the environment. Set before any deepeval
# import. Users can still override these explicitly in their own env.
for _key, _val in (
    ("DEEPEVAL_TELEMETRY_OPT_OUT", "YES"),
    ("CONFIDENT_TELEMETRY_OPT_OUT", "YES"),
    ("ERROR_REPORTING", "NO"),
    ("DEEPEVAL_DISABLE_PROGRESS_BAR", "YES"),
):
    os.environ.setdefault(_key, _val)

_DEEPEVAL_IMPORT_ERROR: Optional[str] = None


def deepeval_available() -> bool:
    """True when the ``deepeval`` package can be imported."""
    global _DEEPEVAL_IMPORT_ERROR
    try:
        import deepeval  # noqa: F401

        return True
    except Exception as exc:  # pragma: no cover - depends on env
        _DEEPEVAL_IMPORT_ERROR = str(exc)
        return False


def _build_model(llm_client: Any):
    """Wrap the app's LLM client as a ``DeepEvalBaseLLM`` (or return None)."""
    if llm_client is None:
        return None
    try:
        from deepeval.models.base_model import DeepEvalBaseLLM
    except Exception as exc:  # pragma: no cover
        logger.info("DeepEval base model import failed: %s", exc)
        return None

    class SchemaAgentDeepEvalModel(DeepEvalBaseLLM):
        """Adapter so DeepEval reuses the already-configured project LLM."""

        def __init__(self, client: Any):
            self._client = client
            self._name = getattr(client, "model_name", None) or "schema-agent-llm"

        def load_model(self):
            return self._client

        def _raw_generate(self, prompt: str) -> str:
            client = self._client
            try:
                if hasattr(client, "generate_content"):
                    resp = client.generate_content(prompt)
                    return resp.text if hasattr(resp, "text") else str(resp)
                if callable(client):
                    return str(client(prompt))
            except Exception as exc:  # pragma: no cover
                logger.warning("DeepEval model generate failed: %s", exc)
                raise
            raise AttributeError("LLM client has no generate_content method")

        def generate(self, prompt: str, *args, **kwargs) -> str:
            return self._raw_generate(prompt)

        async def a_generate(self, prompt: str, *args, **kwargs) -> str:
            import asyncio

            client = self._client
            if hasattr(client, "generate_content_async"):
                try:
                    resp = client.generate_content_async(prompt)
                    if hasattr(resp, "__await__"):
                        resp = await resp
                    return resp.text if hasattr(resp, "text") else str(resp)
                except Exception as exc:  # pragma: no cover
                    logger.warning("DeepEval async generate failed: %s", exc)
            return await asyncio.to_thread(self._raw_generate, prompt)

        def get_model_name(self) -> str:
            return str(self._name)

    try:
        return SchemaAgentDeepEvalModel(llm_client)
    except Exception as exc:  # pragma: no cover
        logger.info("Could not build DeepEval model wrapper: %s", exc)
        return None


def score_geval(
    *,
    name: str,
    criteria: str,
    input_text: str,
    actual_output: str,
    llm_client: Any,
    threshold: float = 0.7,
    evaluation_steps: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Run a single GEval metric. Returns ``{score, reason, threshold, success}`` or None.

    Score is normalized to 0..1. Any failure (missing package, model, or runtime
    error) returns ``None`` so the caller can fall back.
    """
    if not deepeval_available():
        return None
    model = _build_model(llm_client)
    if model is None:
        return None

    try:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams
    except Exception as exc:  # pragma: no cover
        logger.info("DeepEval metric import failed: %s", exc)
        return None

    try:
        kwargs: Dict[str, Any] = {
            "name": name,
            "model": model,
            "threshold": float(threshold),
            "evaluation_params": [
                LLMTestCaseParams.INPUT,
                LLMTestCaseParams.ACTUAL_OUTPUT,
            ],
        }
        if evaluation_steps:
            kwargs["evaluation_steps"] = list(evaluation_steps)
        else:
            kwargs["criteria"] = criteria

        metric = GEval(**kwargs)
        test_case = LLMTestCase(input=input_text, actual_output=actual_output)
        metric.measure(test_case)
        score = metric.score
        if score is None:
            return None
        return {
            "score": max(0.0, min(1.0, float(score))),
            "reason": str(getattr(metric, "reason", "") or ""),
            "threshold": float(threshold),
            "success": bool(getattr(metric, "success", score >= threshold)),
            "engine": "deepeval_geval",
            "metric_name": name,
        }
    except Exception as exc:  # pragma: no cover - runtime/model failure
        logger.warning("DeepEval GEval '%s' failed: %s", name, exc)
        return None
