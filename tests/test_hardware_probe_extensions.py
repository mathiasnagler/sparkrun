"""Hardware probe extension registration, execution, and fingerprint integration."""

from dataclasses import replace
import subprocess

import pytest

from sparkrun.core import hardware_probe_extensions as extensions
from sparkrun.core.fingerprint import build_host_hardware, generate_fingerprint_script
from sparkrun.core.hardware import AcceleratorSpec
from sparkrun.core.hardware_probe import _ACCEL_END, generate_combined_probe_script
from sparkrun.core.installed_plugins import PluginConflictError, registration_transaction


@pytest.fixture(autouse=True)
def isolated_probes(monkeypatch):
    monkeypatch.setattr(extensions, "_PROBES", {})


def unchanged(parsed, hw):
    return hw


def test_idempotence_and_conflicts():
    extensions.register_hardware_probe("example", script="emit EXAMPLE_PRESENT 1", enrich=unchanged)
    extensions.register_hardware_probe("example", script="emit EXAMPLE_PRESENT 1", enrich=unchanged)
    assert extensions.hardware_probe_script().count("EXAMPLE_PRESENT") == 1
    with pytest.raises(PluginConflictError, match="already registered"):
        extensions.register_hardware_probe("example", script="emit EXAMPLE_PRESENT 2", enrich=unchanged)


def test_probe_fragment_has_its_own_shell_scope():
    extensions.register_hardware_probe("example", script="value=changed; emit EXAMPLE_PRESENT 1; exit 0", enrich=unchanged)
    script = (
        'set -euo pipefail\nemit() { printf \'%s=%s\\n\' "$1" "$2"; }\nvalue=original\n'
        + extensions.hardware_probe_script()
        + '\necho "$value"\n'
    )
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "EXAMPLE_PRESENT=1\noriginal\n"


def test_fragments_in_both_target_probes_and_fingerprint_after_enrichment():
    def enrich(parsed, hw):
        return replace(hw, accelerators=[AcceleratorSpec("example", parsed["EXAMPLE_MODEL"], memory_gb=4)])

    extensions.register_hardware_probe("example", script="emit EXAMPLE_MODEL test", enrich=enrich)
    for script in (generate_fingerprint_script(), generate_combined_probe_script()):
        assert "emit EXAMPLE_MODEL test" in script
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    combined = generate_combined_probe_script()
    assert combined.index("emit EXAMPLE_MODEL test") < combined.index('echo "%s"' % _ACCEL_END)
    one = build_host_hardware({"EXAMPLE_MODEL": "first"})
    two = build_host_hardware({"EXAMPLE_MODEL": "second"})
    assert one.fingerprint != two.fingerprint


def test_failed_registration_rolls_back_probe():
    from scitrera_app_framework import Variables

    with pytest.raises(RuntimeError, match="broken"):
        with registration_transaction(Variables()):
            extensions.register_hardware_probe("example", script="true", enrich=unchanged)
            raise RuntimeError("broken")
    assert extensions.hardware_probe_script() == ""


def test_bad_enricher_does_not_silently_keep_partial_hardware():
    extensions.register_hardware_probe("example", script="true", enrich=lambda parsed, hw: None)
    with pytest.raises(TypeError, match="did not return HostHardware"):
        build_host_hardware({})
