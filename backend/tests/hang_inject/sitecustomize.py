# Test-only. When this directory is on PYTHONPATH (set by test_watchdog.py for the actor subprocesses it
# starts), register the test-only hanging implementation at interpreter start.
try:
    import hang_test_impls

    hang_test_impls.register()
except ImportError:
    pass
