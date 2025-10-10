import time
from metaflow import Runner, Deployer
import pytest


@pytest.mark.timeout(300)
def test_hello_world_runner(flows_path):
    hello_world = str(flows_path / "hello_world.py")

    with Runner(hello_world).run() as running:
        status = running.status
    assert status == "successful"


@pytest.mark.timeout(300)
def test_argo_workflow_trigger(flows_path):
    hello_world = str(flows_path / "hello_world.py")

    deployed_flow = Deployer(hello_world).argo_workflows().create()
    triggered_run = deployed_flow.trigger()

    run_obj = triggered_run.wait_for_run(timeout=60 * 10)

    while not run_obj.finished:
        time.sleep(5)
    assert run_obj.successful
