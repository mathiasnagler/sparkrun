"""Concrete runtime for testing inherited defaults without bypassing the ABC."""

from sparkrun.runtimes.base import RuntimePlugin


class StubRuntime(RuntimePlugin):
    runtime_name = "stub"

    def generate_command(self, recipe, overrides, is_cluster, num_nodes=1, head_ip=None, skip_keys=frozenset()):
        return "fixture-serve"
