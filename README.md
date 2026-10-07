# Modal Metaflow

A [Metaflow](https://metaflow.org) extension that runs steps on [Modal](https://modal.com) with the `@modal` decorator.

```python
import modal as modal_sdk
from metaflow import FlowSpec, modal, step


class ModalFlow(FlowSpec):
    @step
    def start(self):
        self.x = 3
        self.next(self.train)

    @modal(gpu="H100", image=modal_sdk.Image.debian_slim().pip_install("torch"))
    @step
    def train(self):
        self.y = self.x * 2  # runs on Modal
        self.next(self.end)

    @step
    def end(self):
        print(self.y)


if __name__ == "__main__":
    ModalFlow()
```

## Install

```bash
pip install git+https://github.com/modal-projects/modal-metaflow
```

`@modal` steps need an S3 datastore (`--datastore=s3`) and S3 credentials inside the Modal container, for example via a secret:

```python
@modal(secrets=[modal_sdk.Secret.from_name("aws-credentials")])
```

## Options

| Option | Meaning |
| --- | --- |
| `image` | `modal.Image` to run the step in. |
| `cpu`, `memory`, `gpu` | Resources, as in `@app.function`. `@resources` is also honored. |
| `secrets`, `volumes` | Modal secrets and volumes to attach. |
| `timeout` | Step timeout in seconds. Defaults to 24 hours; longer needs `mark_reentrant=True`. |
| `retries` | Modal-level retries. Metaflow's `@retry` also works. |
| `environment` | Modal environment to run in. |
| `role_arn` | AWS role to assume via [Modal OIDC](https://modal.com/docs/guide/oidc-integration). |
| `clustered_size`, `clustered_rdma` | Run the step on several nodes, see below. |

Each step of a run deploys a Modal app named `<flow>-<run_id>-<step>`. It is stopped, best effort, when the run finishes (or the task, on Argo).

## Multi-node steps

`clustered_size=N` runs the step on N nodes, like [`modal.clustered`](https://modal.com/docs/guide/multi-node-clusters). Every node runs the same step code; use `current.modal_cluster` to coordinate:

```python
@modal(gpu="H100:8", clustered_size=2)
@step
def train(self):
    from metaflow import current

    cluster = current.modal_cluster  # node_rank, node_ips, world_size, cluster_id
    # e.g. launch torchrun with --node-rank=cluster.node_rank --master-addr=cluster.node_ips[0]
    self.next(self.end)
```

- Only rank 0 saves artifacts for the next step.
- The step fails if any node fails.
- Port 29501 is reserved on rank 0.
- `clustered_rdma=True` requests RDMA. On AWS hosts NCCL uses the EFA plugin, which needs `libcudart.so` in the image (for example an `nvidia/cuda` devel image).

## Argo Workflows

`@modal` steps work in flows deployed with `argo-workflows create`. The Argo pod launches the step on Modal, so it needs:

- a Kubernetes secret `modal-argo-creds` with `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`
- the launcher image `ghcr.io/modal-projects/modal-metaflow:latest` (override with `METAFLOW_DEFAULT_IMAGE`)

`foreach` is not yet supported on Argo.

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md).
