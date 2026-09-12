"""Plugin source must not change registration atomicity or inventory truth."""

from types import ModuleType

import pytest

from test_external_plugins import clean_sys as clean_sys


@pytest.mark.parametrize("source", ["directory", "bundled", "installed"])
@pytest.mark.parametrize("failure_phase", ["import", "hook"])
def test_failed_plugin_rolls_back_and_independent_plugin_still_loads(tmp_path, monkeypatch, clean_sys, source, failure_phase):
    import importlib
    from types import SimpleNamespace
    from sparkrun.core.bootstrap import get_variables
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.core.external_plugins import load_external_plugins, loaded_plugin_module
    from sparkrun.core.in_tree_plugins import load_in_tree_plugins, IN_TREE_PLUGIN_FEATURES
    from sparkrun.core.installed_plugins import load_installed_plugins, installed_plugin_inventory
    from sparkrun.core.features import get_feature, register_feature, FeatureFlag
    from sparkrun.core.cli_registry import registered_cli_commands
    from sparkrun.core.run_handlers import registered_run_handlers
    from sparkrun.core.setup_steps import all_setup_steps
    from sparkrun.transports import list_transports

    v = get_variables()
    before_good = v.get("RECOVERY_GOOD_VALUE", 0)
    package = tmp_path / "recovery_plugins"
    package.mkdir()
    (package / "__init__.py").write_text("")
    body = """
from sparkrun.core.features import FeatureFlag, register_feature
from sparkrun.core.run_handlers import RunHandler, register_run_handler
from sparkrun.core.setup_steps import SetupStep, register_setup_step
from sparkrun.core.cli_registry import register_cli_command
from sparkrun.transports.base import Transport
SPARKRUN_PLUGIN_API_VERSION = 1
class RecoveryTransport(Transport):
    transport_name = "recovery_bad"
def contribute():
    register_feature(FeatureFlag("test.recovery_bad", "bad", default=True))
    register_run_handler(RunHandler("recovery_bad", lambda *a, **kw: None))
    register_setup_step(SetupStep("recovery_bad", "bad", feature_flag="test.recovery_bad"))
    register_cli_command(lambda: None, name="recovery_bad")
def register(v):
    contribute()
    v.set("RECOVERY_BAD_VALUE", True)
    raise RuntimeError("bad registration")
"""
    if failure_phase == "import":
        body += '\ncontribute()\nraise RuntimeError("bad import")\n'
    (package / "recovery_bad.py").write_text(body)
    (package / "recovery_good.py").write_text("""
SPARKRUN_PLUGIN_API_VERSION = 1
def register(v):
    v.set("RECOVERY_GOOD_VALUE", v.get("RECOVERY_GOOD_VALUE", 0) + 1)
""")
    monkeypatch.syspath_prepend(str(tmp_path))
    if source == "directory":
        loaded = load_external_plugins(v, paths=[package])
        prefix = ""
        assert loaded == ["recovery_good"]
    elif source == "bundled":
        gates = dict(IN_TREE_PLUGIN_FEATURES)
        for name in ("recovery_bad", "recovery_good"):
            flag = "test.gate_" + name
            register_feature(FeatureFlag(flag, flag, default=True))
            gates[name] = flag
        monkeypatch.setattr("sparkrun.core.in_tree_plugins.IN_TREE_PLUGIN_FEATURES", gates)
        loaded = load_in_tree_plugins(v, package="recovery_plugins")
        prefix = "recovery_plugins."
        assert loaded == ["recovery_good"]
    else:
        monkeypatch.delenv("SPARKRUN_NO_INSTALLED_PLUGINS")
        monkeypatch.setattr("sparkrun.core.installed_plugins._attempted", set())
        entries = [
            SimpleNamespace(
                name=name,
                value="recovery_plugins." + name,
                dist=SimpleNamespace(name=name, version="1"),
                load=lambda name=name: importlib.import_module("recovery_plugins." + name),
            )
            for name in ("recovery_bad", "recovery_good")
        ]
        monkeypatch.setattr("sparkrun.core.installed_plugins.entry_points", lambda **_: entries)
        config = SparkrunConfig()
        config._data["integrations"] = {name: True for name in ("recovery_bad", "recovery_good")}
        load_installed_plugins(v, config=config)
        rows = {row.name: row for row in installed_plugin_inventory()}
        assert not rows["recovery_bad"].loaded and rows["recovery_bad"].failure
        assert rows["recovery_good"].loaded
        prefix = "recovery_plugins."
    assert loaded_plugin_module(prefix + "recovery_bad") is None
    assert loaded_plugin_module(prefix + "recovery_good") is not None
    assert get_feature("test.recovery_bad") is None
    assert "recovery_bad" not in registered_run_handlers(SparkrunConfig())
    assert "recovery_bad" not in {step.key for step in all_setup_steps()}
    assert "recovery_bad" not in {spec.name for spec in registered_cli_commands()}
    assert "recovery_bad" not in list_transports(v)
    assert not v.get("RECOVERY_BAD_VALUE")
    assert v.get("RECOVERY_GOOD_VALUE") == before_good + 1
    # Registering the successfully loaded module again is a no-op.
    from sparkrun.core.external_plugins import load_plugin_module

    assert load_plugin_module(loaded_plugin_module(prefix + "recovery_good"), v)
    assert v.get("RECOVERY_GOOD_VALUE") == before_good + 1


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_direct_module_registration_restores_state_on_failure(monkeypatch, error):
    from sparkrun.core.bootstrap import get_variables
    from sparkrun.core.external_plugins import load_plugin_module, loaded_plugin_module
    from sparkrun.core.run_handlers import RunHandler, register_run_handler, registered_run_handlers

    from sparkrun.core.config import SparkrunConfig

    v = get_variables()
    monkeypatch.setattr("sparkrun.core.run_handlers._RUN_HANDLERS", {})
    module = ModuleType("recovery_direct")

    def register(v):
        register_run_handler(RunHandler("recovery_direct", lambda *a, **kw: None))
        raise error("failed")

    module.register = register
    if error is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            load_plugin_module(module, v)
    else:
        assert not load_plugin_module(module, v)
    assert not registered_run_handlers(SparkrunConfig())
    assert loaded_plugin_module(module.__name__) is None


