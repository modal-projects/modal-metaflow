from metaflow import Runner


def test_hello_world_runner(flows_path):
    hello_world = str(flows_path / "hello_world.py")

    with Runner(hello_world).run() as running:
        status = running.status
    assert status == "successful"
