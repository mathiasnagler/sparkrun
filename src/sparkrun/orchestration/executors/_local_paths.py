"""Private native path policy shared by command generation and saved destinations."""

from collections.abc import Mapping


def home_relative(path: str) -> str | None:
    """Return the suffix only when the original path opts into home expansion."""
    if path in ("~", "$HOME", "${HOME}"):
        return ""
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if path.startswith(prefix):
            return path[len(prefix) :]
    return None


def anchored_path(path: object) -> bool:
    """Whether a remote path is independent of the invocation's working directory."""
    return isinstance(path, str) and (path.startswith("/") or home_relative(path) is not None)


def validate_managed_paths(config: Mapping) -> None:
    """Managed state must remain addressable across invocations; defaults are anchored."""
    if config.get("pid_file"):
        raise ValueError(
            "LocalExecutor pid_file is unsupported for managed workloads; use pid_dir. "
            "Legacy command helpers remain available for recovery."
        )
    for name in ("pid_dir", "log_dir", "log_file"):
        path = config.get(name)
        if path and not anchored_path(path):
            raise ValueError(
                "LocalExecutor %s requires an absolute or home-relative path for managed workloads. "
                "Use low-level command helpers in the original directory to recover legacy relative-path jobs." % name
            )
