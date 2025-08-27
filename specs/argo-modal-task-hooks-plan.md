# Plan: Move Modal redirection to task_* hooks (Argo-compatible), keep local runs working

This document outlines a clean, Argo-compatible refactor of the Modal integration that relies exclusively on task_* hooks (invoked in Argo pods) rather than runtime_* hooks (which are skipped in Argo pod execution paths).

## Constraints and facts

- In Argo pods, the “metaflow step …” CLI invokes the Task lifecycle, not NativeRuntime:
  - The pod executes `step_cmd.step` which constructs a `MetaflowTask` and calls `task.run_step`.
  - `run_step` invokes only the following step decorator hooks inside the pod: `task_pre_step`, `task_decorate`, `task_post_step`, `task_exception`, `task_finished`.
  - NativeRuntime’s `runtime_step_cli` is NOT used in the “step” CLI path. This is why our runtime-level redirection never runs in Argo pods.

- Local runs (metaflow run/resume) use NativeRuntime, which does call `runtime_step_cli`. However, those runs still fork a subprocess that executes “metaflow step …” where the `task_*` hooks also run. We can safely transition to `task_*` without breaking local runs.

- We cannot modify the Argo plugin; all changes must be done in the decorator.

- Credentials:
  - Argo pods have AWS creds via a global k8s secret set using `METAFLOW_ARGO_WORKFLOWS_KUBERNETES_SECRETS="s3-credentials"`.
  - Modal tokens are injected per @modal step by adding the modal secret to the kubernetes decorator’s `secrets` list in `step_init` (default `modal-argo-creds`, overrideable via env).
  - Modal SDK must be importable in the pod interpreter. We will continue to use `@pypi_base(packages={"modal": "<version>"})` to ensure the interpreter has modal for every step, or a base image with modal preinstalled.

## High-level approach

- Redirect to Modal inside the pod using the `task_*` hooks, not at `runtime_*`.
- For any step with `@modal`:
  - `task_pre_step`: build and deploy a Modal app with a single serialized function wrapping this step’s shell execution (the same wrapper logic as we currently use). Capture `input_paths` for redirection. Read code package identifiers from pod environment variables.
  - `task_decorate`: replace the step function with a wrapper that invokes our existing CLI “metaflow modal step …” (which spawns the Modal function and waits), then bypasses the local user code if the Modal run succeeded.
  - `task_finished`: stop the Modal app for this task to clean up resources.

This keeps each Argo pod as a lightweight launcher that performs only:
- pypi/conda bootstrap (to install modal)
- “metaflow modal step …” to spawn Modal and stream logs back

No Argo plugin changes. No reliance on `runtime_step_cli`.

## Detailed design

### What to keep in `step_init` (compile-time)

- Keep existing parsing/collection of:
  - `@resources`, `@environment`, `@pypi` attributes.
  - Timeout parsing (`_parse_timeout`), including 24h cap unless `mark_reentrant=True` for Modal.
- Detect Argo-injected `@kubernetes`:
  - Minimize pod resources for @modal steps (cpu=0.1, memory=128, disk=256, gpu=0, qos=Burstable).
  - Inject per-step modal secret into the kubernetes decorator:
    - Default `modal-argo-creds`; override via `METAFLOW_MODAL_ARGO_K8S_SECRET_MODAL`.
  - Do NOT inject AWS secrets per-step; rely on global `METAFLOW_ARGO_WORKFLOWS_KUBERNETES_SECRETS="s3-credentials"`.

Note: `step_init` runs during deployment and local run setup; it doesn’t run in the Argo pod at “step” CLI time, so redirection logic must NOT live here.

### New pod-side redirection using `task_*`

We’ll unify redirection through `task_*` so both local and Argo pods use the same code path.

- `task_pre_step(self, step_name, task_datastore, metadata, run_id, task_id, flow, graph, retry_count, max_user_code_retries, ubf_context, inputs)`

  1) Capture code package identifiers from the pod env (set by Argo):
     - `METAFLOW_CODE_METADATA`, `METAFLOW_CODE_SHA`, `METAFLOW_CODE_URL`.
     - Store them on the decorator instance for use by the wrapper (so we don’t rely on `_save_package_once`/`runtime_task_created` paths).

  2) Compute and store `input_paths` string for this step:
     - If `inputs` is present, build a comma-separated string from each `TaskDataStore.pathspec` (e.g., `run_id/step/task_id`).
     - For start steps, leave it empty.

  3) Deploy a Modal app for this step (if not already deployed for this task) and register the function:
     - Import `modal` (SDK must be present; ensured by @pypi_base or base image).
     - Create an `App` and attach a single `@app.function(serialized=True, name=func_name, **modal_attributes)` where:
       - `func_name = f"{flow.__class__.__name__}__{step_name}"` (stable per step)
       - `modal_attributes` filtered from decorator `attributes` (image, secrets, gpu, etc.)
     - Function body is the same subprocess wrapper you already have (it runs a “step shell” that bootstraps, runs the step, captures logs).
     - Deploy the app; capture `app_id`, `app_name` and `func_name` back onto the decorator instance.

  4) Timeout: re-use your parsed Modal timeout value (from `_parse_timeout`), store it on the decorator instance for CLI use.

