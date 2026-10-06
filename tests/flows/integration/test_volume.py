import os
from pathlib import Path

import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env

VOLUME = modal_sdk.Volume.from_name(
    os.environ["TEST_VOLUME_NAME"], create_if_missing=True
)


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.write)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        cpu=0.125,
        memory=128,
        volumes={"/data": VOLUME},
    )
    @environment(vars=aws_env(TEST_VOLUME_NAME=os.environ["TEST_VOLUME_NAME"]))
    @step
    def write(self):
        Path("/data/result.txt").write_text("volume-persisted")
        VOLUME.commit()
        self.next(self.test_volume)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        cpu=0.125,
        memory=128,
        volumes={"/data": VOLUME},
    )
    @environment(vars=aws_env(TEST_VOLUME_NAME=os.environ["TEST_VOLUME_NAME"]))
    @step
    def test_volume(self):
        self.result = Path("/data/result.txt").read_text()
        self.next(self.end)

    @step
    def end(self):
        assert self.result == "volume-persisted", self.result
        print("PASS: test_volume")


if __name__ == "__main__":
    TestFlow()
