"""Modal StepDecorator implementation.

The ModalDecorator is responsible for StepDecorator interop, gathering the configuration needed to wrap
individual step functions, and deploying the App containing the @app.function-wrapped versions of those
step functions. It also passes app/function names to `metaflow modal step --modal-app-name foo --modal-func-name bar`.
"""

import hashlib
import json
import os
import subprocess
import sys
import uuid
import tempfile
from typing import Optional

import modal as modal_sdk
from .runner import metaflow_entry
from metaflow.decorators import StepDecorator
from metaflow.exception import MetaflowException
from metaflow.metadata_provider.metadata import MetaDatum
from metaflow.metadata_provider.util import sync_local_metadata_to_datastore
from metaflow.metaflow_config import (
    DATASTORE_LOCAL_DIR,
    DEFAULT_RUNTIME_LIMIT,
    FEAT_ALWAYS_UPLOAD_CODE_PACKAGE,
)
from metaflow.packaging_sys import ContentType
from metaflow.util import get_username, resolve_identity


def in_modal_worker() -> bool:
    """Check if we're running inside a Modal worker process."""
    return os.getenv("METAFLOW_MODAL_WORKER") == "1"


def _sanitize_modal_app_name(flow_name: str, run_id: str, step_name: str) -> str:
    """
    Sanitize app name to meet Modal's requirements:
    - Only alphanumeric characters, dashes, periods, and underscores
    - Must be shorter than 64 characters
    - Cannot conflict with App ID strings
    """
    # Replace dots with dashes to avoid issues
    clean_flow_name = flow_name.replace(".", "-")
    clean_run_id = run_id.replace(".", "-")

    # Create base name
    base_name = f"{clean_flow_name}-{clean_run_id}-{step_name}"

    # If too long, truncate and add hash suffix for uniqueness
    if len(base_name) > 63:  # Leave room for potential suffixes
        # Create a short hash of the full name to ensure uniqueness
        full_hash = hashlib.sha256(base_name.encode()).hexdigest()[:8]

        # Truncate the base name and add hash
        max_base_len = 63 - 9  # 8 chars for hash + 1 for dash
        truncated_base = base_name[:max_base_len].rstrip("-")
        base_name = f"{truncated_base}-{full_hash}"

    return base_name


def _get_or_create_modal_app(
    app_name: str,
    func_name: str,
    modal_func_kwargs: dict,
    modal_environment: Optional[str] = None,
) -> tuple[modal_sdk.App, str]:
    """
    Get or create a Modal app using deterministic naming.
    Returns (app, function_name).

    Uses Modal's built-in idempotency - multiple processes can safely
    call this with the same parameters.
    """
    try:
        # Try to lookup existing app first
        app = modal_sdk.App.lookup(app_name, environment_name=modal_environment)
        return app, func_name
    except modal_sdk.exception.NotFoundError:
        # App doesn't exist, deploy it
        # Create and deploy the app
        app = modal_sdk.App(name=app_name, include_source=True)
        app.function(**modal_func_kwargs)(metaflow_entry)

        # Deploy the app
        if modal_environment:
            # TODO
            os.environ["MODAL_ENVIRONMENT"] = modal_environment
            app.deploy(environment_name=modal_environment)
        else:
            app.deploy()

        return app, func_name


patch_allow = [
    "step_init",
    # "package_init",
    # "add_to_package",
    "step_task_retry_count",
    "runtime_init",
    "runtime_task_created",
    "runtime_finished",
    "runtime_step_cli",
    "task_pre_step",
    "task_decorate",
    "task_post_step",
    "task_exception",
    "task_finished",
]


