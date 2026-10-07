import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step, timeout
from utils import aws_env

EXPECTED_ERROR = ["TIMEOUT_BODY_ENTERED", "hit its timeout of 30s"]


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_timeout_decorator)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"), cpu=0.125, memory=128
    )
    @environment(vars=aws_env())
    @timeout(seconds=30)
    @step
    def test_timeout_decorator(self):
        import time

        print("TIMEOUT_BODY_ENTERED", flush=True)
        time.sleep(180)
        self.next(self.end)

    @step
    def end(self):
        raise AssertionError("Expected the flow to fail before reaching end")


if __name__ == "__main__":
    TestFlow()
