"""JSON helpers that tolerate datetimes, pandas/numpy scalars in debug and LLM prompts."""

from __future__ import annotations

import datetime
import json
import math
from typing import Any

import numpy as np
import pandas as pd


def _finite_float(val: float) -> Any:
    if math.isnan(val) or math.isinf(val):
        return None
    return val


def json_safe_default(obj: Any) -> Any:
    """``json.dumps(..., default=json_safe_default)`` handler."""
    if isinstance(obj, float):
        return _finite_float(obj)
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, np.datetime64):
        try:
            return pd.Timestamp(obj).isoformat()
        except Exception:
            return str(obj)
    if isinstance(obj, (np.integer, np.floating, np.bool_)):
        val = obj.item()
        if isinstance(val, float):
            return _finite_float(val)
        return val
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            pass
    if hasattr(obj, "to_dict"):
        try:
            return obj.to_dict()
        except Exception:
            pass
    return str(obj)


def sanitize_json_tree(obj: Any) -> Any:
    """Recursively coerce payloads to strict JSON (NaN/Inf → null)."""
    if isinstance(obj, dict):
        return {k: sanitize_json_tree(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_json_tree(v) for v in obj]
    if isinstance(obj, float):
        return _finite_float(obj)
    if isinstance(obj, (np.floating,)):
        return _finite_float(float(obj))
    return obj


def dumps(obj: Any, **kwargs: Any) -> str:
    """Like ``json.dumps`` with safe defaults for planner/debug payloads."""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", json_safe_default)
    kwargs.setdefault("allow_nan", False)
    return json.dumps(sanitize_json_tree(obj), **kwargs)
