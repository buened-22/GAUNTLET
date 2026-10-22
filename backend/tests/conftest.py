import pytest

from gauntlet.pg.provision import InfrastructureError, check_server


@pytest.fixture(scope="session")
def pg_available() -> dict:
    try:
        return check_server()
    except InfrastructureError as exc:
        pytest.skip(f"PostgreSQL not reachable (set GAUNTLET_PG_DSN or run `python -m gauntlet pg start`): {exc}")


@pytest.fixture(scope="session")
def run_cache(pg_available):
    """Memoise expensive end-to-end runs shared by several tests."""
    from gauntlet.engine.runner import execute_run
    cache: dict = {}

    def get(scenario: str, impl: str, seed: int = 1) -> dict:
        key = (scenario, impl, seed)
        if key not in cache:
            cache[key] = execute_run(scenario, impl, seed)
        return cache[key]

    return get
