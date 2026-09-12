"""Ephemeral benchmark authentication, injected only into command construction."""

from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from scitrera_app_framework import Variables
from sparkrun.benchmarking.metadata import public_benchmark_data


@dataclass(frozen=True)
class BenchmarkCredentials:
    api_key: str | None = field(default=None, repr=False)

    def arguments(self, args: Mapping[str, Any]) -> dict[str, Any]:
        result = public_benchmark_data(args)
        if self.api_key:
            result["api_key"] = self.api_key
        return result

    def redact(self, text: str) -> str:
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text


def resolve_credentials(variables: "Variables", *, api_key_env: str | None = None, fallback: str | None = None) -> BenchmarkCredentials:
    if api_key_env:
        value = variables.get(api_key_env)
        if not value:
            from scitrera_app_framework import add_env_file_source

            add_env_file_source(".env", variables)
            value = variables.get(api_key_env)
        if not isinstance(value, str) or not value:
            raise ValueError("Benchmark credential environment variable %r is missing or empty" % api_key_env)
    else:
        value = fallback
    if value is not None and not isinstance(value, str):
        raise TypeError("Benchmark API key must be a string")
    return BenchmarkCredentials(value)
