"""Host-owned isolation for optional vendored plugin tests."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def enable_tested_optional_plugin(request, monkeypatch):
    if Path(request.node.path).is_relative_to(Path(__file__).parent / "coldsnap"):
        monkeypatch.setenv("SPARKRUN_FEATURE_PLUGINS_COLDSNAP", "1")
