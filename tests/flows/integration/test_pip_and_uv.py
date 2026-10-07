import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_pip_and_uv)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim")
        .pip_install("humanize==4.12.3")
        .uv_pip_install("boltons==24.0.0"),
        cpu=0.125,
        memory=128,
    )
    @environment(vars=aws_env())
    @step
    def test_pip_and_uv(self):
        from importlib.metadata import version

        assert version("humanize") == "4.12.3"
        assert version("boltons") == "24.0.0"
        self.next(self.end)

    @step
    def end(self):
        print("PASS: test_pip_and_uv")


if __name__ == "__main__":
    TestFlow()