@pytest.mark.parametrize("declaration", ["missing", None, True, 1.0, "1", 0, 1, 2])
def test_installed_plugin_api_is_integer_and_independent_of_profile_api(monkeypatch, clean_sys, declaration):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from sparkrun.core.bootstrap import get_variables
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.core.features import FeatureFlag, register_feature, get_feature
    from sparkrun.core.installed_plugins import load_installed_plugins, installed_plugin_inventory
    from sparkrun.core.registration import PLUGIN_API_VERSION

    v = get_variables()
    from sparkrun.core import features

    monkeypatch.setattr(features, "FEATURE_FLAGS", dict(features.FEATURE_FLAGS))
    monkeypatch.setattr("sparkrun.core.application_profile.APPLICATION_PROFILE_API_VERSION", 999)
    monkeypatch.delenv("SPARKRUN_NO_INSTALLED_PLUGINS")
    monkeypatch.setattr("sparkrun.core.installed_plugins._attempted", set())
    module = ModuleType("recovery_version")
    if declaration != "missing":
        module.SPARKRUN_PLUGIN_API_VERSION = declaration
    module.register = Mock()

    def load():
        register_feature(FeatureFlag("test.version_import", "version import", default=True))
        return module

    entry = SimpleNamespace(name="version-test", value=module.__name__, dist=SimpleNamespace(name="version-test", version="0.1"), load=load)
    monkeypatch.setattr("sparkrun.core.installed_plugins.entry_points", lambda **kw: [entry])
    config = SparkrunConfig()
    config._data["integrations"] = {"version-test": True}
    load_installed_plugins(v, config=config)
    row = installed_plugin_inventory()[0]
    accepted = type(declaration) is int and declaration == PLUGIN_API_VERSION
    assert row.loaded is accepted
    assert (get_feature("test.version_import") is not None) is accepted
    if accepted:
        module.register.assert_called_once_with(v)
        assert row.failure is None
    else:
        module.register.assert_not_called()
        assert "Plugin API" in row.failure


@pytest.mark.parametrize("declared", [False, True])
def test_legacy_direct_module_may_omit_but_not_misdeclare_api(monkeypatch, declared):
    from unittest.mock import Mock
    from sparkrun.core.bootstrap import get_variables
    from sparkrun.core.external_plugins import load_plugin_module

    module = ModuleType("legacy_api_declaration")
    module.register = Mock()
    if declared:
        module.SPARKRUN_PLUGIN_API_VERSION = True
    assert load_plugin_module(module, get_variables()) is (not declared)
    assert module.register.call_count == int(not declared)
