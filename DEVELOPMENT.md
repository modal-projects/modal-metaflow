# Development

0. Install [k3d](https://k3d.io/v5.6.3/) and `kubectl`.
1. Create a [service token](https://modal.com/docs/guide/service-users) and make sure `MODAL_METAFLOW_TOKEN_ID` and `MODAL_METAFLOW_TOKEN_SECRET` are set in your environment.
2. Build the modal-metaflow wheel and install locally. This enables metaflow to discover the plugin and upload it to argo.

```bash
rm -rf dist && uv build --wheel && uv pip install $(find dist -name '*.whl')
```

When you add a new dependency with `uv add ...`, you'll need to run the above again.

3. Activate the venv:

```bash
source .venv/bin/activate
```

3. Start Metaflow on Modal and Argo locally:

```bash
inv develop
```

4. Run the `source` command that was printed out to configure your local env.

5. Run a simple flow locally:

```bash
python tests/flows/hello_world.py run
```

6. Run a create and trigger a workflow on argo:

```bash
python tests/flows/hello_world.py argo-workflows create
python tests/flows/hello_world.py argo-workflows trigger
```

7. To teardown the metaflow services and argo:

```bash
uv run inv teardown
```

## Only running locally

If you are only testing local execution, then you do not need argo. To just setup the
Metaflow on Modal sandboxes:

```bash
inv start-metaflow
```

## Running pytest

For local testing, it's best to keep the Modal sandboxes up for faster iteration with `--keep-alive`:

```bash
pytest tests/basic_flow_test.py --keep-alive
```
