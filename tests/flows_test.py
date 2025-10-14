from time import sleep
from metaflow import Runner, Deployer
import pytest


def _wait_for_run(running):
    while running.status == "running":
        sleep(1)


@pytest.mark.timeout(300)
def test_hello_world_runner(flows_path):
    hello_world = str(flows_path / "hello_world.py")

    with Runner(hello_world).run(my_value=4) as running:
        _wait_for_run(running)
        run = running.run

    assert run.successful
    assert run.data.custom_value == 9


@pytest.mark.timeout(600)
def test_argo_hello_world_workflow_trigger(flows_path):
    hello_world = str(flows_path / "hello_world.py")

    deployed_flow = Deployer(hello_world).argo_workflows().create()
    triggered_run = deployed_flow.trigger()

    triggered_run.wait_for_completion()
    run = triggered_run.wait_for_run(timeout=600)

    assert run.successful
    assert run.data.custom_value == 10


@pytest.mark.timeout(600)
def test_fanout(flows_path):
    fan_out = str(flows_path / "fanout.py")

    with Runner(fan_out).run() as running:
        _wait_for_run(running)
        run = running.run

    assert run.successful
    expected_outs = [
        "Stranger Things processed",
        "House of Cards processed",
        "Narcos processed",
    ]
    for expected in expected_outs:
        assert expected in run.data.results


@pytest.mark.timeout(600)
@pytest.mark.skip(reason="foreach does not work with argo")
def test_argo_fanout_workflow_trigger(flows_path):
    fan_out = str(flows_path / "fanout.py")

    deployed_flow = Deployer(fan_out).argo_workflows().create()
    triggered_run = deployed_flow.trigger()

    triggered_run.wait_for_completion()
    run = triggered_run.wait_for_run(timeout=60)

    assert run.successful
    expected_outs = [
        "Stranger Things processed",
        "House of Cards processed",
        "Narcos processed",
    ]
    for expected in expected_outs:
        assert expected in run.data.results
