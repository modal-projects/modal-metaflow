"""Metaflow CLI subcommand for Modal execution.

The `metaflow modal step` subcommand is used to launch individual step method execution on Modal.
It's meant to be invoked by `ModalDecorator.runtime_step_cli`.
"""

import json
import os
import shlex
import subprocess
import sys
import time
import traceback
from typing import Dict, Optional

from metaflow import namespace, util
from metaflow._vendor import click
from metaflow.exception import METAFLOW_EXIT_DISALLOW_RETRY
from metaflow.metadata_provider.util import sync_local_metadata_from_datastore
from metaflow.metaflow_config import (
    AWS_SECRETS_MANAGER_DEFAULT_REGION,
    CARD_S3ROOT,
    DATASTORE_LOCAL_DIR,
    DATASTORE_SYSROOT_S3,
    DATATOOLS_S3ROOT,
    DEFAULT_AWS_CLIENT_PROVIDER,
    DEFAULT_METADATA,
    DEFAULT_SECRETS_BACKEND_TYPE,
    OTEL_ENDPOINT,
    S3_ENDPOINT_URL,
    SERVICE_URL,
)
from metaflow.mflog import (
    BASH_SAVE_LOGS,
    bash_capture_logs,
    export_mflog_env_vars,
)

LOGS_DIR = "$PWD/.logs"
STDOUT_FILE = "mflog_stdout"
STDERR_FILE = "mflog_stderr"
STDOUT_PATH = os.path.join(LOGS_DIR, STDOUT_FILE)
STDERR_PATH = os.path.join(LOGS_DIR, STDERR_FILE)


@click.group()
def cli():
    pass


@cli.group(help="Commands related to Modal.")
def modal():
    pass


