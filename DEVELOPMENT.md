# Development

## Setup

For Argo, install [k3d](https://k3d.io) and `kubectl`, create a Modal [service token](https://modal.com/docs/guide/service-users), and export `MODAL_METAFLOW_TOKEN_ID` and `MODAL_METAFLOW_TOKEN_SECRET`.

```bash
uv sync --dev
source .venv/bin/activate
inv wheel  # rerun after adding a dependency
```

## Run

Start Metaflow on Modal plus a local Argo cluster, then run the `source` command it prints:

```bash
inv develop
```

To skip Argo and only run flows locally, use `inv start-metaflow` instead.

```bash
python tests/flows/hello_world.py run
python tests/flows/hello_world.py argo-workflows create
python tests/flows/hello_world.py argo-workflows trigger
```

Tear everything down with `inv teardown`, or `inv stop-metaflow` if you skipped Argo.

## Test

The tests start their own services, but the Metaflow client needs the profile that `inv start-metaflow` writes:

```bash
source .modal_metaflow/activate
pytest tests --no-argo
```

Multinode tests need two 8-GPU nodes and are skipped unless `RUN_MULTINODE_TESTS=1` is set.

## Troubleshooting Argo

If the default launcher image fails with `no match for platform in manifest`, build a local one:

```bash
docker build -f Dockerfile.dev -t modal-metaflow-dev:local .
k3d image import modal-metaflow-dev:local -c metaflow-argo
export METAFLOW_DEFAULT_CONTAINER_IMAGE=modal-metaflow-dev:local  # regular steps
export METAFLOW_DEFAULT_IMAGE=modal-metaflow-dev:local            # modal launcher steps
```

If Argo's example MinIO fails to pull, disable its log archival and scale it down. Metaflow keeps its own logs in the Modal MinIO sandbox.

```bash
kubectl -n argo patch configmap artifact-repositories --type merge \
  -p '{"data":{"default-v1":"archiveLogs: false\n"}}'
kubectl -n argo scale deployment/minio --replicas=0
```
