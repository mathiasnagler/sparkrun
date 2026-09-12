"""Presentation-independent progress contract for benchmark schedulers."""

from typing import Any, Protocol


class BenchmarkProgress(Protocol):
    """Receives task events; implementations may render, forward, or ignore them."""

    def start_task(self, idx: int, label: str) -> None: ...

    def end_task(self, idx: int, success: bool, duration_s: float | None = None) -> None: ...

    def update_results_table(self, consolidated: dict[str, Any]) -> None: ...

    def log(self, message: str) -> None: ...
