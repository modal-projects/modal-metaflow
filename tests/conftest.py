import os
from pathlib import Path

import pytest

from tests.scaffold import (
    DEFAULT_TIMEOUT,
    create_metaflow_modal_resources,
    start_argo_kubernetes,
    stop_argo_kubernetes,
    write_config,
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
        "--teardown",
        action="store_true",
        default=False,
        help="Keep modal sandboxes alive",
    )

    parser.addoption(
        "--sandbox-timeout",
        action="store",
        default=DEFAULT_TIMEOUT,
        type=int,
        help="Timeout for modal sandboxes",
    )

    parser.addoption(
        "--app-name",
        action="store",
        default="metaflow-test",
        help="App name to run tests",
    )

    parser.addoption(
        "--no-argo",
        action="store_true",
        default=False,
        help="Run argo tests",
    )


@pytest.fixture(scope="session")
def teardown(request):
    """A fixture that returns the value of the --keep-alive command-line option."""
    return request.config.getoption("--teardown")


@pytest.fixture(scope="session")
def sandbox_timeout(request):
    """A fixture that returns the value of the --sandbox-timeout command-line option."""
    return request.config.getoption("--sandbox-timeout")


@pytest.fixture(scope="session")
def app_name(request):
    """A fixture that returns the value of the --app-name command-line option."""
    return request.config.getoption("--app-name")


@pytest.fixture(scope="session")
def no_argo(request):
    """A fixture that returns the value of the --no-argo command-line option."""
    return request.config.getoption("--no-argo")


def pytest_collection_modifyitems(config, items):
    no_argo = config.getoption("--no-argo")
    skip_argo = pytest.mark.skip(reason="test is enabled with -no-argo")
    if no_argo:
        for item in items:
            if "argo" in item.name:
                item.add_marker(skip_argo)


@pytest.fixture(scope="session")
def mf_service(app_name, metaflow_home_path, teardown, sandbox_timeout, no_argo):
    mf_service = create_metaflow_modal_resources(
        app_name, include_ui=False, timeout=sandbox_timeout
    )
    write_config(metaflow_home_path, mf_service)

    if not no_argo:
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

    if not teardown:
        return

    sandboxes = [
        mf_service.minio.sandbox,
        mf_service.psql.sandbox,
        mf_service.metadata_service.sandbox,
    ]

    for sandbox in sandboxes:
        sandbox.terminate()

    if not no_argo:
        stop_argo_kubernetes()


@pytest.fixture(autouse=True)
def configure_env(mf_service, monkeypatch, metaflow_home_path):
    """Configure environment variables for metaflow."""
    monkeypatch.setenv("METAFLOW_HOME", str(metaflow_home_path))
    monkeypatch.setenv("METAFLOW_PROFILE", "modal")

    aws_path = metaflow_home_path / "aws_config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(aws_path))

    # the in-process client read its config at import, so point it at this session's service
    from metaflow import metadata

    metadata(f"service@{mf_service.metadata_service.url}")


@pytest.fixture()
def in_ci():
    # Accessing run is not working on github actions.
    return "GITHUB_RUN_ID" in os.environ


collect_ignore_glob = ["flows/integration/test_*.py"]
