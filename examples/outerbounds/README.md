# Outerbounds Huggingface PEFT Example

This example requires a running Metaflow metadata service. You can deploy one on Modal by running the following from the project root:
```bash
modal deploy examples/metadata_service.py
```

This will print out a Modal endpoint for the metadata service, you can configure the metadata service URL inline in the `lora.py` script or with an environment variable override:
```bash
METAFLOW_DEFAULT_METADATA=service METAFLOW_SERVICE_URL=<metadata-service-url> METAFLOW_DATASTORE_SYSROOT_S3=s3://metaflow-modal python lora.py --datastore s3 run --config-file experiment_config.yml
```
