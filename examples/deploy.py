import time
from metaflow import Deployer

deployed_flow = Deployer('examples/basic.py', datastore="s3", datastore_root="s3://metaflow-modal").argo_workflows().create()
print('Production token', deployed_flow.production_token)
triggered_run = deployed_flow.trigger()
while triggered_run.run is None:
    print('Waiting for the run to start')
    time.sleep(1)

print('Run started', triggered_run.run)

print('Terminating the flow', triggered_run.terminate())
