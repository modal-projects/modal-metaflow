import modal as modal_sdk
from metaflow import FlowSpec, current, environment, modal, step
from utils import aws_env

# rank 0 only finishes after rank 1 reports success, so rank 1 also ran.
EXPECTED_OUTPUT = ["NODE_RANK=0 WORLD_SIZE=2"]


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_multinode)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        gpu=["H100:8", "H200:8", "B200:8", "B300:8"],
        clustered_size=2,
        clustered_rdma=True,
        timeout=120,
    )
    @environment(vars=aws_env())
    @step
    def test_multinode(self):
        rank = current.modal_cluster.node_rank
        world_size = current.modal_cluster.world_size
        assert world_size == 2, world_size
        assert rank in (0, 1), rank
        assert len(set(current.modal_cluster.node_ips)) == world_size
        print(f"NODE_RANK={rank} WORLD_SIZE={world_size}", flush=True)
        self.result_rank = rank
        self.next(self.end)

    @step
    def end(self):
        assert self.result_rank == 0, self.result_rank
        print("PASS: test_multinode")


if __name__ == "__main__":
    TestFlow()
