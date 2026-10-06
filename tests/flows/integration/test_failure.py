import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env

EXPECTED_ERROR = ["EXPECTED_USER_FAILURE"]


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_failure)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"), cpu=0.125, memory=128
    )
    @environment(vars=aws_env())
    @step
    def test_failure(self):
        raise RuntimeError("EXPECTED_USER_FAILURE")
        self.next(self.end)  # Metaflow requires a static graph edge.

    @step
    def end(self):
        raise AssertionError("Expected the flow to fail before reaching end")


if __name__ == "__main__":
    TestFlow()
