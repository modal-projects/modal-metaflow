import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, step
from utils import aws_env


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_modal_retries)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        cpu=0.125,
        memory=128,
        retries=1,
    )
    @environment(vars=aws_env())
    @step
    def test_modal_retries(self):
        from metaflow import current
        from utils import attempt_number

        attempt = attempt_number("modal-retries")
        if attempt == 1:
            raise RuntimeError("EXPECTED_MODAL_RETRY")
        assert attempt == 2, attempt
        assert current.retry_count == 0
        self.next(self.end)

    @step
    def end(self):
        from utils import clear_attempt

        clear_attempt("modal-retries")
        print("PASS: test_modal_retries")


if __name__ == "__main__":
    TestFlow()
