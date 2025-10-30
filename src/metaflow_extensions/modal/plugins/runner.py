from typing import Optional


def metaflow_entry(step_cli: str, env_vars: dict, oidc_role_arn: Optional[str] = None):
    import os
    import subprocess
    import sys

    if oidc_role_arn is not None:
        import boto3

        # Use boto3 to assume oidc role, then put the creds in the subprocess
        sts_client = boto3.client("sts")

        # Assume role with Web Identity
        credential_response = sts_client.assume_role_with_web_identity(
            RoleArn=oidc_role_arn,
            RoleSessionName="OIDCSession",
            WebIdentityToken=os.environ["MODAL_IDENTITY_TOKEN"],
        )

        # Extract credentials, and add them to the subprocess env
        credentials = credential_response["Credentials"]
        env_vars["AWS_ACCESS_KEY_ID"] = credentials["AccessKeyId"]
        env_vars["AWS_SECRET_ACCESS_KEY"] = credentials["SecretAccessKey"]
        env_vars["AWS_SESSION_TOKEN"] = credentials["SessionToken"]

    print(f"[Modal Worker Debug] FULL step_cli: {step_cli}")

    # Debug: Log what metadata configuration Modal worker receives
    print("[Modal Worker Debug] Environment variables received:")
    print(
        f"[Modal Worker Debug] METAFLOW_DEFAULT_METADATA: {env_vars.get('METAFLOW_DEFAULT_METADATA')}"
    )
    print(
        f"[Modal Worker Debug] METAFLOW_SERVICE_URL: {env_vars.get('METAFLOW_SERVICE_URL')}"
    )
    print(f"[Modal Worker Debug] METAFLOW_RUN_ID: {env_vars.get('METAFLOW_RUN_ID')}")

    try:
        # Merge environment variables
        if env_vars:
            os.environ.update(env_vars)

        # Debug: Verify environment variables are set
        print("[Modal Worker Debug] After setting env vars:")
        print(
            f"[Modal Worker Debug] os.environ METAFLOW_DEFAULT_METADATA: {os.environ.get('METAFLOW_DEFAULT_METADATA')}"
        )
        print(
            f"[Modal Worker Debug] os.environ METAFLOW_SERVICE_URL: {os.environ.get('METAFLOW_SERVICE_URL')}"
        )

        completed = subprocess.run(
            step_cli,
            shell=True,
            executable="/bin/bash",  # Force bash for bash-specific syntax
            capture_output=True,
        )
        stdout = completed.stdout.decode(errors="ignore") if completed.stdout else ""
        stderr = completed.stderr.decode(errors="ignore") if completed.stderr else ""
        print(stdout)
        print(stderr, file=sys.stderr)
        return completed.returncode, stdout, stderr
    except Exception:
        import traceback as _tb

        err = _tb.format_exc()
        print(err, file=sys.stderr)
        return 1, "", err
