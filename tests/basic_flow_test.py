from metaflow import Runner, Deployer
import pytest


@pytest.mark.timeout(300)
def test_hello_world_runner(flows_path):
    hello_world = str(flows_path / "hello_world.py")

    with Runner(hello_world).run(my_value=4) as running:
        run = running.run

    assert run.successful
    assert run.data.custom_value == 9


@pytest.mark.timeout(600)
def test_argo_workflow_trigger(flows_path):
    hello_world = str(flows_path / "hello_world.py")

    deployed_flow = Deployer(hello_world).argo_workflows().create()
    triggered_run = deployed_flow.trigger()

    triggered_run.wait_for_completion(timeout=200)
    run = triggered_run.wait_for_run(timeout=200)

    assert run.successful
    assert run.data.custom_value == 10
