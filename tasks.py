import os
from pathlib import Path
from invoke import task
from tests.scaffold import (
    create_metaflow_modal_resources,
    write_config,
    terminate_sandboxes,
    stop_argo_kubernetes,
    start_argo_kubernetes,
    DEFAULT_TIMEOUT,
    DEFAULT_APP_NAME,
    DEFAULT_INCLUDE_UI,
)
from textwrap import dedent

project_root = Path(__file__).parent
modal_mf_home = (project_root / ".modal_metaflow").absolute()


@task
def start_metaflow(
    ctx,
    timeout: int = DEFAULT_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    include_ui: bool = DEFAULT_INCLUDE_UI,
) -> str:
    print("Starting metaflow services on Modal")
    metaflow_service = create_metaflow_modal_resources(
        app_name, timeout=timeout, include_ui=include_ui
    )
    source_file = write_config(modal_mf_home, metaflow_service).absolute()

    ui_context = ""
    if metaflow_service.ui:
        ui_context = f"🌎 Metaflow UI: {metaflow_service.ui.url}"

    msg = dedent(f"""\
    🚀 Metaflow started on Modal!
    {ui_context}
    💻 Configure your local environment by running:

    source {source_file}""")

    print(msg)
    return msg


@task
def stop_metaflow(ctx):
    modal_mf_home = Path(".modal_metaflow").absolute()
    terminate_sandboxes(modal_mf_home)
    print("🗑️ Stopped metaflow sandboxes on Modal")


@task
def start_argo(ctx):
    print("Starting argo workflows!")
    try:
        modal_token_id = os.environ["MODAL_METAFLOW_TOKEN_ID"]
        modal_token_secret = os.environ["MODAL_METAFLOW_TOKEN_SECRET"]
    except KeyError:
        raise RuntimeError(
            "MODAL_METAFLOW_TOKEN_ID and MODAL_METAFLOW_TOKEN_SECRET must be set locally. "
            "They are used by argo to authenticate with Modal."
        )

    start_argo_kubernetes(
        modal_mf_home,
        modal_token_id=modal_token_id,
        modal_token_secret=modal_token_secret,
    )

    print("🚀 Argo workflow started!")
    print(
        "🔌 To see the Argo UI run: `kubectl -n argo port-forward service/argo-server 2746:2746`"
    )


@task
def stop_argo(ctx):
    print("Stopping argo workflows")
    stop_argo_kubernetes()


@task
def develop(
    ctx,
    timeout: int = DEFAULT_TIMEOUT,
    app_name: str = DEFAULT_APP_NAME,
    include_ui: bool = DEFAULT_INCLUDE_UI,
):
    msg = start_metaflow(ctx, timeout, app_name, include_ui)
    start_argo(ctx)
    print()
    print(msg)


@task
def teardown(ctx):
    stop_metaflow(ctx)
    stop_argo(ctx)
