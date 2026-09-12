"""Exercise independent application policy without real hardware assumptions."""

from sparkrun.core.application_profile import ApplicationProfile

PROFILE_TEST_APP = ApplicationProfile(
    id="profile-test-app",
    display_name="Profile test application",
    command="profile-test-app",
    package="profile-test-app",
    integrations=("profile-test-plugin",),
    required_integrations=("profile-test-plugin",),
    feature_defaults={"cli.tune": False, "integration.arena": False, "integration.k8s": False},
    registries=(),
    bootstrap_registry_urls=(),
    update_sources={},
    telemetry_enabled=False,
    hardware_fallback="require-metadata",
    profile_ref="profile_test_app.profile:PROFILE_TEST_APP",
)
