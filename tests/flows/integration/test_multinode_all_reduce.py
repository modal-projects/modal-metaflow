import subprocess
import sys
from pathlib import Path

import modal as modal_sdk
from metaflow import FlowSpec, current, environment, modal, step
from utils import aws_env

EXPECTED_OUTPUT = ["ALL_REDUCE_NODE=0"]
WORKER = "/root/all_reduce_worker.py"
IMAGE = (
    modal_sdk.Image.from_registry("python:3.11-slim")
    .pip_install("torch", "numpy")
    .add_local_file(Path(__file__).with_name("all_reduce_worker.py"), WORKER)
)


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_multinode_all_reduce)

    @modal(
        image=IMAGE,
        gpu=["H100:8", "H200:8", "B200:8", "B300:8"],
        multicluster_size=2,
        timeout=180,
    )
    @environment(vars=aws_env())
    @step
    def test_multinode_all_reduce(self):
        cluster = current.modal_cluster
        assert cluster.world_size == 2
        subprocess.run(
            [
                sys.executable,
                "-m", "torch.distributed.run",
                "--nnodes=2",
                "--nproc-per-node=8",
                f"--node-rank={cluster.node_rank}",
                f"--master-addr={cluster.node_ips[0]}",
                "--master-port=29500",
                WORKER,
            ],
            check=True,
        )
        print(f"ALL_REDUCE_NODE={cluster.node_rank}", flush=True)
        self.next(self.end)

    @step
    def end(self):
        print("PASS: test_multinode_all_reduce")


if __name__ == "__main__":
    TestFlow()
