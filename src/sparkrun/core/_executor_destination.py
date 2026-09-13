"""Private namespace identity shared by placement, observations and job recovery."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutorDestination:
    executor: str
    key: str | None
    user_scoped: bool = False
    ssh_user: str | None = None

    @classmethod
    def from_target(cls, target, ssh_user=None):
        return cls(
            target.executor,
            _validated_destination_key(target.executor, target.destination_key, target.config),
            target.user_scoped,
            ssh_user or None,
        )

    @classmethod
    def from_metadata(cls, metadata, *, target):
        key = _validated_destination_key(
            metadata.get("executor"), metadata.get("executor_destination_key"), metadata.get("executor_config") or {}
        )
        return cls(
            metadata.get("executor") or "",
            key,
            bool(metadata.get("executor_user_scoped")) or target.user_scoped,
            metadata.get("ssh_user") or None,
        )

    @property
    def known(self):
        return bool(self.executor) and self.key is not None and (not self.user_scoped or bool(self.ssh_user))

    def matches(self, other):
        return (
            self.known
            and other.known
            and self.executor == other.executor
            and self.key == other.key
            and (not (self.user_scoped or other.user_scoped) or bool(self.ssh_user) and self.ssh_user == other.ssh_user)
        )

    def placement_key(self):
        """Keep existing user-independent default IDs; unknowns cannot be hashed."""
        if not self.known:
            raise ValueError("Cannot derive deterministic placement from an unknown executor destination")
        if not self.key and not self.user_scoped:
            return None
        result = {"executor": self.executor, "key": self.key}
        if self.user_scoped:
            result["ssh_user"] = self.ssh_user
        return result


def _validated_destination_key(executor, key, config):
    if executor == "local":
        from sparkrun.orchestration.executors._local_paths import anchored_path, validate_managed_paths

        # Old fixed-file/relative-path records and cached targets are outside
        # managed discovery. Their matching spelling cannot establish absence.
        try:
            validate_managed_paths(config)
        except ValueError:
            return None
        if key and not anchored_path(key):
            return None
    return key


def resolve_destination_user(target, hosts, ssh_kwargs):
    """Materialize namespace-relevant transport before a new workload starts."""
    user = (ssh_kwargs or {}).get("ssh_user")
    if not target.user_scoped:
        return user
    from sparkrun.orchestration._ssh_identity import resolve_ssh_user

    return resolve_ssh_user(hosts, **{key: (ssh_kwargs or {}).get(key) for key in ("ssh_user", "ssh_key", "ssh_options")})


def metadata_executor_overrides(metadata):
    """Use the same saved executor settings for every lifecycle reader."""
    if not metadata:
        return None
    result = dict(metadata.get("executor_config") or {})
    if metadata.get("executor"):
        result["executor"] = metadata["executor"]
    return result or None


def job_ssh_kwargs(target, metadata, ssh_kwargs, *, explicit_user=False):
    """Keep a saved user-scoped job's principal while allowing credential rotation.

    A missing principal on an old record cannot authorize inspecting another
    namespace. Such a job requires fresh discovery before automatic teardown.
    """
    result = dict(ssh_kwargs or {})
    if not metadata:
        return result
    recorded = ExecutorDestination.from_metadata(metadata, target=target)
    if recorded.executor == "local" and metadata.get("executor_destination_key") is not None and recorded.key is None:
        raise ValueError("Recorded native destination is unsupported or unanchored; use low-level command recovery")
    if not recorded.user_scoped:
        return result
    if not recorded.ssh_user:
        raise ValueError("Recorded SSH user is unknown for this job; use fresh status discovery before teardown")
    if explicit_user and result.get("ssh_user") != recorded.ssh_user:
        raise ValueError("SSH user differs from the recorded job destination; use fresh status discovery")
    result["ssh_user"] = recorded.ssh_user
    return result
