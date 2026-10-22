# Test-only. When this directory is on PYTHONPATH (set by test_r2_repeat_coverage.py for the actor
# subprocesses it starts), register the test-only implementation at interpreter start.
try:
    import r2_test_impls

    r2_test_impls.register()
except ImportError:
    pass
