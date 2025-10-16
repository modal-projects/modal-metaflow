# Modal Metaflow Integration

This is a Metaflow plugin with a single extension, the `@modal` step decorator. This enables Modal to be used as a compute provider for Metaflow steps:

```python
from metaflow import FlowSpec, step, modal

class ModalFlow(FlowSpec):
    @step
    def start(self):
        self.x = 3
        self.next(self.run_on_modal)

    @modal()
    @step
    def run_on_modal(self):
        # this runs on Modal!
        self.y = self.x * 2
        self.next(self.end)

    @step
    def end(self):
        print(f"Modal computed: {self.y}")

```

## Caveats

Currently, we require some S3-permissioned auth in a Modal Secret. This has only been tested with local AWS client credentials, e.g. by doing the following:

```console
$ aws sso login
# --> login in browser
$ aws configure export-credentials --format env-no-export > /tmp/aws_sso.env
```

Then in our Flow, we pass this secret to the `@modal` decorator:

```python
@modal(secrets=[modal.Secret.from_dotenv("/tmp/", filename="aws_sso.env")])
@step
def func(self, ...):
    ...
```

In particular, this hasn't yet been tested with [Modal's OIDC integration](https://modal.com/docs/guide/oidc-integration#demo-usage-with-aws).

# TODOs
- [ ] fix `foreach` support for Argo codepath
- [ ] refactor modal_decorator into local execution vs argo pod execution code paths
- [ ] mflog -- figure out what should be logged across local/argo/modal execution contexts
- [ ] mfconf -- standardize configuration related to modal-metaflow as full metaflow conf instead of simply envvars
- [ ] simple card example - card runs in Modal, output visible in metaflow ui, patched out in argo pod
- [ ] end-to-end testing of OICD with S3 bucket used by flow
- [ ] pypi packaging
- [ ] simple, testable examples of hf_hub, model, checkpoint decorators running in modal
- [x] testing - automated testing of simple/test example flows
- [ ] ~add metaflow ui serving to `metadata_service.py`~ (not planned, `metadata_service.py` superceded by sandbox dev env)
