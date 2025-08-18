import modal as modal_sdk
from metaflow import FlowSpec, step, modal


image = modal_sdk.Image.debian_slim(python_version="3.11").uv_pip_install("semver")
aws_secret = modal_sdk.Secret.from_dotenv("/tmp/", filename="aws_sso.env")


class JoinFlow(FlowSpec):
    @step
    def start(self):
        self.data = list(range(1, 6))
        self.status = "started"
        print(f"Flow beginning with status: {self.status}")
        self.next(self.process, self.process_again)

    @modal(secrets=[aws_secret], image=image)
    @step
    def process(self):
        # Your data processing logic here
        self.processed_data = [x * 2 for x in self.data]
        self._update_status("data-processed-1")
        self.next(self.join)

    @modal(secrets=[aws_secret], image=image)
    @step
    def process_again(self):
        """
        This step runs on Modal with specified resources.
        """
        # Your data processing logic here
        self.processed_data = [x * 3 for x in self.data]
        self._update_status("data-processed-2")
        self.next(self.join)

    @modal(secrets=[aws_secret], image=image)
    @step
    def join(self, inputs):
        first = inputs.process.processed_data
        second = inputs.process_again.processed_data
        self.vecsum = [a + b for a, b in zip(first, second)]

        # Fuse ambiguous status input for the join
        self.status = f"{inputs.process.status},{inputs.process_again.status}"
        self._update_status("joined")
        self.next(self.end)

    @step
    def end(self):
        """
        Final step runs locally.
        """
        print(f"Processed data: {self.vecsum}")
        self._update_status("complete")

    def _update_status(self, new_status):
        initial_status = self.status
        self.status = new_status
        print(f"Flow moved from status '{initial_status}' to '{self.status}'")


if __name__ == "__main__":
    JoinFlow()
