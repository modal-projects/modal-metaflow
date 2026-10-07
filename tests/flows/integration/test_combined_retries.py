import modal as modal_sdk
from metaflow import FlowSpec, environment, modal, retry, step
from utils import aws_env


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_combined_retries)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"),
        cpu=0.125,
        memory=128,
        retries=1,
    )
    @environment(vars=aws_env())
    @retry(times=1, minutes_between_retries=0)
    @step
    def test_combined_retries(self):
        from metaflow import current
        from utils import attempt_number

        attempt = attempt_number("combined-retries")
        if attempt < 4:
            raise RuntimeError("EXPECTED_COMBINED_RETRY")
        assert attempt == 4, attempt
        assert current.retry_count == 1, current.retry_count
        self.next(self.end)

    @step
    def end(self):
        from utils import clear_attempt

        clear_attempt("combined-retries")
        print("PASS: test_combined_retries")


if __name__ == "__main__":
    TestFlow()
