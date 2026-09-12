"""A feature contribution independent of the application package."""

from sparkrun.core.features import FeatureFlag

__version__ = "0.1.0"
SPARKRUN_PLUGIN_API_VERSION = 1
FEATURE_DEFINITIONS = (FeatureFlag("test.installed_plugin", "Installed-plugin fixture", default=True),)


def register(v):
    from sparkrun.application import get_application_identity

    if v is not None:
        v.set("test.plugin.application_identity", get_application_identity())
