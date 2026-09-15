"""Explicit setup selections owned by hardware platforms, without probe imports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SetupPlan:
    """Steps reviewed by a platform for one executor.

    Registration makes steps available; only membership in this plan permits
    selection. Dependencies must also be listed explicitly. Hardware discovery
    is mandatory and is not part of this optional-step list.
    """

    executor: str
    steps: tuple[str, ...]
    operating_systems: tuple[str, ...] = ("Linux",)

    def __post_init__(self):
        if not isinstance(self.executor, str) or not self.executor:
            raise ValueError("Setup plans require an executor name")
        if isinstance(self.steps, str):
            raise TypeError("Setup plan steps must be a sequence of step IDs")
        object.__setattr__(self, "steps", tuple(self.steps))
        if any(not isinstance(key, str) or not key or key == "hardware" for key in self.steps):
            raise ValueError("Setup plans require optional step IDs; hardware discovery is mandatory")
        if isinstance(self.operating_systems, str) or not self.operating_systems:
            raise ValueError("Setup plans require explicit operating systems")
        object.__setattr__(self, "operating_systems", tuple(self.operating_systems))
        if any(not isinstance(name, str) or not name for name in self.operating_systems):
            raise ValueError("Invalid setup operating system")
        if len(set(self.steps)) != len(self.steps):
            raise ValueError("Duplicate setup plan step")


def validate_platform_setup_plans(platform, steps=None) -> None:
    """Validate declarations; validate references once a plugin finished loading."""
    executors = set()
    if not isinstance(platform.setup_plans, tuple):
        raise TypeError("Platform setup_plans must be a tuple of SetupPlan declarations")
    for plan in platform.setup_plans:
        if not isinstance(plan, SetupPlan):
            raise TypeError("Platform setup_plans must contain SetupPlan declarations")
        if plan.executor in executors:
            raise ValueError("Duplicate setup executor %s on platform %s" % (plan.executor, platform.platform_name))
        executors.add(plan.executor)
        if steps is None:
            continue
        for key in plan.steps:
            if key not in steps:
                raise ValueError("Unknown setup step %s in platform %s plan" % (key, platform.platform_name))
            missing = set(steps[key].requires) - set(plan.steps) - {"hardware"}
            if missing:
                raise ValueError(
                    "Setup plan %s/%s omits prerequisites for %s: %s"
                    % (platform.platform_name, plan.executor, key, ", ".join(sorted(missing)))
                )
