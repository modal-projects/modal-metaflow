import pathlib

import modal as modal_sdk
from gpu_profile import gpu_profile
from metaflow import (
    FlowSpec,
    namespace,
    card,
    checkpoint,
    current,
    huggingface_hub,
    metadata,
    modal,
    model,
    project,
    retry,
    step,
)
from mixins import N_GPU, HuggingFaceLora

metadata(
    "service@https://modal-labs-jason-dev--metaflow-metadata-service-metaflow-2fbf76.modal.run"
)

req_path = pathlib.Path(__file__).parent / "requirements.txt"
# req_path = "requirements.txt"

# import subprocess
# import sys


# def get_requirements():
#     requirements = subprocess.run(
#         [sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True
#     )
#     return requirements.stdout.strip().split("\n")


# requirements = get_requirements()
# print(requirements)

hf_image = (
    # NOTE: this image does _not_ work on H100s. the bitsandbytes version shipped
    # here does not have cublas kernels for Hopper
    modal_sdk.Image.from_registry(
        "valayob/hf-transformer-gpu:4.39.3.1", add_python="3.11"
    )
    # .uv_pip_install(*requirements, force_build=True)
    .uv_pip_install(requirements=[str(req_path.absolute())])
    .entrypoint([])
)

aws_secret = modal_sdk.Secret.from_local_environ(
    env_keys=["AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN"]
)


@project(name="chkpt_lora")
class LlamaInstructionTuning(FlowSpec, HuggingFaceLora):
    @card
    @huggingface_hub
    @modal(
        image=hf_image,
        secrets=[aws_secret],
    )
    @step
    def start(self):
        base_model = self.config.model.base_model
        # `current.huggingface_hub.snapshot_download` downloads the model from the Hugging Face Hub
        # and saves it in the backend storage based on the model's `repo_id`. If there exists a model
        # with the same `repo_id` in the backend storage, it will not download the model again. The return
        # value of the function is a reference to the model in the backend storage.
        # This reference can be used to load the model in the subsequent steps via `@model(load=["hf_model_checkpoint"])`
        self.hf_model_checkpoint = current.huggingface_hub.snapshot_download(
            repo_id=base_model,
            ignore_patterns=[
                "*.bin",
            ],
        )
        current.card.extend(self.config_report())
        self.next(self.finetune)

    # @pypi(disabled=True)
    @card(customize=True)
    @gpu_profile(interval=1)
    @model(load=["hf_model_checkpoint"])
    @checkpoint
    @modal(
        image=hf_image,
        gpu=f"A100:{N_GPU}",
        cpu=14,
        memory=72000,
        secrets=[aws_secret],
    )
    @retry(times=3)
    @step
    def finetune(self):
        self.config.model.model_save_directory = current.checkpoint.directory

        self.config.model.resuming_checkpoint_path = None
        if current.checkpoint.is_loaded:
            # Checkpoints Saved via the `MetaflowCheckpointCallback`
            # will be automatically loaded on retries so we just need to pass the
            # underlying function the path where the checkpoint was loaded from.
            self.config.model.resuming_checkpoint_path = current.checkpoint.directory

        self.run(
            base_model_path=current.model.loaded["hf_model_checkpoint"],
            dataset_path=None,
        )

        current.card.extend(self.config_report())
        self.next(self.end)

    @step
    def end(self):
        print("Completed!")


if __name__ == "__main__":
    LlamaInstructionTuning()
