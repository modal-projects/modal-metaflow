# Development

0. Install [k3d](https://k3d.io/v5.6.3/) and `kubectl`.
1. Create a [service token](https://modal.com/docs/guide/service-users) and make sure `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are set in your environment.
2. Start Metaflow on Modal and Argo locally:

```bash
uv run inv develop
```

3. Run the `source` command that was printed out to configure your local env.

4. Run a simple flow locally:


```bash
uv run tests/flows/hello_world.py run
```

5. Run a create and trigger a workflow on argo:

```bash
uv run tests/flows/hello_world.py argo-workflows create
uv run tests/flows/hello_world.py argo-workflows trigger
```

6. To teardown the metaflow services and argo:

```bash
uv run inv teardown
```

## Only running locally

If you are only testing local execution, then you do not need argo. To just setup the
Metaflow on Modal sandboxes:

```bash
uv run inv start-metaflow
```
