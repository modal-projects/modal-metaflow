import time
from datetime import datetime, timedelta

from metaflow import Deployer

deployed_flow = (
    Deployer(
        "examples/basic.py",
        datastore="s3",
        datastore_root="s3://metaflow-modal",
        env={
            "METAFLOW_KUBERNETES_NAMESPACE": "argo",
            "METAFLOW_ARGO_WORKFLOWS_KUBERNETES_SECRETS": "s3-credentials,modal-argo-creds",
            "METAFLOW_DATASTORE_SYSROOT_S3": "s3://metaflow-modal",
            "METAFLOW_DEFAULT_CONTAINER_IMAGE": "ghcr.io/thomasjpfan/modal-client:0.0.3",
        },
    )
    .argo_workflows()
    .create()
)
print(f"Production token: {deployed_flow.production_token}")
triggered_run = deployed_flow.trigger()

print("Run started.")
print(f"Run: {triggered_run.run}")

time.sleep(10)
limit = datetime.now() + timedelta(seconds=30)
while triggered_run.run is None:
    print("Run not found yet, waiting...")
    time.sleep(5)
    if datetime.now() > limit:
        print("Timed out looking for run after 1.5 min.")
        break
