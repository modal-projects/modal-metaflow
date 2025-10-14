from typing import Optional
import asyncio
from metaflow import Runner, Deployer, Run, namespace()
import pytest


async def run_flow(flow: str, timeout: int, run_kwargs: Optional[dict] = None) -> Run:
    run_kwargs = run_kwargs or {}
    with await Runner(flow).async_run(**run_kwargs) as running:
        await running.wait(timeout, stream="stdout")
        assert running.status == "successful"
        pathspec = running.run.pathspec
    namespace(None)
    return Run(pathspec)


@pytest.mark.timeout(300)
def test_hello_world_runner(flows_path):
    import os

    print("XXX", os.getenv("METAFLOW_HOME"))
    print("XXX", os.getenv("METAFLOW_PROFILE"))
    hello_world = str(flows_path / "hello_world.py")

    run = asyncio.run(run_flow(hello_world, 300, run_kwargs={"my_value": 4}))
    assert run.finished
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

    run = asyncio.run(run_flow(fan_out, 600))

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
