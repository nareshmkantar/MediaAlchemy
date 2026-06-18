"""JSON helpers that tolerate datetimes, pandas/numpy scalars in debug and LLM prompts."""

from __future__ import annotations

import datetime
import json
from typing import Any

import numpy as np
import pandas as pd


def json_safe_default(obj: Any) -> Any:
    """``json.dumps(..., default=json_safe_default)`` handler."""
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
        return obj.item()
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


def dumps(obj: Any, **kwargs: Any) -> str:
    """Like ``json.dumps`` with safe defaults for planner/debug payloads."""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", json_safe_default)
    return json.dumps(obj, **kwargs)
