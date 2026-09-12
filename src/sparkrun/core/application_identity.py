"""Portable application/controller identity for plugins and shared services.

These descriptors contain no configuration, credentials, or local paths. They
can coexist without changing the process's selected application profile.
Ownership metadata is provenance, not an authentication credential.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import re
from typing import TYPE_CHECKING
from uuid import uuid4

from sparkrun.core.application_profile import ApplicationProfile, get_application_profile
from sparkrun.core.ownership import OWNER_LABEL, CONTROLLER_LABEL

if TYPE_CHECKING:
    from sparkrun.core.config import SparkrunConfig

_NAME = re.compile(r"[a-z][a-z0-9-]{0,47}")


def _name(value, field):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("Invalid %s: %r" % (field, value))
    return value


@dataclass(frozen=True)
class ApplicationIdentity:
    """Public identity only; distinct from the full application policy profile."""

    id: str
    command: str
    package: str
    resource_namespace: str

    def __post_init__(self):
        for field in ("id", "command", "resource_namespace"):
            _name(getattr(self, field), field)
        if not isinstance(self.package, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.package):
            raise ValueError("Invalid application package: %r" % self.package)

    def to_dict(self) -> dict:
        return {"id": self.id, "command": self.command, "package": self.package, "resource_namespace": self.resource_namespace}

    @classmethod
    def from_dict(cls, data: Mapping) -> ApplicationIdentity:
        return cls(**{key: data[key] for key in ("id", "command", "package", "resource_namespace")})


def get_application_identity(profile: ApplicationProfile | None = None) -> ApplicationIdentity:
    """Describe a profile without selecting it or initializing plugins/state."""
    profile = profile if profile is not None else get_application_profile()
    return ApplicationIdentity(profile.id, profile.command, profile.package, profile.resource_namespace)


@dataclass(frozen=True)
class ControllerIdentity:
    """An application instance associated with one canonical config directory.

    Shared services should key controller state by ``scope_key`` and resource
    state by ``(*scope_key, resource_id)``; resource names alone are insufficient.
    """

    application: ApplicationIdentity
    controller_id: str

    def __post_init__(self):
        if not isinstance(self.application, ApplicationIdentity):
            raise TypeError("application must be an ApplicationIdentity")
        _name(self.controller_id, "controller_id")

    @property
    def scope_key(self) -> tuple[str, str]:
        return self.application.id, self.controller_id

    def labels(self) -> dict[str, str]:
        return {OWNER_LABEL: self.application.id, CONTROLLER_LABEL: self.controller_id}

    def owns(self, labels: Mapping[str, str]) -> bool:
        """Match both labels explicitly; unlabelled legacy resources do not match."""
        return all(labels.get(key) == value for key, value in self.labels().items())

    def to_dict(self) -> dict:
        return {"schema_version": 1, "application": self.application.to_dict(), "controller_id": self.controller_id}

    @classmethod
    def from_dict(cls, data: Mapping) -> ControllerIdentity:
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("Unsupported controller identity schema_version")
        return cls(ApplicationIdentity.from_dict(data["application"]), data["controller_id"])


def get_controller_identity(config: SparkrunConfig | None = None) -> ControllerIdentity:
    """Return the opaque identity for this application and config directory.

    All config files and processes using the same application and canonical
    directory share one ID. A private random seed is persisted atomically and
    bound to the canonical directory when deriving the public opaque ID.
    Copies/moves to another directory have a different identity; restoring the
    same directory preserves it. Configuration cannot override it.
    """
    from sparkrun.core.config import SparkrunConfig
    from sparkrun.utils.fs import atomic_private_write

    config = config if config is not None else SparkrunConfig()
    application = get_application_identity(config.profile)
    config_dir = config.config_path.expanduser().resolve().parent
    path = config_dir / ".controllers" / (application.id + ".id")
    try:
        controller_id = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_private_write(path, "c-" + uuid4().hex + "\n", overwrite=False)
        controller_id = path.read_text(encoding="utf-8").strip()
    # Validate the persisted seed before deriving anything. Corruption must not
    # silently replace an identity. The seed alone is never the ownership key:
    # copying an initialized directory must establish a distinct controller.
    _name(controller_id, "controller_id")
    material = "\0".join((application.id, str(config_dir), controller_id)).encode("utf-8")
    return ControllerIdentity(application, "c-" + hashlib.sha256(material).hexdigest()[:32])
