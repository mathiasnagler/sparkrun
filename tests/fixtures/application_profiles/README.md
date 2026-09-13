# Application profile test fixtures

These two minimal packages exercise installed application composition, entry-point
selection, feature policy and namespace isolation. They are private test data,
not supported applications or hardware integrations. Build them only through
`SPARKRUN_TEST_WHEELS=1 .venv/bin/python -m pytest tests/test_application_profile_wheels.py`.

The fixtures declare compatibility with the 0.4 core API series rather than
repeat its patch version. The wheel test installs the freshly built core first,
then these fixtures, and checks both the core version and unchanged core files.
Their own package versions are independent test-fixture identities.


The plugin also declares an offline typed gateway. Installed-wheel tests select
it under both built-in Sparkrun and the alternate application, exercise model
results/errors and optional console/token capabilities through the public API
and CLI, and verify that API use does not import Click. The test process supplies
a local state record; no real gateway, host, or inference workload is started.
