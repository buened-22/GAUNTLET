"""Typed encoding of statement parameters and result rows between an actor process and the harness.

The harness owns every actor's database connection; an actor sends statement parameters to the harness and
receives result rows back as JSON. Plain JSON would turn ``Decimal`` (NUMERIC) into strings or floats, so
non-JSON types are tagged and restored exactly. Anything that cannot be represented faithfully is an error,
never a silent conversion.
"""

from __future__ import annotations

import base64
import datetime as _dt
import uuid
from decimal import Decimal
from typing import Any

TAG = "\x00gauntlet"  # cannot appear as a key in data produced by the implementations' SQL by accident


class WireError(ValueError):
    """A value cannot be encoded, or a message contains a malformed encoded value."""


def encode(v: Any) -> Any:
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return {TAG: "float", "v": repr(v)}
        return v
    if isinstance(v, Decimal):
        return {TAG: "decimal", "v": str(v)}
    if isinstance(v, (list, tuple)):
        return [encode(x) for x in v]
    if isinstance(v, dict):
        if not all(isinstance(k, str) for k in v):
            raise WireError("dictionary keys must be strings")
        return {TAG: "dict", "v": [[k, encode(x)] for k, x in v.items()]}
    if isinstance(v, _dt.datetime):
        return {TAG: "datetime", "v": v.isoformat()}
    if isinstance(v, _dt.date):
        return {TAG: "date", "v": v.isoformat()}
    if isinstance(v, _dt.time):
        return {TAG: "time", "v": v.isoformat()}
    if isinstance(v, _dt.timedelta):
        return {TAG: "timedelta", "v": [v.days, v.seconds, v.microseconds]}
    if isinstance(v, uuid.UUID):
        return {TAG: "uuid", "v": str(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {TAG: "bytes", "v": base64.b64encode(bytes(v)).decode("ascii")}
    raise WireError(f"cannot transmit a value of type {type(v).__name__}")


def decode(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, list):
        return [decode(x) for x in v]
    if not isinstance(v, dict) or set(v) != {TAG, "v"}:
        raise WireError("malformed encoded value")
    kind, payload = v[TAG], v["v"]
    try:
        if kind == "decimal":
            return Decimal(_str(payload))
        if kind == "float":
            return float(_str(payload))
        if kind == "dict":
            if not isinstance(payload, list):
                raise WireError("malformed dictionary")
            out = {}
            for item in payload:
                if not (isinstance(item, list) and len(item) == 2 and isinstance(item[0], str)):
                    raise WireError("malformed dictionary item")
                out[item[0]] = decode(item[1])
            return out
        if kind == "datetime":
            return _dt.datetime.fromisoformat(_str(payload))
        if kind == "date":
            return _dt.date.fromisoformat(_str(payload))
        if kind == "time":
            return _dt.time.fromisoformat(_str(payload))
        if kind == "timedelta":
            if not (isinstance(payload, list) and len(payload) == 3 and all(type(x) is int for x in payload)):
                raise WireError("malformed timedelta")
            return _dt.timedelta(days=payload[0], seconds=payload[1], microseconds=payload[2])
        if kind == "uuid":
            return uuid.UUID(_str(payload))
        if kind == "bytes":
            return base64.b64decode(_str(payload), validate=True)
    except WireError:
        raise
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise WireError(f"malformed {kind} value: {exc}") from exc
    raise WireError(f"unknown encoded type {kind!r}")


def _str(v: Any) -> str:
    if not isinstance(v, str):
        raise WireError("expected a string payload")
    return v


def decode_params(v: Any) -> Any:
    """Statement parameters: None, a positional sequence or a mapping of named parameters."""
    out = decode(v)
    if out is not None and not isinstance(out, (list, dict)):
        raise WireError("statement parameters must be a sequence, a mapping or null")
    return out
