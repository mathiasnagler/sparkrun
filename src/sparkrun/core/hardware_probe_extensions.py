"""Plugin-owned target probe fragments and hardware enrichment.

Registration does not probe the controller or import plugins. Call the normal
application bootstrap first to load selected integrations. Fragments run in a
subshell inside the target's accelerator probe and emit namespaced KEY=VALUE
facts; enrichers must leave unrelated hardware untouched. Errors propagate so a
failed parser cannot silently substitute another platform's hardware.
"""

from __future__ import annotations

from sparkrun.core.registration import enlist_registry_state, register_unique

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import re

from sparkrun.core.hardware import HostHardware


@dataclass(frozen=True)
class HardwareProbeExtension:
    name: str
    script: str
    enrich: Callable[[Mapping[str, str], HostHardware], HostHardware]


_PROBES: dict[str, HardwareProbeExtension] = {}

enlist_registry_state(globals(), "_PROBES")


def register_hardware_probe(name: str, *, script: str, enrich: Callable[[Mapping[str, str], HostHardware], HostHardware]) -> None:
    """Register a read-only target probe; repeated identical registrations are inert."""
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ValueError("Hardware probe needs a lowercase integration name")
    if not isinstance(script, str) or not script.strip() or not callable(enrich):
        raise TypeError("Hardware probe requires a shell fragment and an enricher")
    spec = HardwareProbeExtension(name, script, enrich)
    register_unique(_PROBES, name, spec, description="Hardware probe")


def hardware_probe_script() -> str:
    """Compose registered fragments without sharing their shell variables."""
    return "\n".join("# Hardware probe: %s\n(\n%s\n)\n" % (spec.name, spec.script) for spec in _PROBES.values())


def enrich_host_hardware(parsed: Mapping[str, str], hardware: HostHardware) -> HostHardware:
    """Apply selected integration enrichers before the final fingerprint is computed."""
    for spec in _PROBES.values():
        hardware = spec.enrich(parsed, hardware)
        if not isinstance(hardware, HostHardware):
            raise TypeError("Hardware probe %r did not return HostHardware" % spec.name)
    return hardware
