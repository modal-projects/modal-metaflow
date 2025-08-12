import modal as modal_sdk
from metaflow import FlowSpec, step, modal


# TODO: handle modal_metaflow deps internally
image = modal_sdk.Image.debian_slim(python_version="3.11").uv_pip_install("semver")


# Example Metaflow + Modal integration
class ExampleFlow(FlowSpec):
    """
    Example flow demonstrating Modal integration.
    """

    @step
    def start(self):
        self.data = list(range(1, 6))
        self.status = "started"
        print(f"Flow beginning with status: {self.status}")
        self.next(self.process)

    # TODO: OIDC
    @modal(
        secrets=[modal_sdk.Secret.from_dotenv("/tmp/", filename="aws_sso.env")],
        image=image,
    )
    @step
    def process(self):
        """
        This step runs on Modal with specified resources.
        """
        # Your data processing logic here
        self.processed_data = [x * 2 for x in self.data]
        self.status = "data-processed"
        print(f"Flow in progress with status: {self.status}")
        self.next(self.end)

    @step
    def end(self):
        """
        Final step runs locally.
        """
        print(f"Processed data: {self.processed_data}")
        self.status = "complete"
        print(f"Flow completed with status: {self.status}")


if __name__ == "__main__":
    ExampleFlow()
