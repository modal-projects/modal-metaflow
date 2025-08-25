"""Modal StepDecorator implementation.

The ModalDecorator is responsible for StepDecorator interop, gathering the configuration needed to wrap
individual step functions, and deploying the App containing the @app.function-wrapped versions of those
step functions. It also passes app/function names to `metaflow modal step --modal-app-name foo --modal-func-name bar`.
"""

import json
import os
import subprocess
import sys
import time
import traceback
from typing import Optional

from metaflow.util import get_username
from metaflow.metaflow_config import DATASTORE_LOCAL_DIR
from metaflow.metadata_provider.util import sync_local_metadata_to_datastore
import modal as modal_sdk
from metaflow.decorators import StepDecorator
from metaflow.exception import MetaflowException
from metaflow.metadata_provider.metadata import MetaDatum
from metaflow.metaflow_config import (
    DEFAULT_RUNTIME_LIMIT,
    FEAT_ALWAYS_UPLOAD_CODE_PACKAGE,
)


class ModalDecoratorException(MetaflowException):
    headline = "@modal decorator failed"


class ModalDecorator(StepDecorator):
    """
    Decorator for Metaflow steps to run on Modal.
    """

    name = "modal"
    defaults = {
        "cpu": None,
        "gpu": None,
        "memory": None,
        "ephemeral_disk": None,
        "image": None,
        "retries": None,
        "timeout": None,
        "volumes": dict(),
        "secrets": list(),
        "name": None,
        "mark_reentrant": False,
    }
    # shared state across ModalDecorator instances
    _modal_app: Optional[modal_sdk.App] = None
    _modal_app_id: Optional[str] = None
    _modal_func_name: Optional[str] = None
    _modal_app_deployed = False
    _has_logged_reentrant_warning = False
    package_metadata = None
    package_url = None
    package_sha = None

    def init(self):
        self.env_vars = None
        self.requirements = []
        self.python_version = None
        self.mark_reentrant = self.attributes.pop("mark_reentrant")

        # shared state across ModalDecorator instances
        if self.__class__._modal_app is None:
            timestamp = str(int(time.time()))[-6:]  # unique app name
            app_name = f"metaflow-exec-{timestamp}"
            self.__class__._modal_app = modal_sdk.App(app_name)

    def _parse_timeout(self, decorators, logger):
        """Parse and validate timeout configuration from decorators."""

        # Check if both @modal(..., timeout=N) and @timeout are specified
        timeout_decorator_present = any(
            hasattr(deco, "name") and deco.name == "timeout" for deco in decorators
        )
        modal_timeout_specified = self.attributes["timeout"] is not None

        if modal_timeout_specified and timeout_decorator_present:
            raise ModalDecoratorException(
                "Cannot specify both @modal(timeout=...) and @timeout decorator. "
                "Please use only one timeout configuration method."
            )

        # Fallback to globally configured timeout
        default_timeout = DEFAULT_RUNTIME_LIMIT

        # Determine timeout_val based on priority
        if modal_timeout_specified:
            timeout_val = self.attributes["timeout"]
        elif timeout_decorator_present:
            # Find the timeout decorator and get its value
            for deco in decorators:
                if hasattr(deco, "name") and deco.name == "timeout":
                    timeout_val = deco.secs
                    break
            raise ModalDecoratorException(
                "Internal error: timeout decorator was detected but not found"
            )
        else:
            timeout_val = default_timeout

        timeout_val = int(timeout_val)  # type: ignore

        if timeout_val > 24 * 60 * 60:  # 1 day
            if not self.mark_reentrant:
                if not modal_timeout_specified and not timeout_decorator_present:
                    # Value derived from DEFAULT_RUNTIME_LIMIT - truncate and warn
                    timeout_val = 24 * 60 * 60
                    if not self.__class__._has_logged_reentrant_warning:
                        logger(
                            "Modal steps are limited to 24 hours by default. "
                            "Timeout has been truncated to 24 hours. "
                            "To run longer steps, specify mark_reentrant=True in @modal decorator.",
                            system_msg=True,
                            bad=True,
                        )
                        self.__class__._has_logged_reentrant_warning = True
                else:
                    # Value from @modal or @timeout - raise error
                    raise ModalDecoratorException(
                        "Modal steps with timeout > 24 hours must be reentrant. "
                        "Please specify mark_reentrant=True in @modal decorator "
                        "to run with a timeout greater than 24 hours."
                    )
            else:
                # mark_reentrant=True - allow any timeout
                # TODO: Add Modal Retries support for reentrant functions
                pass

        return timeout_val

    def step_init(
        self, flow, graph, step_name, decorators, environment, flow_datastore, logger
    ):
        if flow_datastore.TYPE != "s3":
            raise ModalDecoratorException(
                "The *@modal* decorator requires --datastore=s3 at the moment."
            )
        self.environment = environment
        self.flow_datastore = flow_datastore

        # Parse and validate timeout configuration
        timeout_val = self._parse_timeout(decorators, logger)
        self.attributes["timeout"] = timeout_val

        for deco in decorators:
            if hasattr(deco, "name"):
                # Unrecoverable
                if deco.name == "kubernetes":
                    raise ModalDecoratorException(
                        "@kubernetes and @modal are mutually exclusive."
                    )

                if deco.name == "batch":
                    raise ModalDecoratorException(
                        "@batch and @modal are mutually exclusive."
                    )

                # Adaptable
                if deco.name == "environment":
                    if hasattr(deco, "vars") and len(deco.vars) > 0:
                        self.env_vars = deco.vars

                elif deco.name == "secrets":
                    # TODO
                    pass

                elif deco.name == "pypi":
                    if hasattr(deco, "packages") and len(deco.packages) > 0:
                        self.requirements.extend(
                            [f"{pkg}=={ver}" for pkg, ver in deco.packages.items()]
                        )
                    if hasattr(deco, "python") and deco.python is not None:
                        version_parts = deco.python.split(".")
                        if len(version_parts) >= 2:
                            try:
                                major = int(version_parts[0])
                                minor = int(version_parts[1])
                                self.python_version = f"{major}.{minor}"
                            except ValueError:
                                # If parsing fails, use the original string as-is
                                self.python_version = deco.python
                        else:
                            # If not in expected format, use as-is
                            self.python_version = deco.python

                elif deco.name == "conda":
                    # TODO
                    pass

                elif deco.name == "resources":
                    if hasattr(deco, "cpu") and deco.cpu is not None:
                        self.attributes["cpu"] = max(
                            self.attributes["cpu"] or -1, float(deco.cpu)
                        )

                    if hasattr(deco, "gpu") and deco.gpu is not None:
                        gpu_cfg = self.attributes.pop("gpu", None)
                        self.attributes["gpu"] = _handle_gpu_cfg(
                            gpu_cfg, deco.gpu, logger
                        )

                    if hasattr(deco, "disk") and deco.disk is not None:
                        self.attributes["ephemeral_disk"] = max(
                            self.attributes["ephemeral_disk"] or -1, deco.disk
                        )

                    if hasattr(deco, "memory") and deco.memory is not None:
                        self.attributes["memory"] = max(
                            self.attributes["memory"] or -1, deco.memory
                        )

                    if (
                        hasattr(deco, "shared_memory")
                        and deco.shared_memory is not None
                    ):
                        logger(
                            "Modal backend does not use shared_memory, ignoring.",
                            system_msg=True,
                        )

    def runtime_init(self, flow, graph, package, run_id):
        """Deploy Modal app with all @modal step functions"""
        self.flow_name = flow.__class__.__name__
        self.graph = graph
        self.package = package
        self.run_id = run_id

        if not self.__class__._modal_app_deployed:
            # Iterate through all @modal decorated steps
            for step in flow:
                for deco in step.decorators:
                    if isinstance(deco, ModalDecorator):
                        # Create unique function names: f"{flow_name}__{step_name}"
                        func_name = f"{self.flow_name}__{step.name}"

                        # Filter out None values and name from attributes for Modal function
                        modal_attributes = {
                            k: v
                            for k, v in deco.attributes.items()
                            if v is not None and k != "name"
                        }

                        # Create the Modal function wrapper
                        modal_wrapper = deco._create_modal_func(step)
                        modal_wrapper.__name__ = func_name  # Set function name

                        # Create and register Modal function with serialized=True
                        _ = self.__class__._modal_app.function(
                            serialized=True, name=func_name, **modal_attributes
                        )(modal_wrapper)

                        # Store function name in _modal_func_name for CLI lookup
                        deco._modal_func_name = func_name

            # Deploy single shared Modal app
            with modal_sdk.enable_output():
                self.__class__._modal_app.deploy()
                assert self.__class__._modal_app.app_id is not None
                self.__class__._modal_app_id = self.__class__._modal_app.app_id
                self.__class__._modal_app_deployed = True

    def _create_modal_func(self, step):
        """Create Modal function that wraps subprocess execution"""
        if self.env_vars is not None:
            addl_env_vars = self.env_vars
        else:
            addl_env_vars = None

        username = get_username()
        if username is None:
            raise RuntimeError()

        def modal_wrapper(step_cli, env):
            """Simple subprocess wrapper that takes step_cli and env"""
            try:
                # Add envvars specified in @environment
                if addl_env_vars is not None:
                    env.update(addl_env_vars)

                # Add modal function environ to subprocess env
                env.update(os.environ)

                # Metaflow CLI step subcommand requirement
                env["USERNAME"] = username

                # Execute the Metaflow step command in subprocess
                result = subprocess.run(
                    step_cli,
                    shell=True,
                    env=env,
                    capture_output=True,
                )

                # Print stdout/stderr for debugging
                if result.stdout:
                    print("STDOUT\n", result.stdout.decode())
                if result.stderr:
                    print("STDERR\n", result.stderr.decode(), file=sys.stderr)

                # Return exit code for proper Metaflow retry handling
                return result.returncode, result.stdout, result.stderr

            except Exception:
                err_str = f"Modal function execution failed:\n{traceback.format_exc()}"
                print(err_str, file=sys.stderr)
                return 1, "", err_str

        return modal_wrapper

    def runtime_task_created(
        self,
        task_datastore,
        task_id,
        split_index,
        input_paths,
        is_cloned,
        ubf_context,
    ):
        if not is_cloned:
            self._save_package_once(self.flow_datastore, self.package)

    @classmethod
    def _save_package_once(cls, flow_datastore, package):
        if cls.package_url is None:
            if not FEAT_ALWAYS_UPLOAD_CODE_PACKAGE:
                cls.package_url, cls.package_sha = flow_datastore.save_data(
                    [package.blob], len_hint=1
                )[0]
                cls.package_metadata = package.package_metadata
            else:
                # Blocks until the package is uploaded
                cls.package_url = package.package_url()
                cls.package_sha = package.package_sha()
                cls.package_metadata = package.package_metadata

    def runtime_step_cli(
        self, cli_args, retry_count, max_user_code_retries, ubf_context
    ):
        """Redirect to 'metaflow modal step' with app/func identifiers"""
        if retry_count <= max_user_code_retries:
            # Change cli_args.commands to ["modal", "step"]
            cli_args.commands = ["modal", "step"]
            cli_args.command_args.append(self.package_metadata)
            cli_args.command_args.append(self.package_sha)
            cli_args.command_args.append(self.package_url)

            # Add modal_app_name and modal_func_name to command_options
            cli_args.command_options.update(
                {
                    "modal_app_name": self.__class__._modal_app.name,
                    "modal_func_name": self._modal_func_name,
                }
            )
            # Ensure python executable is preserved
            cli_args.entrypoint[0] = sys.executable

    def runtime_finished(self, exception):
        # Clean up deployed app
        if self.__class__._modal_app_id:
            try:
                subprocess.run(
                    ["modal", "app", "stop", self.__class__._modal_app_id],
                    capture_output=True,
                    text=True,
                )
            except Exception:
                # Ignore errors when stopping the app
                pass

    def task_pre_step(
        self,
        step_name,
        task_datastore,
        metadata,
        run_id,
        task_id,
        flow,
        graph,
        retry_count,
        max_user_code_retries,
        ubf_context,
        inputs,
    ):
        self.metadata = metadata
        self.task_datastore = task_datastore
        meta_entries = [
            MetaDatum(
                field="modal_execution",
                value="true",
                type="modal_execution",
                tags=["modal"],
            ),
            MetaDatum(
                field="modal_config",
                value=json.dumps(_serialize_modal_attributes(self.attributes)),
                type="modal_config",
                tags=["modal"],
            ),
        ]
        metadata.register_metadata(run_id, step_name, task_id, meta_entries)

    def task_decorate(
        self, step_func, flow, graph, retry_count, max_user_code_retries, ubf_context
    ):
        return step_func

    def task_post_step(
        self, step_name, flow, graph, retry_count, max_user_code_retries
    ):
        pass

    def task_exception(
        self, exception, step_name, flow, graph, retry_count, max_user_code_retries
    ):
        return

    def task_finished(
        self, step_name, flow, graph, is_task_ok, retry_count, max_user_code_retries
    ):
        if hasattr(self, "metadata") and self.metadata.TYPE == "local":
            # Note that the datastore is *always* Amazon S3 (see
            # runtime_task_created function).
            sync_local_metadata_to_datastore(DATASTORE_LOCAL_DIR, self.task_datastore)


