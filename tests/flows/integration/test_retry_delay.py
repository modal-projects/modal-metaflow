import time

import modal as modal_sdk
from metaflow import FlowSpec, current, environment, modal, retry, step
from utils import attempt_store, aws_env, clear_attempt

EXPECTED_OUTPUT = ["EXPECTED_DELAYED_RETRY"]


class TestFlow(FlowSpec):
    @step
    def start(self):
        self.next(self.test_retry_delay)

    @modal(
        image=modal_sdk.Image.from_registry("python:3.11-slim"), cpu=0.125, memory=128
    )
    @environment(vars=aws_env())
    @retry(times=1, minutes_between_retries=1)
    @step
    def test_retry_delay(self):
        s3, bucket, key = attempt_store("retry-delay")
        if current.retry_count == 0:
            s3.put_object(Bucket=bucket, Key=key, Body=str(time.time()).encode())
            raise RuntimeError("EXPECTED_DELAYED_RETRY")
        assert current.retry_count == 1, current.retry_count
        failed_at = float(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        self.delay = time.time() - failed_at
        self.next(self.end)

    @step
    def end(self):
        assert self.delay >= 60, self.delay
        clear_attempt("retry-delay")
        print("PASS: test_retry_delay")


if __name__ == "__main__":
    TestFlow()
