import pytest
from pathlib import Path
from tests.scaffold import create_metaflow_sandboxes, write_config
from os import getenv


@pytest.fixture(scope="session")
def flows_path() -> Path:
    return (Path(__file__).parent / "flows").resolve()


@pytest.fixture(scope="session")
def app_name() -> str:
    return "metaflow-test"


@pytest.fixture(scope="session")
def metaflow_home_path(tmp_path_factory) -> Path:
    mf_home = tmp_path_factory.mktemp(".modal_metaflow")
    return mf_home


def pytest_addoption(parser):
    parser.addoption(
        "--keep-alive",
        action="store_true",
        default=False,
        help="Keep modal sandboxes alive",
    )


@pytest.fixture(scope="session")
def keep_alive(request):
    """A fixture that returns the value of the --keep-alive command-line option."""
    return request.config.getoption("--keep-alive")


@pytest.fixture(scope="session")
def mf_service(app_name, metaflow_home_path, keep_alive):
    mf_service = create_metaflow_sandboxes(app_name, include_ui=False)
    write_config(metaflow_home_path, mf_service)

    yield mf_service

    if keep_alive:
        return

    sandboxes = [
        mf_service.minio.sandbox,
        mf_service.psql.sandbox,
        mf_service.metadata_service.sandbox,
    ]

    for sandbox in sandboxes:
        sandbox.terminate()


@pytest.fixture(autouse=True)
def configure_env(mf_service, monkeypatch, metaflow_home_path):
    monkeypatch.setenv("METAFLOW_HOME", str(metaflow_home_path))
    monkeypatch.setenv("METAFLOW_PROFILE", "modal")

    aws_path = metaflow_home_path / "aws_config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_path))