@modal.command(
    help="Execute a single task using Modal. This command calls the "
    "top-level step command inside a Modal app with the given options. "
    "Typically you do not call this command directly; it is used internally by "
    "Metaflow."
)
@click.argument("step-name")
@click.argument("code-package-metadata")
@click.argument("code-package-sha")
@click.argument("code-package-url")
@click.option("--executable", help="Executable requirement for Modal.")
@click.option("--run-id", help="Passed to the top-level 'step'.")
@click.option("--task-id", help="Passed to the top-level 'step'.")
@click.option("--input-paths", help="Passed to the top-level 'step'.")
@click.option("--split-index", help="Passed to the top-level 'step'.")
@click.option("--clone-path", help="Passed to the top-level 'step'.")
@click.option("--clone-run-id", help="Passed to the top-level 'step'.")
@click.option(
    "--tag", multiple=True, default=None, help="Passed to the top-level 'step'."
)
@click.option("--namespace", default=None, help="Passed to the top-level 'step'.")
@click.option("--retry-count", default=0, help="Passed to the top-level 'step'.")
@click.option(
    "--max-user-code-retries", default=0, help="Passed to the top-level 'step'."
)
@click.option(
    "--run-time-limit",
    default=5 * 24 * 60 * 60,
    help="Run time limit in seconds for the Modal function. Default is 5 days.",
)
@click.option("--modal-app-name", help="Modal app name for function lookup.")
@click.option("--modal-func-name", help="Modal function name for execution.")
@click.option("--environment", multiple=True, help="Environment variables for Modal.")
@click.option("--modal-role-arn", help="Role ARN for Modal to authenticate with.")
@click.pass_context
def step(
    ctx,
    step_name,
    code_package_metadata,
    code_package_sha,
    code_package_url,
    executable=None,
    run_time_limit=None,
    modal_app_name=None,
    modal_func_name=None,
    environment=None,
    modal_role_arn=None,
    **kwargs,
):
    """Execute a single task using Modal infrastructure."""

    # Sanity check - something's gone wrong if we're running inside a Modal worker
    if os.getenv("METAFLOW_MODAL_WORKER") == "1":
        ctx.fail("This command must not be run inside a Modal worker.")

    # Validate required arguments
    if not modal_app_name or not modal_func_name:
        ctx.fail("Missing Modal app or function name")

    if not code_package_url or not code_package_sha:
        ctx.fail(
            "Missing code package URL or SHA - ensure Modal app was deployed properly"
        )

    def echo(msg, stream="stderr", modal_id=None, **kwargs):
        msg = util.to_unicode(msg)
        if modal_id:
            msg = "[%s] %s" % (modal_id, msg)
        ctx.obj.echo_always(msg, err=(stream == sys.stderr), **kwargs)

    # Get node information for decorators
    node = ctx.obj.graph[step_name]

    # Get the executable - prefer python
    if executable is None:
        executable = ctx.obj.environment.executable(step_name, "python")

    # Construct the entrypoint command
    entrypoint = "%s -u %s" % (executable, os.path.basename(sys.argv[0]))
    top_args = " ".join(util.dict_to_cli_options(ctx.parent.parent.params))

    # Handle input paths by splitting them into environment variables
    input_paths = kwargs.get("input_paths")
    split_vars = None
    if input_paths:
        max_size = 30 * 1024
        split_vars = {
            "METAFLOW_INPUT_PATHS_%d" % (i // max_size): input_paths[i : i + max_size]
            for i in range(0, len(input_paths), max_size)
        }
        kwargs["input_paths"] = "".join("${%s}" % s for s in split_vars.keys())

    # Construct step arguments
    step_args = " ".join(util.dict_to_cli_options(kwargs))
    step_cli = "{entrypoint} {top_args} step {step} {step_args}".format(
        entrypoint=entrypoint,
        top_args=top_args,
        step=step_name,
        step_args=step_args,
    )

    # Get retry information
    retry_count = kwargs.get("retry_count", 0)
    retry_deco = [deco for deco in node.decorators if deco.name == "retry"]
    minutes_between_retries = None
    if retry_deco:
        minutes_between_retries = int(
            retry_deco[0].attributes.get("minutes_between_retries", 1)
        )
    if retry_count:
        ctx.obj.echo_always(
            "Sleeping %d minutes before the next retry" % minutes_between_retries
        )
        time.sleep(minutes_between_retries * 60)

    # Set up environment variables
    env = {
        "METAFLOW_FLOW_FILENAME": os.environ.get(
            "METAFLOW_FLOW_FILENAME", os.path.basename(sys.argv[0])
        )
    }

    # Add split variables for input paths
    if split_vars:
        env.update(split_vars)

    # Add additional environment variables from CLI
    if environment:
        for env_var in environment:
            if "=" in env_var:
                key, value = env_var.split("=", 1)
                env[key] = value

    env_vars_to_add = {
        "METAFLOW_SERVICE_URL": SERVICE_URL,
        "METAFLOW_CODE_METADATA": code_package_metadata,
        "METAFLOW_CODE_SHA": code_package_sha,
        "METAFLOW_CODE_URL": code_package_url,
        "METAFLOW_DATASTORE_SYSROOT_S3": DATASTORE_SYSROOT_S3,
        "METAFLOW_DATATOOLS_S3ROOT": DATATOOLS_S3ROOT,
        "METAFLOW_DEFAULT_DATASTORE": ctx.obj.flow_datastore.TYPE,
        "METAFLOW_DEFAULT_METADATA": DEFAULT_METADATA,
        "METAFLOW_RUNTIME_ENVIRONMENT": "modal",
        "METAFLOW_MODAL_WORKER": "1",  # Flag to indicate we're inside Modal worker
        "METAFLOW_DEFAULT_SECRETS_BACKEND_TYPE": DEFAULT_SECRETS_BACKEND_TYPE,
        "METAFLOW_CARD_S3ROOT": CARD_S3ROOT,
        "METAFLOW_DEFAULT_AWS_CLIENT_PROVIDER": DEFAULT_AWS_CLIENT_PROVIDER,
        "METAFLOW_AWS_SECRETS_MANAGER_DEFAULT_REGION": AWS_SECRETS_MANAGER_DEFAULT_REGION,
        "METAFLOW_S3_ENDPOINT_URL": S3_ENDPOINT_URL,
        "METAFLOW_OTEL_ENDPOINT": OTEL_ENDPOINT,
        # Pass production token and user info to ensure namespace consistency between Argo pod and Modal worker
        "METAFLOW_PRODUCTION_TOKEN": os.environ.get("METAFLOW_PRODUCTION_TOKEN"),
        "METAFLOW_USER": os.environ.get("METAFLOW_USER"),
    }

    # Ensure USERNAME is present for worker environment
    # This ensures get_username() returns the same value in Modal worker as in Argo pod
    username = os.environ.get("USERNAME")
    if not username:
        try:
            # util may not always expose get_username, so guard it
            username = getattr(util, "get_username", lambda: None)()  # type: ignore
        except Exception:
            username = None
    if username:
        env_vars_to_add["USERNAME"] = str(username)

    # Filter out None values
    env_vars_to_add = {k: v for k, v in env_vars_to_add.items() if v is not None}

    env.update(env_vars_to_add)

    # Set up log locations for streaming
    ds = ctx.obj.flow_datastore.get_task_datastore(
        mode="w",
        run_id=kwargs["run_id"],
        step_name=step_name,
        task_id=kwargs["task_id"],
        attempt=int(retry_count),
    )
    # TODO: proper mflog logging
    # stdout_location = ds.get_log_location(TASK_LOG_SOURCE, "stdout")
    # stderr_location = ds.get_log_location(TASK_LOG_SOURCE, "stderr")

    def _sync_metadata():
        try:
            if ctx.obj.metadata.TYPE == "local":
                sync_local_metadata_from_datastore(
                    DATASTORE_LOCAL_DIR,
                    ctx.obj.flow_datastore.get_task_datastore(
                        kwargs["run_id"],
                        step_name,
                        kwargs["task_id"],
                        attempt=int(retry_count),
                    ),
                )
        except Exception as e:
            # Log the error but don't fail the entire task if metadata sync fails
            echo(f"Warning: Failed to sync metadata: {e}", stream="stderr")

    step_cli = _build_command(
        ctx,
        kwargs["run_id"],
        step_name,
        kwargs["task_id"],
        retry_count,
        code_package_metadata,
        code_package_url,
        ds.TYPE,
        [step_cli],
    )

    try:
        # Execute the Modal task
        exit_code = _execute_modal_task(
            step_cli=shlex.join(step_cli),
            env=env,
            modal_app_name=modal_app_name,
            modal_func_name=modal_func_name,
            modal_role_arn=modal_role_arn,
            run_time_limit=run_time_limit,
            echo=echo,
            **kwargs,
        )

        if exit_code != 0:
            _sync_metadata()
            sys.exit(exit_code)

    except KeyboardInterrupt:
        echo("Modal task interrupted by user")
        _sync_metadata()
        sys.exit(METAFLOW_EXIT_DISALLOW_RETRY)
    except Exception as e:
        echo(f"Modal task failed with error: {e}")
        traceback.print_exc()
        _sync_metadata()
        sys.exit(METAFLOW_EXIT_DISALLOW_RETRY)
    finally:
        _sync_metadata()


def _build_command(
    ctx,
    run_id,
    step_name,
    task_id,
    attempt,
    code_package_metadata,
    code_package_url,
    datastore_type,
    step_cmds,
):
    mflog_expr = export_mflog_env_vars(
        flow_name=ctx.obj.flow.name,
        run_id=run_id,
        step_name=step_name,
        task_id=task_id,
        retry_count=attempt,
        datastore_type=datastore_type,
        stdout_path=STDOUT_PATH,
        stderr_path=STDERR_PATH,
    )
    init_cmds = ctx.obj.environment.get_package_commands(
        code_package_url, datastore_type, code_package_metadata
    )
    init_expr = " && ".join(init_cmds)
    step_expr = bash_capture_logs(
        " && ".join(
            ctx.obj.environment.bootstrap_commands(step_name, datastore_type)
            + step_cmds
        )
    )

    # Construct an entry point that
    # 1) initializes the mflog environment (mflog_expr)
    # 2) bootstraps a metaflow environment (init_expr)
    # 3) executes a task (step_expr)

    # The `true` command is to make sure that the generated command
    # plays well with docker containers which have entrypoint set as
    # eval $@
    cmd_str = "true && mkdir -p %s && %s && %s && %s; " % (
        LOGS_DIR,
        mflog_expr,
        init_expr,
        step_expr,
    )
    # After the task has finished, we save its exit code (fail/success)
    # and persist the final logs. The whole entrypoint should exit
    # with the exit code (c) of the task.
    #
    # Note that if step_expr OOMs, this tail expression is never executed.
    # We lose the last logs in this scenario.
    #
    # TODO: Capture hard exit logs in Kubernetes.
    cmd_str += "c=$?; %s; exit $c" % BASH_SAVE_LOGS
    # For supporting sandboxes, ensure that a custom script is executed before
    # anything else is executed. The script is passed in as an env var.
    cmd_str = (
        '${METAFLOW_INIT_SCRIPT:+eval \\"${METAFLOW_INIT_SCRIPT}\\"} && %s' % cmd_str
    )

    return shlex.split('bash -c "%s"' % cmd_str)


def _execute_modal_task(
    step_cli: str,
    env: Dict[str, str],
    modal_app_name: Optional[str] = None,
    modal_func_name: Optional[str] = None,
    modal_role_arn: Optional[str] = None,
    run_time_limit: Optional[int] = None,
    echo=None,
    **kwargs,
) -> int:
    """Spawn Modal function and poll for completion"""
    try:
        import modal as modal_sdk

        # Arguments are already validated in the step function

        # Environment preference: decorator-resolved (via wrapper) > pod envs > CLI inference
        env_name = os.getenv("METAFLOW_MODAL_ENVIRONMENT") or os.getenv(
            "MODAL_ENVIRONMENT"
        )
        if not env_name:
            try:
                probe = subprocess.run(
                    ["modal", "environment", "list", "--json"],
                    capture_output=True,
                    text=True,
                )
                if probe.returncode == 0 and probe.stdout:
                    data = json.loads(probe.stdout)
                    if isinstance(data, list):
                        active = next(
                            (
                                e
                                for e in data
                                if isinstance(e, dict)
                                and e.get("active") in (True, "True")
                            ),
                            None,
                        )
                        if active:
                            env_name = active.get("name") or env_name
            except Exception:
                pass

        # Use Modal SDK to lookup function directly with short retries to handle propagation
        func = None
        last_err = None
        for i in range(5):
            try:
                func = modal_sdk.Function.from_name(
                    modal_app_name,
                    modal_func_name,
                    environment_name=env_name,
                )
                break
            except Exception as e:
                last_err = e
                time.sleep(1)
        if func is None:
            if echo:
                echo(f"Failed to lookup Modal app/function: {last_err}")
            return 1

        try:
            call = func.spawn(step_cli, env, modal_role_arn)
        except Exception as e:
            if echo:
                echo(f"Failed to spawn Modal function: {e}")
            return 1

        # Wait for function completion using natural timeout
        try:
            result, stdout_result, stderr_result = call.get(timeout=run_time_limit)
            if echo:
                echo(stdout_result, stream="stdout")
                echo(stderr_result, stream="stderr")
                echo(f"Modal function completed with exit code {result}")
            return result
        except TimeoutError:
            if echo:
                echo("Modal function timed out")
            call.cancel()
            return 1
        except KeyboardInterrupt:
            if echo:
                echo("Modal function cancelled by user")
            call.cancel()
            return 1
        except Exception as e:
            if echo:
                echo(f"Modal function failed: {e}")
            return 1

    except Exception as e:
        if echo:
            echo(f"Failed to execute Modal task: {e}")
        return 1
