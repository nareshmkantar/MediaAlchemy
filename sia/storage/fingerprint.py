"""Stable fingerprinting for storage artifacts.

A fingerprint is a short, deterministic digest over the inputs that, when
changed, must invalidate a derived artifact. Callers typically combine:

- source file hash or modification time
- approved scope/layout payload
- approved mappings payload
- approved business rules payload
- template version or fingerprint

Using :func:`compute_fingerprint` keeps the hashing logic consistent across
the storage and orchestration layers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(value: Any) -> Any:
    """Return a JSON-serializable canonical representation of ``value``.

    Dicts are key-sorted recursively so equivalent payloads hash identically
    regardless of the Python insertion order. Unknown objects are coerced to
    strings as a safe fallback.
    """
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value.keys())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def compute_fingerprint(*parts: Any) -> str:
    """Compute a stable 16-character fingerprint for ``parts``.

    Each part is canonicalized (sorted-key JSON), concatenated with a null
    separator, and hashed with SHA-256. The first 16 hex characters are
    returned so the fingerprint is short enough for paths while remaining
    collision-resistant for realistic cardinalities.
    """
    hasher = hashlib.sha256()
    for idx, part in enumerate(parts):
        if idx:
            hasher.update(b"\x00")
        serialized = json.dumps(_canonical(part), sort_keys=True, ensure_ascii=False, default=str)
        hasher.update(serialized.encode("utf-8"))
    return hasher.hexdigest()[:16]
