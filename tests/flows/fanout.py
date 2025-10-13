from metaflow import FlowSpec, step, modal
import modal as modal_sdk

aws_secret = modal_sdk.Secret.from_name("s3-secret-metaflow-test")


class ForeachFlow(FlowSpec):
    @step
    def start(self):
        self.titles = ["Stranger Things", "House of Cards", "Narcos"]
        self.next(self.a, foreach="titles")

    @modal(secrets=[aws_secret])
    @step
    def a(self):
        self.title = "%s processed" % self.input
        self.next(self.join)

    @modal(secrets=[aws_secret])
    @step
    def join(self, inputs):
        self.results = [inp.title for inp in inputs]
        self.next(self.end)

    @step
    def end(self):
        print("\n".join(self.results))


if __name__ == "__main__":
    ForeachFlow()
