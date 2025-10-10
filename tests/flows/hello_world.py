from metaflow import FlowSpec, step, modal
import modal as modal_sdk

aws_secret = modal_sdk.Secret.from_name("s3-secret-metaflow-test")


class HelloFlow(FlowSpec):
    @step
    def start(self):
        print("HelloFlow is starting")
        self.next(self.hello)

    @modal(secrets=[aws_secret])
    @step
    def hello(self):
        print("Metaflow says: Hi!")
        self.next(self.end)

    @step
    def end(self):
        print("HelloFlow is all done.")


if __name__ == "__main__":
    HelloFlow()
