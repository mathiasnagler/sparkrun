# Application profile test fixtures

These two minimal packages exercise installed application composition, entry-point
selection, feature policy and namespace isolation. They are private test data,
not supported applications or hardware integrations. Build them only through
`SPARKRUN_TEST_WHEELS=1 .venv/bin/python -m pytest tests/test_application_profile_wheels.py`.

The fixtures declare compatibility with the 0.4 core API series rather than
repeat its patch version. The wheel test installs the freshly built core first,
then these fixtures, and checks both the core version and unchanged core files.
Their own package versions are independent test-fixture identities.
