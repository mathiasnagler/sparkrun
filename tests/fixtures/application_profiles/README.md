# Application profile test fixtures

These two minimal packages exercise installed application composition, entry-point
selection, feature policy and namespace isolation. They are private test data,
not supported applications or hardware integrations. Build them only through
`SPARKRUN_TEST_WHEELS=1 .venv/bin/python -m pytest tests/test_application_profile_wheels.py`.
