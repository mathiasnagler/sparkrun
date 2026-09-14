"""Presentation helpers retain partial observations and reject invalid input shapes."""

from dataclasses import dataclass

import pytest

from sparkrun.core.monitoring import HostMonitorState
from sparkrun.utils.cli_formatters import format_monitor_table, format_activity_table
from sparkrun.utils.json_helpers import dumps_json


def test_empty_monitor_tables_render_headers():
    assert "HOST" in format_monitor_table({}, [])
    assert "HOST" in format_activity_table(None, [])


def test_monitor_without_sample_handles_empty_error():
    assert "connecting" in format_monitor_table({"h1": HostMonitorState(error="")}, ["h1"])


def test_json_serializes_dataclass_instances_but_not_classes():
    @dataclass
    class Value:
        number: int

    assert dumps_json(Value(4)) == '{"number": 4}'
    with pytest.raises(TypeError, match="not JSON serializable"):
        dumps_json(Value)


def test_missing_nv_monitor_bundle_fails_before_starting_background_threads(monkeypatch):
    import sys
    from unittest.mock import Mock
    from sparkrun.core import monitoring
    from sparkrun.orchestration.telemetry.host import HostTelemetryProvider

    monkeypatch.setitem(sys.modules, "sparkrun.bin", None)
    thread = Mock(side_effect=AssertionError("must fail before background work"))
    monkeypatch.setattr(monitoring.threading, "Thread", thread)
    with pytest.raises(RuntimeError, match="binary assets.*bash"):
        HostTelemetryProvider().open(["h1"], backend="nv-monitor")
    thread.assert_not_called()
