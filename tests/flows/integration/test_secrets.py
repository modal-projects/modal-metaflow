import os

import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_secrets)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        cpu=0.125,
        memory=128,
        secrets=[modal_sdk.Secret.from_dict({"TEST_SECRET": "dummy-secret"})],
    )
    @environment(vars=aws_env())
    @step
    def test_secrets(self):
        self.result = os.environ["TEST_SECRET"]
        self.next(self.end)

    @step
    def end(self):
        assert self.result == "dummy-secret", self.result
        print("PASS: test_secrets")


if __name__ == "__main__":
    TestFlow()
