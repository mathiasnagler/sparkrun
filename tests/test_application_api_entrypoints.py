"""Supported application facade and shared API initialization failure contract."""

from unittest.mock import Mock

import pytest


def test_default_api_context_uses_public_initializer(monkeypatch):
    from sparkrun.api import default_sctx
    from sparkrun.application import initialize, ApplicationProfile, UpdateSource, APPLICATION_PROFILE_API_VERSION
    from sparkrun.core import application_profile

    assert ApplicationProfile is application_profile.ApplicationProfile
    assert UpdateSource is application_profile.UpdateSource
    assert APPLICATION_PROFILE_API_VERSION == application_profile.APPLICATION_PROFILE_API_VERSION
    expected = initialize()
    factory = Mock(return_value=expected)
    monkeypatch.setattr("sparkrun.application.initialize", factory)
    assert default_sctx() is expected
    factory.assert_called_once_with()


@pytest.mark.parametrize("operation", ["default", "benchmark", "resume"])
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError, KeyboardInterrupt])
def test_implicit_initialization_errors_are_consistent(monkeypatch, operation, error_type):
    from sparkrun.api import default_sctx, benchmark, resume_benchmark, BenchmarkOptions, SparkrunError

    error = error_type("bad initialization")
    monkeypatch.setattr("sparkrun.application.initialize", Mock(side_effect=error))
    call = {
        "default": default_sctx,
        "benchmark": lambda: benchmark(BenchmarkOptions(recipe="r")),
        "resume": lambda: resume_benchmark("bench_test"),
    }[operation]
    with pytest.raises(KeyboardInterrupt if error_type is KeyboardInterrupt else SparkrunError) as caught:
        call()
    if error_type is KeyboardInterrupt:
        assert caught.value is error
    else:
        assert caught.value.__cause__ is error
