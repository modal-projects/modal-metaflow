import pytest
from pathlib import Path


@pytest.fixture(scope="session")
def flows_path() -> Path:
    return (Path(__file__).parent / "flows").resolve()
