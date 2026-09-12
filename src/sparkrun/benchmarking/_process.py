"""Private benchmark process lifetime and redacted output handling (POSIX hosts)."""

from __future__ import annotations

import codecs
import logging
import os
import re
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Sequence

from sparkrun.benchmarking._credentials import BenchmarkCredentials

logger = logging.getLogger(__name__)


class _Output:
    """Decode and redact across reads before emitting complete text lines."""

    def __init__(self, sink: Callable[[str], None], credentials: BenchmarkCredentials):
        self.sink = sink
        self.key = credentials.api_key
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""
        self.line = ""

    def feed(self, data: bytes, *, final: bool = False) -> None:
        text = self.pending + self.decoder.decode(data, final=final)
        self.pending = ""
        if self.key:
            parts = text.split(self.key)
            ready = "[REDACTED]".join(parts[:-1]) + ("[REDACTED]" if len(parts) > 1 else "")
            text = parts[-1]
            if not final:
                for size in range(min(len(self.key) - 1, len(text)), 0, -1):
                    if text.endswith(self.key[:size]):
                        text, self.pending = text[:-size], text[-size:]
                        break
            text = ready + text
        # Only CR/LF frame transport lines. Unicode separators are payload.
        # Hold a trailing CR until the next read so split CRLF stays together.
        text = self.line + text
        start = 0
        for match in re.finditer(r"\r\n|\n|\r(?!$)", text):
            self.sink(text[start : match.end()])
            start = match.end()
        self.line = text[start:]
        if final and self.line:
            self.sink(self.line)
            self.line = ""


def run_benchmark_process(
    command: Sequence[str],
    *,
    timeout: float | None,
    credentials: BenchmarkCredentials,
    stdout: Callable[[str], None],
    stderr: Callable[[str], None] | None = None,
) -> int:
    """Run one owned process group with a deadline including pipe draining.

    A missing stderr sink merges stderr into stdout. Callbacks run synchronously;
    their errors/interrupts propagate after cleanup. No reader threads or raw
    credential-bearing output survive the operation. Escaped process groups are
    not adopted; closing our pipes never waits for their EOF after timeout.
    """
    deadline = time.monotonic() + timeout if timeout is not None else None
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr is not None else subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        start_new_session=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            for stream, sink in ((proc.stdout, stdout), (proc.stderr, stderr)):
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, _Output(sink, credentials))
            while proc.poll() is None or selector.get_map():
                remaining = deadline - time.monotonic() if deadline is not None else None
                if remaining is not None and remaining <= 0:
                    # Avoid attaching secret-bearing command arguments to the exception.
                    raise subprocess.TimeoutExpired("benchmark", timeout)
                for key, _ in selector.select(min(remaining, 0.1) if remaining is not None else 0.1):
                    data = os.read(key.fileobj.fileno(), 65536)
                    key.data.feed(data, final=not data)
                    if not data:
                        selector.unregister(key.fileobj)
            return proc.returncode
    finally:
        # Kill the group even if its leader exited: workers may still be
        # running or holding a pipe. The session belongs only to this call.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            logger.warning("Benchmark process did not exit after termination")
        finally:
            proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
