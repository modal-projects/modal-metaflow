from metaflow import FlowSpec, step, modal, Parameter
import modal as modal_sdk

aws_secret = modal_sdk.Secret.from_name("s3-secret-metaflow-test")


class HelloFlow(FlowSpec):
    my_value = Parameter("my_value", default=5)

    @step
    def start(self):
        print("HelloFlow is starting")
        self.custom_value = self.my_value + 2
        self.next(self.hello)

    @modal(secrets=[aws_secret])
    @step
    def hello(self):
        print("Metaflow says: Hi!")
        self.custom_value += 3
        self.next(self.end)

    @step
    def end(self):
        print("HelloFlow is all done.")


if __name__ == "__main__":
    HelloFlow()
