import os
import modal as modal_sdk

from metaflow import FlowSpec, step, modal, metadata, kubernetes, project

metadata(f"service@{os.environ['METAFLOW_SERVICE_URL']}")


image = modal_sdk.Image.debian_slim(python_version="3.11")
aws_secret = modal_sdk.Secret.from_local_environ(
    env_keys=["AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"]
)


# Example Metaflow + Modal integration
@project(name="chkpt_lora")
class ExampleFlow(FlowSpec):
    """
    Example flow demonstrating Modal integration.
    """

    @modal(
        secrets=[aws_secret],
        image=image,
    )
    @step
    def start(self):
        self.data = list(range(1, 6))
        self.status = "started"
        print(f"Flow beginning with status: {self.status}")
        self.next(self.process)

    @modal(
        secrets=[aws_secret],
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
