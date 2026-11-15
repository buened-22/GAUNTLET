# Test-only. When this directory is on PYTHONPATH (set by test_watchdog.py and test_step_timeout_pg.py for the
# actor subprocesses they start), register the test-only implementations at interpreter start.
try:
    import hang_test_impls

    hang_test_impls.register()
    hang_test_impls.register_slow()
except ImportError:
    pass
