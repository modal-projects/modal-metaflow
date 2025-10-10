from subprocess import run
import shutil
import json
from textwrap import dedent
from pathlib import Path
from typing import Optional
import urllib.request
import psycopg
from contextlib import suppress
import modal
from typing import NamedTuple
import time

K8S_NAMESPACE = "argo"
ARGO_WORKFLOWS_VERSION = "v3.7.2"
CLUSTER_NAME = "metaflow-argo"
S3_SECRET_NAME = "s3-credentials"
MODAL_SECRET_NAME = "modal-argo-creds"
DEFAULT_TIMEOUT = 60 * 60 * 6
DEFAULT_APP_NAME = "metaflow-test"
DEFAULT_INCLUDE_UI = True

# TODO: This should be configurable
METAFLOW_DEFAULT_CONTAINER_IMAGE = "ghcr.io/thomasjpfan/modal-client:0.0.3"


class Minio(NamedTuple):
    sandbox: modal.Sandbox
    endpoint: str
    console: str
    key: str
    secret: str
    bucket: str


class PostgreSQL(NamedTuple):
    sandbox: modal.Sandbox
    host: str
    port: int
    user: str
    password: str
    db_name: str


class MetadataService(NamedTuple):
    sandbox: modal.Sandbox
    url: str


class MetaflowUI(NamedTuple):
    sandbox: modal.Sandbox
    url: str


class MetaflowService(NamedTuple):
    minio: Minio
    psql: PostgreSQL
    metadata_service: MetadataService
    ui: Optional[MetaflowUI]


def is_minio_alive(url: str, key: str, secret: str) -> bool:
    import boto3

    try:
        s3_client = boto3.client(
            "s3",
            endpoint_url=url,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            region_name="us-east-1",
        )
        s3_client.list_buckets(MaxBuckets=1)
        return True
    except Exception:
        return False


def create_bucket_if_not_exists(url: str, key: str, secret: str, bucket: str):
    import boto3

    s3_client = boto3.client(
        "s3",
        endpoint_url=url,
        aws_access_key_id=key,
        aws_secret_access_key=secret,
        region_name="us-east-1",
    )

    with suppress(Exception):
        s3_client.create_bucket(Bucket=bucket)


def create_minio_sandbox(
    app: modal.App,
    app_name: str,
    key: str,
    secret: str,
    timeout: int,
    bucket: str,
) -> Minio:
    minio_image = modal.Image.from_registry(
        "minio/minio:RELEASE.2025-09-07T16-13-09Z"
    ).entrypoint([])

    sandbox_name = "metaflow-minio"
    try:
        minio_sb = modal.Sandbox.create(
            "minio",
            "server",
            "/data",
            "--console-address",
            ":9001",
            app=app,
            image=minio_image,
            encrypted_ports=[9001, 9000],
            timeout=timeout,
            name=sandbox_name,
            secrets=[
                modal.Secret.from_dict(
                    {
                        "MINIO_ROOT_USER": key,
                        "MINIO_ROOT_PASSWORD": secret,
                    }
                )
            ],
        )
    except modal.exception.AlreadyExistsError:
        minio_sb = modal.Sandbox.from_name(app_name=app_name, name=sandbox_name)

    tunnels = minio_sb.tunnels()
    minio = Minio(
        sandbox=minio_sb,
        endpoint=tunnels[9000].url,
        console=tunnels[9001].url,
        key=key,
        secret=secret,
        bucket=bucket,
    )

    # Wait for minio to come up
    for _ in range(30):
        if is_minio_alive(minio.endpoint, minio.key, minio.secret):
            break
        time.sleep(1)
    else:  # no break
        raise RuntimeError("minio failed to start")

    create_bucket_if_not_exists(minio.endpoint, minio.key, minio.secret, minio.bucket)

    print("✅ Minio sandbox started")
    return minio


