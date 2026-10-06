import ast
import json
import os
import signal
import subprocess
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path

import modal
import pytest

FLOWS = Path(__file__).parent / "flows" / "integration"


def expectations(path):
    values = {"COMMAND": ["run"], "EXPECTED_ERROR": [], "EXPECTED_OUTPUT": []}
    for node in ast.parse(path.read_text()).body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in values:
                values[target.id] = ast.literal_eval(node.value)
    return values


@pytest.fixture
def flow_env(mf_service, metaflow_home_path):
    config = json.loads((metaflow_home_path / "config_modal.json").read_text())
    return dict(
        os.environ,
        **{key: str(value) for key, value in config.items()},
        AWS_ACCESS_KEY_ID=mf_service.minio.key,
        AWS_SECRET_ACCESS_KEY=mf_service.minio.secret,
        AWS_SESSION_TOKEN="",
        AWS_DEFAULT_REGION="us-east-1",
        AWS_ENDPOINT_URL=mf_service.minio.endpoint,
        PYTHONUNBUFFERED="1",
    )


@pytest.mark.parametrize("flow", sorted(FLOWS.glob("test_*.py")), ids=lambda p: p.stem)
def test_flow(flow, flow_env, tmp_path):
    expected = expectations(flow)
    env = flow_env.copy()
    with ExitStack() as cleanup:
        if flow.stem == "test_volume":
            env["TEST_VOLUME_NAME"] = f"metaflow-test-{uuid.uuid4().hex}"
            cleanup.callback(
                modal.Volume.objects.delete,
                env["TEST_VOLUME_NAME"],
                allow_missing=True,
            )
        process = subprocess.Popen(
            [sys.executable, flow.name, *expected["COMMAND"]],
            cwd=FLOWS,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        timed_out = False
        try:
            output, _ = process.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()

    errors = expected["EXPECTED_ERROR"]
    required = errors or [f"PASS: {flow.stem}", *expected["EXPECTED_OUTPUT"]]
    missing = [text for text in required if text not in output]
    correct_exit = process.returncode != 0 if errors else process.returncode == 0
    for key, value in env.items():
        if value and (key.startswith("AWS_") or "TOKEN" in key or "SECRET" in key):
            output = output.replace(value, "<redacted>")
    log = tmp_path / f"{flow.stem}.log"
    log.write_text(output)
    detail = f"Full log: {log}\n" + "\n".join(output.splitlines()[-60:])
    assert not timed_out, f"Test exceeded 300 seconds\n{detail}"
    assert correct_exit and not missing, (
        f"exit={process.returncode}, missing={missing}\n{detail}"
    )
