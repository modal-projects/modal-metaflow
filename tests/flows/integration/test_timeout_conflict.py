import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step, timeout
from utils import aws_env

EXPECTED_ERROR = ["Choose @timeout or @modal(timeout=...), not both."]


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_timeout_conflict)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        cpu=0.125,
        memory=128,
        timeout=60,
    )
    @environment(vars=aws_env())
    @timeout(seconds=60)
    @step
    def test_timeout_conflict(self):
        self.next(self.end)

    @step
    def end(self):
        raise AssertionError("Expected the flow to fail before reaching end")


if __name__ == "__main__":
    TestFlow()