def is_sql_connection_alive(host, port, db, user, password):
    try:
        psql_uri = (
            f"host={host} port={port} dbname={db} user={user} password={password}"
        )
        with psycopg.connect(psql_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return True
    except Exception:
        return False


def create_psql_sandbox(
    app: modal.App,
    app_name: str,
    user: str,
    password: str,
    db_name: str,
    timeout: int,
):
    psql_image = modal.Image.from_registry("postgres:18.0-bookworm")

    sandbox_name = "metaflow-psql"
    try:
        psql_sb = modal.Sandbox.create(
            "postgres",
            unencrypted_ports=[5432],
            name=sandbox_name,
            app=app,
            image=psql_image,
            timeout=timeout,
            secrets=[
                modal.Secret.from_dict(
                    {
                        "POSTGRES_PASSWORD": password,
                        "POSTGRES_USER": user,
                        "POSTGRES_DB": db_name,
                    }
                )
            ],
        )
    except modal.exception.AlreadyExistsError:
        psql_sb = modal.Sandbox.from_name(app_name=app_name, name=sandbox_name)

    tunnel = psql_sb.tunnels()[5432]
    psql = PostgreSQL(
        sandbox=psql_sb,
        host=tunnel.unencrypted_host,
        port=tunnel.unencrypted_port,
        user=user,
        password=password,
        db_name=db_name,
    )
    # Wait for minio to come up
    for _ in range(30):
        if is_sql_connection_alive(
            host=psql.host,
            port=psql.port,
            db=psql.db_name,
            user=psql.user,
            password=psql.password,
        ):
            break
        time.sleep(1)
    else:  # no break
        raise RuntimeError("psql failed to start")

    print("✅ Postgresql sandbox started")
    return psql


def is_metadata_service_alive(url):
    ping_url = f"{url}/ping"
    try:
        with urllib.request.urlopen(ping_url) as response:
            out = response.read().strip()
        return out == b"pong"
    except Exception:
        return False


def create_metadata_service_sandbox(
    app: modal.App, app_name: str, timeout: int, psql: PostgreSQL
):
    metadata_service_image = modal.Image.from_registry(
        "netflixoss/metaflow_metadata_service:v2.5.0"
    )

    sandbox_name = "metaflow-metadata-service"
    try:
        metadata_service_sb = modal.Sandbox.create(
            "/opt/latest/bin/python3",
            "-m",
            "services.metadata_service.server",
            image=metadata_service_image,
            app=app,
            timeout=timeout,
            encrypted_ports=[8080],
            name=sandbox_name,
            secrets=[
                modal.Secret.from_dict(
                    {
                        "MF_METADATA_DB_NAME": psql.db_name,
                        "MF_METADATA_DB_PORT": str(psql.port),
                        "MF_METADATA_DB_PSWD": psql.password,
                        "MF_METADATA_DB_USER": psql.user,
                        "MF_METADATA_DB_HOST": psql.host,
                    }
                )
            ],
        )

        e = metadata_service_sb.exec(
            "/opt/latest/bin/python3", "/root/run_goose.py", "--only-if-empty-db"
        )
        e.wait()
    except modal.exception.AlreadyExistsError:
        metadata_service_sb = modal.Sandbox.from_name(
            app_name=app_name, name=sandbox_name
        )

    metadata_service = MetadataService(
        sandbox=metadata_service_sb, url=metadata_service_sb.tunnels()[8080].url
    )

    for _ in range(30):
        if is_metadata_service_alive(metadata_service.url):
            break
        time.sleep(1)
    else:  # no break
        raise RuntimeError("Metadata service failed to start")

    print("✅ Metaflow metadata service sandbox started")
    return metadata_service


def create_metaflow_ui_sandbox(
    app: modal.App, app_name: str, timeout: int, minio: Minio, psql: PostgreSQL
) -> MetaflowUI:
    metaflow_ui_static_url = "https://github.com/Netflix/metaflow-ui/releases/download/v1.3.14/metaflow-ui-v1.3.14.zip"
    metaflow_ui_image = modal.Image.from_registry(
        "netflixoss/metaflow_metadata_service:v2.5.0"
    ).run_commands(
        [
            f"curl -L {metaflow_ui_static_url} -o /tmp/metaflow-ui.zip && "
            "unzip -o /tmp/metaflow-ui.zip -d /root/services/ui_backend_service/ui &&"
            "rm /tmp/metaflow-ui.zip"
        ]
    )

    sandbox_name = "metaflow-ui"
    try:
        metaflow_ui_sb = modal.Sandbox.create(
            "/opt/latest/bin/python3",
            "-m",
            "services.ui_backend_service.ui_server",
            image=metaflow_ui_image,
            app=app,
            name=sandbox_name,
            timeout=timeout,
            encrypted_ports=[8083],
            secrets=[
                modal.Secret.from_dict(
                    {
                        "AWS_ACCESS_KEY_ID": minio.key,
                        "AWS_SECRET_ACCESS_KEY": minio.secret,
                        "UI_ENABLED": "1",
                        "MF_DATASTORE_ROOT": f"s3://{minio.bucket}",
                        "METAFLOW_DATASTORE_SYSROOT_S3": f"s3://{minio.bucket}",
                        "METAFLOW_S3_ENDPOINT_URL": minio.endpoint,
                        "LOGLEVEL": "DEBUG",
                        "METAFLOW_DEFAULT_DATASTORE": "s3",
                        "METAFLOW_DEFAULT_METADATA": "service",
                        "MF_METADATA_DB_NAME": psql.db_name,
                        "MF_METADATA_DB_PSWD": psql.password,
                        "MF_METADATA_DB_USER": psql.user,
                        "MF_METADATA_DB_HOST": psql.host,
                        "MF_METADATA_DB_PORT": str(psql.port),
                    }
                )
            ],
        )
    except modal.exception.AlreadyExistsError:
        metaflow_ui_sb = modal.Sandbox.from_name(
            app_name=app_name,
            name=sandbox_name,
        )

    print("✅ Metaflow UI sandbox started")
    return MetaflowUI(sandbox=metaflow_ui_sb, url=metaflow_ui_sb.tunnels()[8083].url)


def construct_env(mf_service: MetaflowService) -> dict:
    mf_config = {
        "METAFLOW_DATASTORE_SYSROOT_S3": f"s3://{mf_service.minio.bucket}",
        "METAFLOW_DEFAULT_DATASTORE": "s3",
        "METAFLOW_DEFAULT_METADATA": "service",
        "METAFLOW_SERVICE_URL": mf_service.metadata_service.url,
        "METAFLOW_SERVICE_INTERNAL_URL": mf_service.metadata_service.url,
        "METAFLOW_ARGO_WORKFLOWS_KUBERNETES_SECRETS": f"{S3_SECRET_NAME},{MODAL_SECRET_NAME}",
        "METAFLOW_KUBERNETES_NAMESPACE": K8S_NAMESPACE,
        "METAFLOW_S3_ENDPOINT_URL": mf_service.minio.endpoint,
        "METAFLOW_DEFAULT_CONTAINER_IMAGE": METAFLOW_DEFAULT_CONTAINER_IMAGE,
    }

    if mf_service.ui:
        mf_config["METAFLOW_UI_URL"] = mf_service.ui.url

    return mf_config


def write_config(mf_home: Path, mf_service: MetaflowService):
    mf_home.mkdir(exist_ok=True, parents=True)
    s3_config_path = mf_home / "aws_config"

    config = dedent(f"""\
        [default]
        aws_access_key_id = {mf_service.minio.key}
        aws_secret_access_key = {mf_service.minio.secret}
        endpoint_url = {mf_service.minio.endpoint}
    """)
    s3_config_path.write_text(config)

    sandbox_ids = [
        mf_service.minio.sandbox.object_id,
        mf_service.psql.sandbox.object_id,
        mf_service.metadata_service.sandbox.object_id,
    ]
    if mf_service.ui:
        sandbox_ids.append(mf_service.ui.sandbox.object_id)

    mf_config = construct_env(mf_service)
    modal_metaflow_path = mf_home / "config_modal.json"
    modal_metaflow_path.write_text(json.dumps(mf_config, indent=4))

    sandbox_ids_path = mf_home / "sandbox_ids.json"
    sandbox_ids_path.write_text(json.dumps(sandbox_ids))

    s3_config_ = str(s3_config_path.absolute())
    mf_home_ = str(mf_home.absolute())

    source_content = dedent(f"""\
    export METAFLOW_HOME="{mf_home_}"
    export METAFLOW_PROFILE="modal"
    export AWS_CONFIG_FILE="{s3_config_}"

    deactivate () {{
        unset METAFLOW_HOME
        unset METAFLOW_PROFILE
        unset AWS_CONFIG_FILE
    }}""")
    source_path = mf_home / "activate"
    source_path.write_text(source_content)

    aws_dot_file = mf_home / "aws_creds.env"
    aws_dot_content = dedent(f"""\
    AWS_ACCESS_KEY_ID={mf_service.minio.key}
    AWS_SECRET_ACCESS_KEY={mf_service.minio.secret}
    AWS_DEFAULT_REGION=us-east-1""")
    aws_dot_file.write_text(aws_dot_content)

    return source_path


def terminate_sandboxes(mf_home: Path):
    sandbox_ids_path = mf_home / "sandbox_ids.json"
    if not sandbox_ids_path.exists():
        raise RuntimeError("No sandbox_ids.json referencing existing sandboxes")

    sandbox_ids = json.loads(sandbox_ids_path.read_text())

    for sandbox_id in sandbox_ids:
        sb = modal.Sandbox.from_id(sandbox_id)
        sb.terminate()

    paths_to_remove = [
        sandbox_ids_path,
        mf_home / "sandbox_ids.json",
        mf_home / "config_modal.json",
        mf_home / "aws_config",
        mf_home / "aws_creds.env",
        mf_home / "activate",
    ]

    for path in paths_to_remove:
        path.unlink(missing_ok=True)


def create_metaflow_sandboxes(
    app_name: str = DEFAULT_APP_NAME,
    timeout: int = DEFAULT_TIMEOUT,
    include_ui: bool = DEFAULT_INCLUDE_UI,
) -> MetaflowService:
    app = modal.App.lookup(app_name, create_if_missing=True)

    user = "metaflow"
    password = "metaflow123"
    bucket = "metaflow-test"

    minio = create_minio_sandbox(
        app, app_name, key=user, secret=password, timeout=timeout, bucket=bucket
    )
    psql = create_psql_sandbox(
        app, app_name, user=user, db_name=user, password=password, timeout=timeout
    )
    metadata_service = create_metadata_service_sandbox(
        app, app_name, timeout=timeout, psql=psql
    )

    if include_ui:
        ui = create_metaflow_ui_sandbox(
            app, app_name, timeout=timeout, minio=minio, psql=psql
        )
    else:
        ui = None

    return MetaflowService(
        minio=minio, psql=psql, metadata_service=metadata_service, ui=ui
    )


def _check_k3d_kube() -> tuple[str, str]:
    k3d = shutil.which("k3d")
    if k3d is None:
        raise RuntimeError("k3d is not installed")

    kubectl = shutil.which("kubectl")

    if kubectl is None:
        raise RuntimeError("kubectl is not installed")
    return k3d, kubectl


def check_cluster_exists(k3d: str, name: str) -> bool:
    list_result = run(
        [k3d, "cluster", "list", "--output", "json"],
        check=True,
        text=True,
        capture_output=True,
    )
    clusters = json.loads(list_result.stdout)
    for cluster in clusters:
        if cluster["name"] == name:
            return True
    return False


def check_namespace_exists(kubectl: str, namespace: str) -> bool:
    namespace_result = run(
        [kubectl, "get", "ns", "--output", "json"],
        check=True,
        text=True,
        capture_output=True,
    )
    namespace_list = json.loads(namespace_result.stdout)
    for namespace_item in namespace_list["items"]:
        if namespace_item["metadata"]["name"] == namespace:
            return True
    return False


def check_if_secret_exists(kubectl: str, secret_name: str, namespace: str) -> bool:
    result = run(
        [kubectl, "-n", namespace, "get", "secret", "--output", "json"],
        check=True,
        text=True,
        capture_output=True,
    )
    result_list = json.loads(result.stdout)
    for item in result_list["items"]:
        if item["metadata"]["name"] == secret_name:
            return True
    return False


def start_argo_kubernetes(mf_home: Path, modal_token_id: str, modal_token_secret: str):
    k3d, kubectl = _check_k3d_kube()

    if not check_cluster_exists(k3d, CLUSTER_NAME):
        run([k3d, "cluster", "create", CLUSTER_NAME], check=True)

    if not check_namespace_exists(kubectl, K8S_NAMESPACE):
        run([kubectl, "create", "namespace", K8S_NAMESPACE], check=True)

    aws_dot_file = str((mf_home / "aws_creds.env").absolute())

    if not check_if_secret_exists(kubectl, S3_SECRET_NAME, K8S_NAMESPACE):
        run(
            [
                kubectl,
                "-n",
                K8S_NAMESPACE,
                "create",
                "secret",
                "generic",
                S3_SECRET_NAME,
                "--from-env-file",
                aws_dot_file,
            ],
            check=True,
        )

    if not check_if_secret_exists(kubectl, MODAL_SECRET_NAME, K8S_NAMESPACE):
        run(
            [
                kubectl,
                "-n",
                K8S_NAMESPACE,
                "create",
                "secret",
                "generic",
                MODAL_SECRET_NAME,
                f"--from-literal=MODAL_TOKEN_ID={modal_token_id}",
                f"--from-literal=MODAL_TOKEN_SECRET={modal_token_secret}",
            ],
            check=True,
        )

    run(
        [
            kubectl,
            "apply",
            "-n",
            "argo",
            "-f",
            "https://github.com/argoproj/argo-workflows/releases/download"
            f"/{ARGO_WORKFLOWS_VERSION}/quick-start-minimal.yaml",
        ],
        check=True,
    )


def stop_argo_kubernetes():
    k3d, _ = _check_k3d_kube()
    run([k3d, "cluster", "delete", "metaflow-argo"])
