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
                if deco.name == "kubernetes":
                    # If the user explicitly authored @kubernetes on this step, keep prior behavior.
                    is_user_k8s = getattr(deco, "statically_defined", False)
                    if is_user_k8s:
                        pass
                        # raise ModalDecoratorException(
                        #     "@kubernetes and @modal are mutually exclusive on a user-authored decorator. Please remove one of them from your step."
                        # )
                    else:
                        # Argo injected @kubernetes. Minimize pod resources and inject required secrets
                        k8s_attrs = deco.attributes
                        # Minimize resources for the launcher pod
                        k8s_attrs["cpu"] = str(0.1)
                        k8s_attrs["memory"] = str(128)
                        k8s_attrs["disk"] = str(256)
                        k8s_attrs["gpu"] = 0
                        k8s_attrs["qos"] = "Burstable"
                        k8s_attrs["use_tmpfs"] = False
                        k8s_attrs["tmpfs_size"] = None
                        k8s_attrs["shared_memory"] = None
                        k8s_attrs["image"] = "ghcr.io/thomasjpfan/modal-client:0.0.3"

                        # Per-step Modal secret injection.
                        # Name can be customized via env; defaults chosen for local testing.
                        modal_secret_name = os.environ.get(
                            "METAFLOW_MODAL_ARGO_K8S_SECRET_MODAL", "modal-argo-creds"
                        )
                        secrets_to_add = (
                            [modal_secret_name]
                            if modal_secret_name and isinstance(modal_secret_name, str)
                            else []
                        )
                        if secrets_to_add:
                            existing = k8s_attrs.get("secrets")
                            if not existing:
                                k8s_attrs["secrets"] = secrets_to_add
                            elif isinstance(existing, str):
                                k8s_attrs["secrets"] = [existing] + secrets_to_add
                            elif isinstance(existing, list):
                                # Avoid duplicates
                                existing_set = set(existing)
                                k8s_attrs["secrets"] = list(
                                    existing_set.union(secrets_to_add)
                                )
                            else:
                                # Fallback: overwrite with our list
                                k8s_attrs["secrets"] = secrets_to_add

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

        if not self.__class__._modal_app_deployed:
            # Iterate through all @modal decorated steps
            flow_name = flow.name
            for step in flow:
                for deco in step.decorators:
                    if isinstance(deco, ModalDecorator):
                        # Create unique function names: f"{flow_name}__{step_name}"
                        func_name = f"{flow_name}__{step.name}"

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
                            serialized=True,
                            name=func_name,
                            **modal_attributes,
                        )(modal_wrapper)

                        # Store function name in _modal_func_name for CLI lookup
                        deco._modal_func_name = func_name

            # Deploy single shared Modal app
            with modal_sdk.enable_output():
                self.__class__._modal_app.deploy(environment_name="jason-dev")
                assert self.__class__._modal_app.app_id is not None
                self.__class__._modal_app_id = self.__class__._modal_app.app_id
                self.__class__._modal_app_deployed = True

    def runtime_init(self, flow, graph, package, run_id):
        """No-op for compatibility with @kubernetes."""
        pass

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
        """No-op for compatibility with @kubernetes"""
        return

    def runtime_finished(self, exception):
        if not self.__class__._modal_app_deployed:
            return

        # Best-effort stop of per-task Modal app
        app_id = getattr(self, "_pod_modal_app_id", None)
        if app_id:
            try:
                result = subprocess.run(
                    ["modal", "app", "stop", app_id], capture_output=True, text=True
                )
                if result.returncode == 0:
                    self.__class__._modal_app_deployed = False
            except Exception:
                pass
        return

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
        # Persist references for use in decorate/finish
        self.metadata = metadata
        self.task_datastore = task_datastore
        self._current_step_name = step_name
        self._run_id = run_id
        self._task_id = task_id
        self._modal_timeout = int(
            self.attributes.get("timeout") or DEFAULT_RUNTIME_LIMIT
        )

        # Write metadata for observability
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

        # Capture code package identifiers from env (Argo pods) or fall back to saved ones
        self.__class__.package_metadata = os.environ.get(
            "METAFLOW_CODE_METADATA", self.__class__.package_metadata
        )
        self.__class__.package_sha = os.environ.get(
            "METAFLOW_CODE_SHA", self.__class__.package_sha
        )
        self.__class__.package_url = os.environ.get(
            "METAFLOW_CODE_URL", self.__class__.package_url
        )

        # Compute input_paths string for joins/foreach
        input_paths_list: list[str] = []
        try:
            if inputs:
                for inp in inputs:
                    ps = getattr(inp, "pathspec", None)
                    if ps is None and hasattr(inp, "task"):
                        ps = getattr(inp.task, "pathspec", None)
                    if ps:
                        input_paths_list.append(str(ps))
        except Exception:
            # Best-effort; leave empty if unavailable
            input_paths_list = []
        self._input_paths = ",".join(input_paths_list) if input_paths_list else None

    def task_decorate(
        self, step_func, flow, graph, retry_count, max_user_code_retries, ubf_context
    ):
        """Replace step body with a wrapper that invokes 'metaflow modal step'."""
        step_name = getattr(self, "_current_step_name", None)
        run_id = getattr(self, "_run_id", None)
        task_id = getattr(self, "_task_id", None)
        input_paths = getattr(self, "_input_paths", None)
        modal_app_name = getattr(self, "_pod_modal_app_name", None)
        modal_func_name = getattr(self, "_modal_func_name", None)
        run_time_limit = getattr(self, "_modal_timeout", None)

        if modal_app_name is None:
            raise ModalDecoratorException(
                "Unrecoverable: Unknown Modal app name for Flow"
            )

        if modal_func_name is None:
            raise ModalDecoratorException(
                "Unrecoverable: Unknown Modal function name for Flow"
            )

        # Environment variables captured from @environment decorator
        env_args: list[str] = []
        if self.env_vars:
            for k, v in self.env_vars.items():
                env_args.extend(["--environment", f"{k}={v}"])

        def wrapper(*args, **kwargs):
            # Build CLI args for 'python <flowfile> modal step ...'
            flow_entry = sys.argv[0]
            cmd: list[str] = [
                sys.executable,
                flow_entry,
                "modal",
                "step",
                str(step_name),
                str(self.__class__.package_metadata or ""),
                str(self.__class__.package_sha or ""),
                str(self.__class__.package_url or ""),
                "--run-id",
                str(run_id),
                "--task-id",
                str(task_id),
            ]
            if input_paths:
                cmd.extend(["--input-paths", input_paths])
            cmd.extend(
                [
                    "--retry-count",
                    str(retry_count or 0),
                    "--max-user-code-retries",
                    str(max_user_code_retries or 0),
                    "--modal-app-name",
                    str(modal_app_name),
                    "--modal-func-name",
                    str(modal_func_name),
                    "--run-time-limit",
                    str(run_time_limit or DEFAULT_RUNTIME_LIMIT),
                ]
            )
            # Append any environment passthroughs
            cmd.extend(env_args)

            # Prepare environment for subprocess
            env = os.environ.copy()
            # Ensure METAFLOW_FLOW_FILENAME is present
            env.setdefault("METAFLOW_FLOW_FILENAME", os.path.basename(sys.argv[0]))

            result = subprocess.run(cmd)
            if result.returncode != 0:
                raise ModalDecoratorException(
                    f"Modal step failed with exit code {result.returncode}"
                )
            # Modal executed the step remotely; skip local user code
            return None

        return wrapper

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
