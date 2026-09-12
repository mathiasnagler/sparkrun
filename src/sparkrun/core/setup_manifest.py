"""Setup manifest tracking for sparkrun setup operations.

Tracks which setup phases have been applied to a cluster, enabling
the uninstall command to reverse them. Manifests are stored as YAML
files alongside cluster definitions.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from threading import RLock
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sparkrun.core.application_profile import get_application_profile

logger = logging.getLogger(__name__)


@dataclass
class PhaseRecord:
    """Record of a single setup phase applied to a cluster."""

    applied: bool
    timestamp: str  # ISO 8601
    hosts: list[str]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SetupManifest:
    """Manifest of all setup phases applied to a cluster."""

    version: int
    cluster: str
    created: str  # ISO 8601
    updated: str  # ISO 8601
    user: str
    hosts: list[str]
    phases: dict[str, PhaseRecord] = field(default_factory=dict)
    distribution: str = field(default_factory=lambda: get_application_profile().id)


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("%s must be a mapping with string keys" % field)
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError("%s must be a string" % field)
    return value


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError("%s must be a list of nonempty strings" % field)
    return value


def validate_setup_details(value: Any, *, field: str = "extra") -> dict[str, Any]:
    """Validate core-owned detail fields, retaining other plugin-owned values."""
    details = _mapping(value, field)
    for key in ("files", "cx7_ips", "mesh_hosts"):
        if key in details:
            _strings(details[key], field + "." + key)
    if "installed_package" in details and type(details["installed_package"]) is not bool:
        raise ValueError(field + ".installed_package must be a boolean")
    if "host_details" in details:
        for host, values in _mapping(details["host_details"], field + ".host_details").items():
            if not host:
                raise ValueError(field + ".host_details requires nonempty host names")
            validate_setup_details(values, field=field + ".host_details." + host)
    return details


def _decode_manifest(data: Any, cluster_name: str, version: int) -> SetupManifest:
    data = _mapping(data, "Setup manifest")
    if not data:
        raise ValueError("Setup manifest must be a nonempty mapping")
    if type(data.get("version", version)) is not int or data.get("version", version) != version:
        raise ValueError("Unsupported setup manifest version: %r" % data.get("version"))
    if data.get("distribution", "sparkrun") != get_application_profile().id:
        raise ValueError("Setup manifest belongs to another application")
    if data.get("cluster", cluster_name) != cluster_name:
        raise ValueError("Setup manifest cluster does not match its filename")
    phases = {}
    for name, pdata in _mapping(data.get("phases", {}), "phases").items():
        if not name:
            raise ValueError("Setup phase name cannot be empty")
        pdata = _mapping(pdata, "phases." + name)
        if type(pdata.get("applied", False)) is not bool:
            raise ValueError("phases.%s.applied must be a boolean" % name)
        phases[name] = PhaseRecord(
            applied=pdata.get("applied", False),
            timestamp=_text(pdata.get("timestamp", ""), "phases.%s.timestamp" % name),
            hosts=_strings(pdata.get("hosts", []), "phases.%s.hosts" % name),
            extra=validate_setup_details(pdata.get("extra", {}), field="phases.%s.extra" % name),
        )
    return SetupManifest(
        version=version,
        cluster=cluster_name,
        created=_text(data.get("created", ""), "created"),
        updated=_text(data.get("updated", ""), "updated"),
        user=_text(data.get("user", ""), "user"),
        hosts=_strings(data.get("hosts", []), "hosts"),
        phases=phases,
    )


class ManifestManager:
    """Manages setup manifest files alongside cluster definitions."""

    MANIFEST_VERSION = 1

    def __init__(self, clusters_dir: Path) -> None:
        self.clusters_dir = Path(clusters_dir)
        self.clusters_dir.mkdir(parents=True, exist_ok=True)
        self._mutex = RLock()
        self._held: set[str] = set()

    def _manifest_path(self, cluster_name: str) -> Path:
        if not isinstance(cluster_name, str) or not cluster_name or cluster_name in {".", ".."} or any(c in cluster_name for c in "/\\"):
            raise ValueError("Invalid setup manifest cluster name: %r" % cluster_name)
        return self.clusters_dir / ("%s.manifest.yaml" % cluster_name)

    @contextmanager
    def _locked(self, cluster_name: str):
        from sparkrun.utils.fs import file_lock

        path = self._manifest_path(cluster_name).with_suffix(".lock")
        with self._mutex:
            if cluster_name in self._held:
                yield
                return
            with file_lock(path):
                self._held.add(cluster_name)
                try:
                    yield
                finally:
                    self._held.remove(cluster_name)

    @contextmanager
    def recording(self, cluster_name: str):
        """Reject unusable existing state before mutation and serialize writers.

        Hold across actions and their records, including frontend-owned topology
        actions. Reentrant for this manager; a missing manifest is permitted.
        """
        with self._locked(cluster_name):
            self.load(cluster_name, strict=True)
            yield

    def load(self, cluster_name: str, *, strict: bool = False) -> SetupManifest | None:
        """Load owned state. Strict reads reject foreign or unreadable files.

        Destructive callers must use ``strict=True`` so only a missing file
        can enable legacy inference. Inventory callers may skip invalid files.
        """
        path = self._manifest_path(cluster_name)
        if not path.exists():
            return None

        try:
            with path.open() as f:
                data = yaml.safe_load(f)
            return _decode_manifest(data, cluster_name, self.MANIFEST_VERSION)
        except Exception as exc:
            if strict:
                raise ValueError("Cannot use setup manifest for %r: %s" % (cluster_name, exc)) from exc
            logger.warning("Failed to load manifest for cluster '%s'", cluster_name, exc_info=True)
            return None

    def save(self, manifest: SetupManifest) -> None:
        """Save manifest to YAML file."""
        with self._locked(manifest.cluster):
            if manifest.distribution != get_application_profile().id:
                raise ValueError("Setup manifest belongs to another distribution")
            path = self._manifest_path(manifest.cluster)
            if path.exists() and self.load(manifest.cluster) is None:
                raise ValueError("Refusing to replace unreadable or foreign setup manifest")

            data = asdict(manifest)
            _decode_manifest(data, manifest.cluster, self.MANIFEST_VERSION)

            from sparkrun.utils.fs import atomic_private_write

            atomic_private_write(path, yaml.safe_dump(data, default_flow_style=False, sort_keys=False))

            logger.debug("Saved manifest for cluster '%s' to %s", manifest.cluster, path)

    def delete(self, cluster_name: str) -> None:
        """Delete manifest file for a cluster."""
        with self._locked(cluster_name):
            path = self._manifest_path(cluster_name)
            if path.exists() and self.load(cluster_name) is not None:
                path.unlink()
                logger.info("Deleted manifest for cluster '%s'", cluster_name)

    def clear_phase_hosts(self, cluster_name: str, phase: str, hosts: list[str]) -> None:
        """Forget only confirmed successful undo targets, retaining all others."""
        with self.recording(cluster_name):
            manifest = self.load(cluster_name, strict=True)
            if manifest is None or phase not in manifest.phases:
                return
            remove_phase_hosts(manifest, phase, hosts)
            manifest.updated = datetime.now(timezone.utc).isoformat()
            self.save(manifest)

    def record_phase(
        self,
        cluster_name: str,
        user: str,
        hosts: list[str],
        phase: str,
        **extra: Any,
    ) -> None:
        """Record a completed setup phase, creating or updating the manifest.

        If a phase already exists, hosts are unioned and extra fields are
        merged (new keys added, existing keys preserved).
        """
        with self.recording(cluster_name):
            now = datetime.now(timezone.utc).isoformat()
            manifest = self.load(cluster_name, strict=True)

            if manifest is None:
                manifest = SetupManifest(
                    version=self.MANIFEST_VERSION,
                    cluster=cluster_name,
                    created=now,
                    updated=now,
                    user=user,
                    hosts=list(hosts),
                    phases={},
                )
            else:
                # Union top-level hosts
                existing_hosts = set(manifest.hosts)
                for h in hosts:
                    if h not in existing_hosts:
                        manifest.hosts.append(h)
                        existing_hosts.add(h)
                manifest.updated = now
                if user:
                    manifest.user = user

            # Upsert phase record
            if phase in manifest.phases:
                existing = manifest.phases[phase]
                # Union hosts
                host_set = set(existing.hosts)
                for h in hosts:
                    if h not in host_set:
                        existing.hosts.append(h)
                        host_set.add(h)
                # Merge extra (existing keys preserved, new keys added)
                for k, v in extra.items():
                    if k == "host_details":
                        details = existing.extra.setdefault(k, {})
                        for host, values in v.items():
                            previous = details.setdefault(host, {})
                            for key, value in values.items():
                                if key == "files":
                                    previous[key] = list(dict.fromkeys([*previous.get(key, []), *value]))
                                elif key == "installed_package":
                                    previous[key] = bool(previous.get(key) or value)
                                else:
                                    previous.setdefault(key, value)
                    elif k not in existing.extra:
                        existing.extra[k] = v
                existing.timestamp = now
                existing.applied = True
            else:
                manifest.phases[phase] = PhaseRecord(
                    applied=True,
                    timestamp=now,
                    hosts=list(hosts),
                    extra=dict(extra),
                )

            self.save(manifest)
            logger.info("Recorded phase '%s' for cluster '%s' (%d hosts)", phase, cluster_name, len(hosts))


def remove_phase_hosts(manifest: SetupManifest, phase: str, hosts: list[str]) -> None:
    """Update a detached manifest after successful undo; shared by recording."""
    record = manifest.phases.get(phase)
    if record is None:
        return
    # Keep the original mesh key sources when some target hosts finish first.
    if phase in {"ssh_mesh", "ssh_mesh_post_cx7"}:
        record.extra["mesh_hosts"] = list(dict.fromkeys([*record.extra.get("mesh_hosts", []), *record.hosts]))
    removed = set(hosts)
    record.hosts = [host for host in record.hosts if host not in removed]
    details = record.extra.get("host_details", {})
    for host in removed:
        details.pop(host, None)
    if not record.hosts:
        del manifest.phases[phase]
