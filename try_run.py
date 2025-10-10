import time
from metaflow import Deployer

flow_path = "tests/flows/hello_world.py"

deployed_flow = Deployer(flow_path).argo_workflows().create()
triggered_run = deployed_flow.trigger()

run_obj = triggered_run.wait_for_run(timeout=60 * 10)
print("Run started", run_obj)

while not run_obj.finished:
    print("Run is not finished yet. Waiting...")
    time.sleep(5)  # Adjust the sleep interval as needed
print(run_obj.successful)
