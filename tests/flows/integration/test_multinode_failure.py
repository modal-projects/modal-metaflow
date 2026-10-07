import modal as modal_sdk
from metaflow import FlowSpec, current, environment, modal, step
from utils import aws_env

EXPECTED_ERROR = ["Modal cluster node 1 failed", "follower failed on purpose"]


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_multinode_failure)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        gpu=["H100:8", "H200:8", "B200:8", "B300:8"],
        multicluster_size=2,
        timeout=120,
    )
    @environment(vars=aws_env())
    @step
    def test_multinode_failure(self):
        if current.modal_cluster.node_rank == 1:
            raise RuntimeError("follower failed on purpose")
        self.next(self.end)

    @step
    def end(self):
        pass


if __name__ == "__main__":
    TestFlow()
