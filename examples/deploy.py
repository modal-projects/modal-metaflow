import time

from metaflow import Deployer

deployed_flow = (
    Deployer(
        "examples/basic.py",
        datastore="s3",
        datastore_root="s3://metaflow-modal",
        environment="pypi",
        env={
            "METAFLOW_KUBERNETES_NAMESPACE": "argo",
            "METAFLOW_ARGO_WORKFLOWS_KUBERNETES_SECRETS": "s3-credentials",
            # "METAFLOW_DATASTORE_SYSROOT_S3": "s3://metaflow-modal",
        },
    )
    .argo_workflows()
    .create()
)
print("Production token", deployed_flow.production_token)
triggered_run = deployed_flow.trigger()

print("Run started", triggered_run.run)

time.sleep(10)
if triggered_run.run is None:
    print("Run not found yet, waiting...")
    time.sleep(50)

print("Terminating the flow after 1m", triggered_run.terminate())