def disable_decorator(deco):
    s = StepDecorator()
    for method_name in patch_allow:
        method = getattr(s, method_name)
        setattr(deco, method_name, method)


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
        # decorator-specific kwargs
        "role_arn": None,
        "mark_reentrant": False,
        "environment": None,  # optional Modal environment name
    }
    # Keep only truly global class flags that need to be shared
    _has_logged_reentrant_warning = False
    package_metadata = None
    package_url = None
    package_sha = None
    _metaflow_home = None

    def init(self):
        self.env_vars = None
        self.requirements = []
        self.python_version = None
        self.role_arn = self.attributes.pop("role_arn")
        self.mark_reentrant = self.attributes.pop("mark_reentrant")
        # Resolved Modal environment name for this step (from attribute or env)
        self._modal_environment_name: Optional[str] = None

        # Per-instance attributes for each step
        self.modal_app: Optional[modal_sdk.App] = None
        self.modal_app_name: Optional[str] = None
        self.modal_app_id: Optional[str] = None
        self.modal_func_name: Optional[str] = None
        self.modal_timeout: Optional[int] = None

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
        """Lightweight step initialization - heavy lifting moved to runtime_init."""
        # Skip if we're running inside a Modal worker
        if in_modal_worker():
            return

        if flow_datastore.TYPE != "s3":
            raise ModalDecoratorException(
                "The *@modal* decorator requires --datastore=s3 at the moment."
            )

        # Store basic context
        self.environment = environment
        self.flow_datastore = flow_datastore
        self.step_name = step_name

        # Parse and validate timeout configuration (needed for step validation)
        timeout_val = self._parse_timeout(decorators, logger)
        self.attributes["timeout"] = timeout_val

        # Handle interactions with other decorators
        self._handle_decorator_interactions(decorators)

    def _handle_decorator_interactions(self, decorators):
        """Handle interactions with other decorators, especially Argo-injected ones."""
        in_argo = False
        for deco in decorators:
            if hasattr(deco, "name"):
                if deco.name == "kubernetes":
                    # If the user explicitly authored @kubernetes on this step, keep prior behavior.
                    is_user_k8s = getattr(deco, "statically_defined", False)
                    if is_user_k8s:
                        raise ModalDecoratorException(
                            "@kubernetes and @modal are mutually exclusive on a user-authored decorator. Please remove one of them from your step."
                        )
                    else:
                        # Argo injected @kubernetes. Minimize pod resources and inject required secrets
                        in_argo = True
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
                        k8s_attrs["image"] = (
                            os.getenv("METAFLOW_DEFAULT_IMAGE")
                            or "ghcr.io/thomasjpfan/modal-client:0.0.3"
                        )

                        # Per-step Modal secret injection
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

                elif deco.name == "batch":
                    raise ModalDecoratorException(
                        "@batch and @modal are mutually exclusive."
                    )

                # Collect information from other decorators
                elif deco.name == "environment":
                    if hasattr(deco, "vars") and len(deco.vars) > 0:
                        self.env_vars = deco.vars

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
                    # TODO: Handle conda decorator
                    pass

                elif deco.name == "resources":
                    if hasattr(deco, "cpu") and deco.cpu is not None:
                        self.attributes["cpu"] = max(
                            self.attributes["cpu"] or -1, float(deco.cpu)
                        )

                    if hasattr(deco, "gpu") and deco.gpu is not None:
                        gpu_cfg = self.attributes.pop("gpu", None)
                        self.attributes["gpu"] = _handle_gpu_cfg(
                            gpu_cfg,
                            deco.gpu,
                            lambda msg, **kwargs: print(f"[Modal] {msg}"),
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
                        pass  # Modal backend does not use shared_memory

        # if we deployed with argo, but are not yet in the modal worker
        # then we want to disable all other decorators to prevent them from running twice
        # in modal worker we skip, so that the decorators can run there.
        if not in_modal_worker() and in_argo:
            for deco in decorators:
                if deco.name in ["model", "checkpoint"]:
                    disable_decorator(deco)

    def runtime_init(self, flow, graph, package, run_id):
        """Deploy Modal app and save package info for local execution."""
        # Store flow context
        self.flow = flow
        self.graph = graph
        self.package = package
        self.run_id = run_id

        # Skip if we're running inside a Modal worker
        if in_modal_worker():
            return

        # Save code package for local execution
        self._save_package_once(self.flow_datastore, package)

        # Extract the full code package (including non-Python files like requirements.txt)
        # following the @conda decorator pattern for accessing external files
        if self.__class__._metaflow_home is None:
            # Do this ONCE per flow - use class-level shared directory
            self.__class__._metaflow_home = tempfile.TemporaryDirectory(dir="/tmp")
            package.extract_into(
                self.__class__._metaflow_home.name, ContentType.ALL_CONTENT
            )

        # Deploy Modal app for local execution
        step_name = getattr(self, "step_name", "unknown")

        # Get Modal environment
        modal_environment = (
            self.attributes.get("environment")
            or os.environ.get("METAFLOW_MODAL_ENVIRONMENT")
            or os.environ.get("MODAL_ENVIRONMENT")
        )

        # Build Modal function kwargs
        modal_func_kwargs = {
            "timeout": int(self.attributes.get("timeout", DEFAULT_RUNTIME_LIMIT)),
        }

        # Add optional attributes
        if self.attributes.get("image") is not None:
            modal_func_kwargs["image"] = self.attributes["image"]
        if self.attributes.get("secrets"):
            modal_func_kwargs["secrets"] = self.attributes["secrets"]
        if self.attributes.get("cpu") is not None:
            modal_func_kwargs["cpu"] = self.attributes["cpu"]
        if self.attributes.get("gpu") is not None:
            modal_func_kwargs["gpu"] = self.attributes["gpu"]
        if self.attributes.get("memory") is not None:
            modal_func_kwargs["memory"] = self.attributes["memory"]
        if self.attributes.get("retries"):
            modal_func_kwargs["retries"] = self.attributes["retries"]
        if self.attributes.get("volumes"):
            modal_func_kwargs["volumes"] = self.attributes["volumes"]

        # Deploy the app for local execution
        try:
            flow_name = flow.name
            app_name = _sanitize_modal_app_name(flow_name, run_id, step_name)
            func_name = f"{flow_name}__{step_name}"
            app, _ = _get_or_create_modal_app(
                app_name,
                func_name,
                modal_func_kwargs=modal_func_kwargs,
                modal_environment=modal_environment,
            )
            # Save app ID for cleanup
            self.deployed_app_id = getattr(app, "app_id", None)
        except Exception as e:
            raise ModalDecoratorException(
                f"Failed to deploy Modal app in runtime_init: {e}"
            )

    def runtime_task_created(
        self, task_datastore, task_id, split_index, input_paths, is_cloned, ubf_context
    ):
        """No-op - deployment moved to task_decorate for Argo compatibility."""
        pass

    def runtime_step_cli(
        self, cli_args, retry_count, max_user_code_retries, ubf_context
    ):
        """Rewrite CLI to use modal execution (works in local, no-op in Argo)."""
        if retry_count <= max_user_code_retries:
            # Get package info from class variables (set by runtime_init)
            package_url = getattr(self.__class__, "package_url", None)
            package_sha = getattr(self.__class__, "package_sha", None)
            package_metadata = getattr(self.__class__, "package_metadata", None)

            # Only rewrite CLI if we have package info (local execution)
            if package_url:
                cli_args.commands = ["modal", "step"]
                cli_args.command_args.extend(
                    [package_metadata or "", package_sha or "", package_url or ""]
                )

                # Add Modal-specific arguments
                flow_name = getattr(self.flow, "name", "unknown")
                step_name = getattr(self, "step_name", "unknown")
                run_id = getattr(self, "run_id", "unknown")
                app_name = _sanitize_modal_app_name(flow_name, run_id, step_name)

                cli_args.command_options["modal-app-name"] = app_name
                cli_args.command_options["modal-func-name"] = (
                    f"{flow_name}__{step_name}"
                )
                cli_args.command_options["run-time-limit"] = self.attributes.get(
                    "timeout", DEFAULT_RUNTIME_LIMIT
                )
            else:
                print(
                    "[Modal] No package info - CLI rewrite skipped, will use task_decorate"
                )

    def runtime_finished(self, exception):
        """Clean up deployed Modal app for local execution."""
        if hasattr(self, "deployed_app_id") and self.deployed_app_id:
            try:
                stop_cmd = ["modal", "app", "stop", str(self.deployed_app_id)]
                subprocess.run(stop_cmd, capture_output=True, text=True, timeout=30)
            except Exception:
                # App cleanup failures are non-fatal
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
        """Task pre-step hook - capture code package info for task_decorate."""
        # Debug: Log metadata configuration and namespace in Argo pod

        print("[Modal Debug] task_pre_step in Argo pod:")
        print(
            f"[Modal Debug] METAFLOW_DEFAULT_METADATA: {os.environ.get('METAFLOW_DEFAULT_METADATA')}"
        )
        print(
            f"[Modal Debug] METAFLOW_SERVICE_URL: {os.environ.get('METAFLOW_SERVICE_URL')}"
        )
        print(f"[Modal Debug] run_id: {run_id}")
        print(
            f"[Modal Debug] Metadata backend type: {metadata.TYPE if hasattr(metadata, 'TYPE') else 'UNKNOWN'}"
        )
        print(f"[Modal Debug] Resolved identity: {resolve_identity()}")
        print(f"[Modal Debug] Username: {get_username()}")
        print(f"[Modal Debug] USER env: {os.environ.get('USER')}")
        print(f"[Modal Debug] USERNAME env: {os.environ.get('USERNAME')}")
        print(f"[Modal Debug] METAFLOW_USER env: {os.environ.get('METAFLOW_USER')}")
        print(
            f"[Modal Debug] METAFLOW_PRODUCTION_TOKEN env: {os.environ.get('METAFLOW_PRODUCTION_TOKEN')}"
        )

        # Skip if we're inside a Modal worker
        if in_modal_worker():
            return

        # Persist references for use in task_decorate
        self.metadata = metadata
        self.task_datastore = task_datastore
        self._run_id = run_id
        self._task_id = task_id
        self._step_name = step_name

        # Try multiple sources for code package information
        # 1. Environment variables (set by runtime in parent process)
        self._code_package_url = os.environ.get("METAFLOW_CODE_URL")
        self._code_package_sha = os.environ.get("METAFLOW_CODE_SHA")
        self._code_package_metadata = os.environ.get("METAFLOW_CODE_METADATA")

        # 2. Try to access through flow's package if available
        if hasattr(flow, "_package"):
            try:
                test_url = flow._package.package_url()
                test_sha = flow._package.package_sha()
                test_metadata = flow._package.package_metadata
                if test_url:
                    self._code_package_url = test_url
                    self._code_package_sha = test_sha
                    self._code_package_metadata = test_metadata
            except Exception:
                pass

        # 3. Try to access class-level package info if available
        if not self._code_package_url and hasattr(self.__class__, "package_url"):
            self._code_package_url = self.__class__.package_url
            self._code_package_sha = self.__class__.package_sha
            self._code_package_metadata = self.__class__.package_metadata

        # Store task metadata for observability
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
        """Task decorate hook - fallback for Argo execution only."""
        # If we're inside a Modal worker, execute user code directly

        if in_modal_worker():
            print("[Modal Worker Debug]: in task_decorate")
            print(f"[Modal Worker Debug]: image {self.attributes.get('image')}")
            print(f"[Modal Worker Debug]: all attributes {self.attributes}")
            return step_func

        # Check if this is local execution where runtime_step_cli should have handled this
        # In local execution, runtime_* hooks run and package info is available
        package_url = getattr(self.__class__, "package_url", None)
        if package_url:
            # In local execution, the CLI should have been rewritten by runtime_step_cli
            # If we're here, something went wrong with the CLI rewrite
            def error_wrapper(*args, **kwargs):
                raise ModalDecoratorException(
                    "Local execution: CLI rewrite failed. Expected modal CLI to be called, but task wrapper invoked."
                )

            return error_wrapper

        # This is Argo execution - deploy app and execute via Modal

        def wrapper(*args, **kwargs):
            print("[Modal Debug] in task_decorate wrapper")
            try:
                # Extract the full code package (including non-Python files like requirements.txt)
                # following the @conda decorator pattern for accessing external files in Argo execution
                if self.__class__._metaflow_home is None:
                    # Do this ONCE per flow - use class-level shared directory
                    self.__class__._metaflow_home = tempfile.TemporaryDirectory(
                        dir="/tmp"
                    )
                    # In Argo, the package is already downloaded and available through the flow
                    if hasattr(flow, "_package"):
                        flow._package.extract_into(
                            self.__class__._metaflow_home.name, ContentType.ALL_CONTENT
                        )

                # Get code package information from environment variables (set by Argo)
                code_package_url = os.environ.get("METAFLOW_CODE_URL")
                code_package_sha = os.environ.get("METAFLOW_CODE_SHA")
                code_package_metadata = os.environ.get("METAFLOW_CODE_METADATA")

                if not code_package_url:
                    raise ModalDecoratorException(
                        "Argo execution: Code package URL not available from environment variables. "
                        "Ensure Argo is properly configured with METAFLOW_CODE_* variables."
                    )

                # Get flow and step context
                flow_name = flow.name
                run_id = os.getenv("METAFLOW_RUN_ID", str(uuid.uuid4()))
                step_name = step_func.__name__

                # Build Modal function kwargs
                modal_func_kwargs = {
                    "timeout": int(
                        self.attributes.get("timeout", DEFAULT_RUNTIME_LIMIT)
                    ),
                }

                # Add optional attributes
                print(f"[Modal Worker Debug]: all attributes {self.attributes}")
                if self.attributes.get("image") is not None:
                    print(f"[Modal Debug] image: {self.attributes['image']}")
                    modal_func_kwargs["image"] = self.attributes["image"]
                if self.attributes.get("secrets"):
                    modal_func_kwargs["secrets"] = self.attributes["secrets"]
                if self.attributes.get("cpu") is not None:
                    modal_func_kwargs["cpu"] = self.attributes["cpu"]
                if self.attributes.get("gpu") is not None:
                    modal_func_kwargs["gpu"] = self.attributes["gpu"]
                if self.attributes.get("memory") is not None:
                    modal_func_kwargs["memory"] = self.attributes["memory"]
                if self.attributes.get("retries"):
                    modal_func_kwargs["retries"] = self.attributes["retries"]
                if self.attributes.get("volumes"):
                    modal_func_kwargs["volumes"] = self.attributes["volumes"]

                # Get Modal environment
                modal_environment = (
                    self.attributes.get("environment")
                    or os.environ.get("MODAL_ENVIRONMENT")
                    or os.environ.get("METAFLOW_MODAL_ENVIRONMENT")
                    or modal_sdk.config.Config().get("environment")
                )

                # Deploy the Modal app first (needed before calling modal CLI)
                app_name = _sanitize_modal_app_name(flow_name, run_id, step_name)
                func_name = f"{flow_name}__{step_name}"
                app, func_name = _get_or_create_modal_app(
                    app_name,
                    func_name,
                    modal_func_kwargs=modal_func_kwargs,
                    modal_environment=modal_environment,
                )

                # Store app info for cleanup in task_finished
                self.argo_deployed_app_id = getattr(app, "app_id", None)
                self.argo_app_name = app.name if app else None

                # Now call modal CLI which will find the deployed app
                task_id = os.getenv("METAFLOW_TASK_ID", "unknown")

                # Build modal CLI command (same pattern as runtime_step_cli)
                script_name = os.environ.get("METAFLOW_FLOW_FILENAME", "basic.py")

                # Extract input paths from sys.argv (Oracle's fix)
                def _extract_input_paths():
                    """Extract input paths from sys.argv, handling both CLI args and env vars."""
                    # First try environment variable
                    if "METAFLOW_INPUT_PATHS" in os.environ:
                        return os.environ["METAFLOW_INPUT_PATHS"]

                    # Then try split environment variables
                    pieces = [
                        os.environ[k]
                        for k in sorted(os.environ)
                        if k.startswith("METAFLOW_INPUT_PATHS_")
                    ]
                    if pieces:
                        return "".join(pieces)

                    # Finally extract from sys.argv
                    try:
                        idx = sys.argv.index("--input-paths")
                        if idx + 1 < len(sys.argv):
                            return sys.argv[idx + 1]
                    except (ValueError, IndexError):
                        pass

                    return ""

                input_paths = _extract_input_paths()

                modal_cmd = [
                    sys.executable,
                    script_name,  # basic.py
                    "modal",
                    "step",
                    step_name,
                    code_package_metadata,
                    code_package_sha,
                    code_package_url,
                    "--modal-app-name",
                    app_name,
                    "--modal-func-name",
                    func_name,
                    "--run-id",
                    run_id,
                    "--task-id",
                    task_id,
                ]

                if self.role_arn:
                    modal_cmd.extend(["--modal-role-arn", self.role_arn])

                # Add input-paths if available
                if input_paths:
                    modal_cmd.extend(["--input-paths", input_paths])

                # Add environment variables if we have them
                if hasattr(self, "env_vars") and self.env_vars:
                    for k, v in self.env_vars.items():
                        modal_cmd.extend(["--environment", f"{k}={v}"])

                if modal_environment:
                    modal_cmd.extend(
                        [
                            "--environment",
                            f"METAFLOW_MODAL_ENVIRONMENT={modal_environment}",
                        ]
                    )

                # Execute the modal CLI from the Argo pod
                completed = subprocess.run(
                    modal_cmd, capture_output=True, text=True, env=dict(os.environ)
                )

                # Process modal CLI results
                exit_code = completed.returncode

                if exit_code != 0:
                    stdout = completed.stdout or ""
                    stderr = completed.stderr or ""
                    raise ModalDecoratorException(
                        f"Modal CLI failed with exit code {exit_code}. "
                        f"STDOUT: {stdout} STDERR: {stderr}"
                    )

                # Load artifacts produced by Modal worker back into local flow
                try:
                    # Get the datastore for this task
                    task_datastore = self.flow_datastore.get_task_datastore(
                        run_id, step_name, task_id, mode="r"
                    )

                    # Set the datastore on the flow object - artifacts will be loaded on-demand
                    flow._set_datastore(task_datastore)

                except Exception as e:
                    # This is a critical failure - artifact persistence won't work
                    raise ModalDecoratorException(
                        f"Failed to load artifacts from Modal worker: {e}"
                    )

                # Return None to skip local execution
                return None

            except Exception as e:
                raise ModalDecoratorException(f"Modal Argo execution failed: {e}")

        return wrapper

    def task_post_step(
        self, step_name, flow, graph, retry_count, max_user_code_retries
    ):
        """Task post-step hook."""
        pass

    def task_exception(
        self, exception, step_name, flow, graph, retry_count, max_user_code_retries
    ):
        """Task exception hook."""
        return

    def task_finished(
        self, step_name, flow, graph, is_task_ok, retry_count, max_user_code_retries
    ):
        """Task finished hook."""
        if hasattr(self, "metadata") and self.metadata.TYPE == "local":
            # Note that the datastore is *always* Amazon S3 (see
            # runtime_task_created function).
            print("[Modal] Syncing local metadata to datastore")
            sync_local_metadata_to_datastore(DATASTORE_LOCAL_DIR, self.task_datastore)

        # Clean up Modal app deployed in Argo execution
        if hasattr(self, "argo_deployed_app_id") and self.argo_deployed_app_id:
            try:
                stop_cmd = ["modal", "app", "stop", str(self.argo_deployed_app_id)]
                subprocess.run(stop_cmd, capture_output=True, text=True, timeout=30)
            except Exception:
                # App cleanup failures are non-fatal
                pass

        return

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
        logger("No GPU type specified in @modal step, defaulting to H100.")
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
                    logger(
                        "Num GPUs requested in @modal and @resources conflict; falling back to @modal gpu config."
                    )
                return modal_gpu
            else:
                # combine GPU type and resources gpu amount
                return f"{gpu_split}:{resources_gpu}"

    elif isinstance(modal_gpu, list):
        logger("Complex GPU type requested in @modal step, ignoring @resources gpu.")
        return modal_gpu
