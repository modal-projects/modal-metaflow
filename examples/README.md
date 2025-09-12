# Modal Metaflow - Examples

## Flow examples
- `examples/basic.py`: Basic execution example (single @modal step)
- `examples/join.py`: Branching execution example (multiple @modal steps + join)
- `examples/foreach.py`: (TODO) Iterative execution example (iterative @modal steps + foreach + join)
- `examples/outerbounds_lora.py`: Multi-GPU LoRA finetuning

## Infra
- `examples/metadata_service.py`: A Modal App that serves the Metaflow metadata service and metadata DB.
- `examples/deploy.py`: An Argo Workflows deployment of the `basic.py` example.

TODO: document kube requirements (modal in METAFLOW_CONTAINER_IMAGE or similar, etc.)

## Argo Instructions
- Optional: with nix/direnv, run `direnv allow` to get Minikube and the Argo CLI. Otherwise install them and set:
```
export ARGO_WORKFLOWS_VERSION=$(argo version | grep -m1 -oP 'refs/tags/v\K[0-9]+\.[0-9]+\.[0-9]+')
```

1) Start a local cluster (namespace `argo`):
```bash
minikube start --driver=docker --namespace argo
```

2) Create secrets:
- Modal:
```bash
kubectl create secret generic modal-argo-creds \
  --from-literal=MODAL_TOKEN_ID=ak-*** \
  --from-literal=MODAL_TOKEN_SECRET=as-***
```
- S3:
```bash
kubectl create secret generic s3-credentials \
  --from-env-file /tmp/aws_sso.env
```

3) Apply minimal Argo Workflows template to cluster:
```bash
kubectl apply -n argo -f "https://github.com/argoproj/argo-workflows/releases/download/${ARGO_WORKFLOWS_VERSION}/quick-start-minimal.yaml"
```

4) Open the Argo UI:
```bash
kubectl argo port-forward service/argo-server 2746:2746
```

5) Deploy a Flow with Argo Workflows:
```bash
python examples/deploy.py
```
