import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env

EXPECTED_OUTPUT = ["STDOUT_SENTINEL_yyz", "STDERR_SENTINEL_lga"]


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_logs)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"), cpu=0.125, memory=128
    )
    @environment(vars=aws_env())
    @step
    def test_logs(self):
        import sys

        print("STDOUT_SENTINEL_yyz", flush=True)
        print("STDERR_SENTINEL_lga", file=sys.stderr, flush=True)
        self.next(self.end)

    @step
    def end(self):
        print("PASS: test_logs")


if __name__ == "__main__":
    TestFlow()
