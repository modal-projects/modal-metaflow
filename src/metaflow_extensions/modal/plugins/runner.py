def metaflow_entry(step_cli: str, env_vars: dict, oidc_role_arn: str | None = None):
    import os
    import subprocess
    import sys
    from tempfile import TemporaryDirectory

    import modal

    from metaflow_extensions.modal.plugins.cluster import ModalCluster

    env_vars = dict(env_vars)
    try:
        context = modal.Cluster.from_context()
    except modal.exception.InvalidError:  # not a multinode step
        cluster = None
    else:
        # the step subprocess can't see modal's cluster context, so pass it via env
        cluster = ModalCluster(
            cluster_id=context.object_id,
            node_rank=context.container_rank(),
            node_ips=tuple(context.container_ips()),
        )
        env_vars["METAFLOW_MODAL_CLUSTER"] = cluster.to_env()
        if cluster.node_rank:
            # followers get their own task id so their logs and metadata don't overwrite rank 0's.
            env_vars["METAFLOW_MODAL_TASK_ID"] += f"-node-{cluster.node_rank}"

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

    os.environ.update(env_vars)
    with TemporaryDirectory(prefix="metaflow-modal-") as directory:
        completed = subprocess.run(
            step_cli,
            check=False,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            errors="replace",
            cwd=directory,
        )
    print(completed.stdout)
    print(completed.stderr, file=sys.stderr)
    if cluster and cluster.node_rank:
        cluster.report(completed.returncode, completed.stderr)
    if completed.returncode:
        raise subprocess.CalledProcessError(completed.returncode, "Metaflow step")
    return completed.returncode, completed.stdout, completed.stderr
