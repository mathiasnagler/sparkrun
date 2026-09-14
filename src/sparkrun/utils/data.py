"""Detached data values for configuration and public snapshots; no I/O."""

from __future__ import annotations
from collections.abc import Mapping
from datetime import date
from pathlib import PurePath
from types import MappingProxyType
from typing import Any


def freeze(value: Any, *, label: str = "Data") -> Any:
    """Make primitive mappings/sequences recursively read-only."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item, label=label) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item, label=label) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("%s must contain only mappings, sequences and scalar values" % label)


def thaw(value: Any) -> Any:
    """Copy read-only data into ordinary mutable mappings/lists."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def normalize_data(value: Any, *, path: str = "data") -> Any:
    """Copy serializable values; dates become ISO strings, paths become strings.

    Mappings require string keys; tuples become lists. Other application objects
    are rejected with a field path instead of leaking into a public snapshot.
    """
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("%s must have string keys" % path)
        return {key: normalize_data(item, path="%s.%s" % (path, key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_data(item, path="%s[%d]" % (path, index)) for index, item in enumerate(value)]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("Unsupported %s value: %s" % (path, type(value).__name__))


def integer_setting(value: object, *, key: str) -> int:
    """Convert a numeric configuration scalar, naming invalid input in errors."""
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        raise ValueError("Configuration %r must be numeric" % key)
    try:
        return int(value)
    except (ValueError, OverflowError) as error:
        raise ValueError("Configuration %r must be numeric" % key) from error
