"""Host compatibility for pinned providers; no public registration policy."""


def adapt_gateway_class(engine_class: type) -> type:
    """Adapt only the bundled legacy class, without importing disabled plugins."""
    if (engine_class.__module__, engine_class.__name__) == ("sparkrun.plugins.sparkroute.engine", "SparkrouteEngine"):
        from ._sparkroute_adapter import SparkrouteGateway, SparkrouteEngine

        if engine_class is SparkrouteEngine:
            return SparkrouteGateway
    return engine_class
