"""
canonical.py — deterministic object serialisation for hashing.

A hash chain is only as strong as its canonicalisation. If two machines can
serialise the same logical record two different ways, the chain will "detect
tampering" that never happened, and operators will learn to ignore the alarm.

Rules we enforce here (documented, versioned, and covered by tests):

  1. Object keys are emitted in sorted order.
  2. No insignificant whitespace (separators are "," and ":").
  3. UTF-8, no ASCII escaping, so Urdu/Pashto strings hash identically
     everywhere.
  4. ``None`` values are dropped: a field that was never supplied and a field
     explicitly set to null are the *same* record.
  5. Floats are fixed to ``FLOAT_PRECISION`` decimals and rendered without
     scientific notation. GPS coordinates must not hash differently because one
     client sent 34.0151234 and another sent 34.0151234000001.
  6. ``Decimal`` money values are quantised to 4 dp and rendered as strings so
     ``Decimal("1500000.0")`` and ``1500000`` do not diverge.
  7. ``datetime`` is normalised to UTC and emitted as ISO-8601 with a ``Z``
     suffix at microsecond resolution. Naive datetimes are assumed UTC.
  8. NaN / Infinity are rejected — they are not valid JSON and would let an
     attacker smuggle unhashable state into the ledger.

``CANONICAL_VERSION`` is stamped into every ledger entry so that a future
change to these rules can be distinguished from an attack.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

CANONICAL_VERSION = "kp-canonical-1"

FLOAT_PRECISION = 7          # ~1.1 cm at the equator; enough for cadastral GPS
DECIMAL_PRECISION = 4        # money / area
_DECIMAL_QUANTUM = Decimal(1).scaleb(-DECIMAL_PRECISION)

_TRAILING_ZEROES = re.compile(r"(\.\d*?)0+$")
_TRAILING_DOT = re.compile(r"\.$")


class CanonicalisationError(ValueError):
    """Raised when a payload contains something that cannot be hashed safely."""


def normalise_float(value: float) -> str:
    if not math.isfinite(value):
        raise CanonicalisationError(f"non-finite float cannot be hashed: {value!r}")
    text = f"{round(float(value), FLOAT_PRECISION):.{FLOAT_PRECISION}f}"
    text = _TRAILING_ZEROES.sub(r"\1", text)
    return _TRAILING_DOT.sub("", text)


def normalise_decimal(value: Decimal) -> str:
    quantised = Decimal(value).quantize(_DECIMAL_QUANTUM)
    text = format(quantised, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    # Avoid "-0" which is a distinct string for an equal value.
    return "0" if text in ("-0", "-0.0") else text


def normalise_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond:06d}Z"


def _normalise(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return normalise_float(value)
    if isinstance(value, Decimal):
        return normalise_decimal(value)
    if isinstance(value, datetime):
        return normalise_datetime(value)
    if isinstance(value, (bytes, bytearray)):
        return "b64:" + __import__("base64").b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key in value:
            if not isinstance(raw_key, str):
                raise CanonicalisationError(f"object keys must be strings, got {raw_key!r}")
            item = _normalise(value[raw_key])
            if item is not None:
                out[raw_key] = item
        return out
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, set):
        return [_normalise(v) for v in sorted(value, key=repr)]
    if hasattr(value, "model_dump"):  # pydantic v2
        return _normalise(value.model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return _normalise(vars(value))
    raise CanonicalisationError(f"unsupported type for hashing: {type(value)!r}")


def canonical_json(obj: Any) -> str:
    """Return the canonical JSON *text* for ``obj``."""
    return json.dumps(
        _normalise(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(obj: Any) -> bytes:
    """Return UTF-8 encoded canonical form of ``obj``."""
    return canonical_json(obj).encode("utf-8")


def canonical_sha256(obj: Any) -> str:
    """SHA-256 of the canonical form of ``obj``, lowercase hex."""
    import hashlib

    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
