import os

import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env

VALUE = "space 'quote' $dollar\nsecond line"


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_environment)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"), cpu=0.125, memory=128
    )
    @environment(vars=aws_env(TEST_VALUE=VALUE))
    @step
    def test_environment(self):
        self.result = os.environ["TEST_VALUE"]
        self.next(self.end)

    @step
    def end(self):
        assert self.result == VALUE, self.result
        print("PASS: test_environment")


if __name__ == "__main__":
    TestFlow()
