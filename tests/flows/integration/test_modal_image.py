import os

import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env

IMAGE = modal_sdk.Image.debian_slim(python_version="3.11").env(
    {"IMAGE_MARKER": "custom-image"}
)


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_modal_image)

    @modal(image=IMAGE, cpu=0.125, memory=128)
    @environment(vars=aws_env())
    @step
    def test_modal_image(self):
        self.result = os.environ["IMAGE_MARKER"]
        self.next(self.end)

    @step
    def end(self):
        assert self.result == "custom-image", self.result
        print("PASS: test_modal_image")


if __name__ == "__main__":
    TestFlow()