def _safe_primitive(val):
    try:
        json.dumps(val)
        return val
    except Exception:
        return str(val)


def _serialize_modal_attributes(attrs: dict) -> dict:
    """Return a JSON-serializable copy of the attributes.

    - modal.Secret -> {"kind": "modal.Secret", "name": <name>, "info": <stringified info>}
    - modal.Image  -> {"kind": "modal.Image",  "repr": <string repr>}
    - Recurses into lists/tuples/sets and dicts
    - Falls back to string for anything not JSON-serializable
    """

    def _serialize(v):
        # Primitives stay as-is
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        # Collections
        if isinstance(v, (list, tuple, set)):
            return [_serialize(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _serialize(vv) for k, vv in v.items()}

        if isinstance(v, modal_sdk.Secret):
            name = getattr(v, "name", None)
            info = getattr(v, "info", None)
            return {
                "kind": "modal.Secret",
                "name": _safe_primitive(name),
                "info": _safe_primitive(info),
            }
        if isinstance(v, modal_sdk.Image):
            # Image has no stable public fields that are obviously JSON-able; use repr
            return {
                "kind": "modal.Image",
                "repr": str(v),
            }

        # Unknown / other objects -> string repr
        return str(v)

    return {k: _serialize(v) for k, v in attrs.items()}


def _handle_gpu_cfg(
    modal_gpu: str | list[str] | None, resources_gpu: int, logger
) -> str | list[str] | None:
    if modal_gpu is None and resources_gpu == 0:
        return

    if resources_gpu > 0 and modal_gpu is None:
        logger.warn("No GPU type specified in @modal step, defaulting to H100.")
        return f"H100:{resources_gpu}"

    if resources_gpu > 8 and modal_gpu is None:
        raise ModalDecoratorException(
            f"Modal supports between 0 and 8 GPUs per node, but @resources(gpu={resources_gpu}) exceeds that."
        )

    if isinstance(modal_gpu, str):
        if resources_gpu == 0 and modal_gpu is not None:
            # No gpu requested by resources; use modal_gpu
            return modal_gpu
        if resources_gpu > 0 and modal_gpu is not None:
            gpu_split = modal_gpu.split(":", 1)
            if len(gpu_split) > 1:
                gpu_type, num_gpus = gpu_split
                if num_gpus != resources_gpu:
                    logger.warn(
                        "Num GPUs requested in @modal and @resources conflict; falling back to @modal gpu config."
                    )
                return modal_gpu
            else:
                # combine GPU type and resources gpu amount
                return f"{gpu_split}:{resources_gpu}"

    elif isinstance(modal_gpu, list):
        logger.warn(
            "Complex GPU type requested in @modal step, ignoring @resources gpu."
        )
        return modal_gpu
