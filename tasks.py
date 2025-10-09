from pathlib import Path
from invoke.tasks import task
from tests.services import create_metaflow_sandboxes, write_config, terminate_sandboxes
from textwrap import dedent


@task
def start_metaflow(
    ctx,
    timeout: int = 60 * 60,
    app_name: str = "metaflow-test",
    include_ui: bool = True,
):
    print("Starting metaflow services on Modal")
    metaflow_service = create_metaflow_sandboxes(
        app_name, timeout=timeout, include_ui=include_ui
    )

    modal_mf_home = Path(".modal_metaflow").absolute()
    source_file = write_config(modal_mf_home, metaflow_service).absolute()

    ui_context = ""
    if metaflow_service.ui:
        ui_context = f"🌎 Metaflow UI: {metaflow_service.ui.url}"

    print(
        dedent(f"""\
    🚀 Metaflow started on Modal!
    {ui_context}
    💻 Configure your local environment by running:

    source {source_file}""")
    )


@task
def stop_metaflow(ctx):
    modal_mf_home = Path(".modal_metaflow").absolute()
    terminate_sandboxes(modal_mf_home)
    print("🗑️ Stopped metaflow sandboxes on Modal")
