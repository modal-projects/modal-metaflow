import os
import pytest
from pathlib import Path
from tests.scaffold import (
    create_metaflow_modal_resources,
    stop_argo_kubernetes,
    write_config,
    start_argo_kubernetes,
)


@pytest.fixture(scope="session")
def flows_path() -> Path:
    return (Path(__file__).parent / "flows").resolve()


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

    parser.addoption(
        "--sandbox-timeout",
        action="store",
        default=None,
        type=int,
        help="Timeout for modal sandboxes",
    )

    parser.addoption(
        "--app-name",
        action="store",
        default="metaflow-test",
        help="App name to run tests",
    )


@pytest.fixture(scope="session")
def keep_alive(request):
    """A fixture that returns the value of the --keep-alive command-line option."""
    return request.config.getoption("--keep-alive")


@pytest.fixture(scope="session")
def sandbox_timeout(request):
    """A fixture that returns the value of the --keep-alive command-line option."""
    return request.config.getoption("--sandbox-timeout")


@pytest.fixture(scope="session")
def app_name(request):
    """A fixture that returns the value of the --keep-alive command-line option."""
    return request.config.getoption("--app-name")


@pytest.fixture(scope="session")
def mf_service(app_name, metaflow_home_path, keep_alive, sandbox_timeout):
    mf_service = create_metaflow_modal_resources(
        app_name, include_ui=False, timeout=sandbox_timeout
    )
    write_config(metaflow_home_path, mf_service)

    try:
        modal_token_id = os.environ["MODAL_METAFLOW_TOKEN_ID"]
        modal_token_secret = os.environ["MODAL_METAFLOW_TOKEN_SECRET"]
    except KeyError:
        raise RuntimeError(
            "MODAL_METAFLOW_TOKEN_ID and MODAL_METAFLOW_TOKEN_SECRET must be set locally. "
            "They are used by argo to authenticate with Modal."
        )

    start_argo_kubernetes(
        metaflow_home_path,
        modal_token_id=modal_token_id,
        modal_token_secret=modal_token_secret,
    )

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

    stop_argo_kubernetes()


@pytest.fixture(autouse=True)
def configure_env(mf_service, monkeypatch, metaflow_home_path):
    """Configure environment variables for metaflow."""
    monkeypatch.setenv("METAFLOW_HOME", str(metaflow_home_path))
    monkeypatch.setenv("METAFLOW_PROFILE", "modal")

    aws_path = metaflow_home_path / "aws_config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_path))
