# Modal Metaflow - Examples

## Flow examples
- `examples/basic.py`: Basic execution example (single @modal step)
- `examples/join.py`: Branching execution example (multiple @modal steps + join)
- `examples/foreach.py`: (TODO) Iterative execution example (iterative @modal steps + foreach + join)
- `examples/outerbounds_lora.py`: Multi-GPU LoRA finetuning

## Infra
- `examples/metadata_service.py`: A Modal App that serves the Metaflow metadata service and metadata DB.
- `examples/deploy.py`: An Argo Workflows deployment of the `basic.py` example.