- `task_decorate(self, step_func, flow, graph, retry_count, max_user_code_retries, ubf_context) -> callable`

  Replace `step_func` with a wrapper that:

  1) Constructs “metaflow modal step …” with arguments:
     - Positional:
       - `step-name`
       - `code-package-metadata`
       - `code-package-sha`
       - `code-package-url`
     - Options:
       - `--run-id`, `--task-id`
       - `--input-paths <captured string>` (if any)
       - `--split-index` if you need it (can be added later for foreach)
       - `--retry-count`, `--max-user-code-retries`
       - `--modal-app-name <stored app name>`, `--modal-func-name <stored func name>`
       - `--run-time-limit <stored Modal timeout>`
  2) Sets env:
     - `METAFLOW_FLOW_FILENAME` (modal_cli expects this) and any `@environment` vars were already gathered in `step_init` and can be added again here if needed.
  3) Invokes the CLI via subprocess and waits for completion.
  4) On exit code != 0, raise to mark the task failed (allows Argo retries).
  5) On success, return without executing the user’s step body (Modal already did it and wrote artifacts).

- `task_finished(self, step_name, flow, graph, is_task_ok, retry_count, max_user_code_retries)`

  - Stop the Modal app that was deployed for this task (best-effort).
  - This provides per-task cleanup. Since Argo runs each step in separate pods, this matches the lifecycle; we don’t need a run-level `runtime_finished`.

### Local runs

- Local runs via `metaflow run` will also call `task_*` hooks inside the step process; the redirection wrapper will execute “metaflow modal step …” there as well.
- We can keep `runtime_step_cli` if you want to preserve the current local behavior, but it’s no longer necessary once `task_*` does the redirection. Removing `runtime_step_cli` reduces complexity.

### Interaction with Pypi/Conda

- Keep `@pypi_base(packages={"modal": "..."})` so that modal is available in every step interpreter (including the Argo pod’s “step” process).
- For “deploy” subprocess (run “argo-workflows create”), include `environment='pypi'` and set `METAFLOW_DATASTORE_SYSROOT_S3` in env so the pypi env is satisfied at deploy time.
- Alternatively use a k8s image with modal pre-installed and omit `@pypi_base`.

### Logging and retries

- Modal CLI prints remote stdout/stderr; the Argo pod wraps the whole command with `bash_capture_logs` so everything is captured and persisted as usual.
- Modal CLI returns a non-zero exit on timeout or error; the pod step fails and Argo retry strategy applies.

### Foreach / Join / Parallel

- For `join` steps, the input pathspecs are captured from `inputs` in `task_pre_step`; modal_cli uses them to reconstruct the proper “step” execution in the Modal function.
- For `foreach` split-index forwarding: we can add `--split-index` support by extracting it from the CLI options that the task runner received and passing it through to modal_cli (phase 2 enhancement).
- `@parallel` JobSets are handled by Argo; we generally do not plan to run `@parallel` steps on Modal for now.

### Cleanup and shared-state

- Use per-task modal Apps:
  - Deploy in `task_pre_step` and stop in `task_finished`. This avoids per-run shared state and lets cleanup happen reliably per pod.
- If you want to reuse a single app across steps locally (for performance), you can still retain the current class-level `_modal_app` approach for local runs only, keyed by a “not in pod” heuristic. In Argo pods, always deploy per-step.

### What to remove or move from current code

- Deprecate redirection in `runtime_step_cli` and move the redirection to `task_decorate`.
- Move Modal app deployment from `runtime_init` to `task_pre_step` (with a best-effort lazy import of modal).
- Keep compilation-time handling in `step_init`:
  - Minimizing Argo-injected `@kubernetes` for @modal steps
  - Injecting per-step Modal secret into `@kubernetes` decorator’s `secrets` list
  - Parsing @pypi, @environment, @resources, Modal timeout
- Keep `task_pre_step` metadata write (you already write `modal_execution` and `modal_config`).

### Testing checklist

1) Local run:
   - Without Argo: `python examples/basic.py --datastore s3 --datastore-root s3://... run`
   - Expect modal redirection prints and remote logs.

2) Argo deploy/trigger:
   - Ensure:
     - `METAFLOW_ARGO_WORKFLOWS_KUBERNETES_SECRETS="s3-credentials"`
     - Modal secret `modal-argo-creds` exists with `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`.
     - `@pypi_base(packages={"modal": "..."})` is present (or base image has modal).
   - Deploy: environment='pypi', env includes `METAFLOW_DATASTORE_SYSROOT_S3`.
   - Trigger and inspect pod logs:
     - Bootstrap logs
     - Modal CLI messages and remote function logs

3) Retry behavior:
   - Force a failure and confirm non-zero exit propagates, Argo retries with configured delays.

4) Join and input paths:
   - Confirm aggregated `input_paths` seen by modal_cli and remote step runs as expected.

5) Cleanup:
   - Confirm per-task Modal app is stopped in `task_finished` (best-effort).

### Risks and mitigations

- Modal app deploy overhead per step: acceptable for now; optimize later by caching per process/pod.
- Foreach split-index propagation: add later if needed (modal_cli already supports `--split-index`).
- Double-install of modal via `@pypi_base` and base image: choose one approach; don’t mix.

### Summary of code changes (at a glance)

- In `ModalDecorator`:
  - `step_init`: keep existing; minimize k8s for @modal steps; inject `modal-argo-creds`.
  - `task_pre_step`:
    - Read code package identifiers from env; compute `input_paths`; deploy step-specific modal app/function; store names/timeout on self.
  - `task_decorate`:
    - Return a wrapper that runs “metaflow modal step …” with proper args/env; bypass user code on success.
  - `task_finished`:
    - Stop modal app for this task.

- Remove or simplify:
  - `runtime_init`: no longer deploy modal app here (not invoked in Argo pod path).
  - `runtime_step_cli`: no longer required for redirection (keep only if you want to preserve old local behavior).

This approach aligns with Metaflow’s actual lifecycle in Argo pods, keeps the code clean (no Argo plugin patching), and preserves your existing modal_cli logic and logging semantics.
