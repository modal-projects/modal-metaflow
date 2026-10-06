import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_apt_install)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim").apt_install("jq"),
        cpu=0.125,
        memory=128,
    )
    @environment(vars=aws_env())
    @step
    def test_apt_install(self):
        import subprocess

        self.result = subprocess.check_output(["jq", "--version"], text=True).strip()
        self.next(self.end)

    @step
    def end(self):
        assert self.result.startswith("jq-"), self.result
        print("PASS: test_apt_install")


if __name__ == "__main__":
    TestFlow()
