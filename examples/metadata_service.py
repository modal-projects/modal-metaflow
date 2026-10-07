import os
import subprocess
import sys
import time

import modal

app = modal.App("metaflow-metadata-service")

MF_METADATA_DB_HOST = os.environ["MF_METADATA_DB_HOST"]
metadata_image = (
    modal.Image.from_registry("netflixoss/metaflow_metadata_service")
    .pip_install("uv")
    .run_commands(
        "uv pip install --system -r ./services/metadata_service/requirements.txt"
    )
    .env(
        {
            "MF_METADATA_DB_USER": "postgres",
            "MF_METADATA_DB_PSWD": "postgres",
            "MF_METADATA_DB_NAME": "metaflow",
        }
    )
    .entrypoint([])
)

pg_image = (
    modal.Image.from_registry("postgres:11", add_python="3.11")
    .env(
        {
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": "postgres",
            "POSTGRES_DB": "metaflow",
        }
    )
    .entrypoint([])
)
pg_vol = modal.Volume.from_name("metaflow-metadata-db", create_if_missing=True)


@app.cls(
    image=pg_image,
    volumes={"/var/lib/postgresql/data2": pg_vol},
    min_containers=1,
    timeout=60 * 60,
)
class MetaflowMetadataDb:
    @modal.enter()
    def setup(self):
        self.db_process = subprocess.Popen(
            ["/usr/local/bin/docker-entrypoint.sh", "-h", "0.0.0.0"],
        )
        # TODO: turn off bare TCP
        self.tunnel_manager = modal.forward(5432, unencrypted=True)
        self.tunnel_handle = self.tunnel_manager.__enter__()

    @modal.method()
    def serve(self):
        pass

    @modal.method()
    def get_tunnel_info(self):
        return self.tunnel_handle.tls_socket, self.tunnel_handle.tcp_socket

    @modal.exit()
    def cleanup(self):
        exc_info = sys.exc_info()
        self.tunnel_manager.__exit__(*exc_info)
        pg_vol.commit()
        self.db_process.terminate()


@app.cls(image=metadata_image, min_containers=1, timeout=60 * 60)
class MetaflowMetadataService:
    @modal.enter()
    def setup(self):
        timeout = 10
        start_time = time.time()
        while time.time() - start_time <= timeout:
            try:
                db_cls = modal.Cls.from_name(
                    app.name or "metaflow-metadata-service", "MetaflowMetadataDb"
                )()
                # TODO: use TLS
                _, tcp_socket = db_cls.get_tunnel_info.remote()
                tunnel_url, tunnel_port = tcp_socket
                break
            except modal.exception.NotFoundError:
                time.sleep(0.05)
                continue
        else:
            raise RuntimeError("Couldn't connect to postgres in time")

        print(f"Socket: {tunnel_url}:{tunnel_port}")
        env = os.environ.update(
            {
                "MF_METADATA_HOST": "0.0.0.0",
                "MF_METADATA_DB_HOST": tunnel_url,
                "MF_METADATA_DB_PORT": str(tunnel_port),
            }
        )
        self.server_process = subprocess.Popen(
            ["python", "-m", "services.metadata_service.server"], env=env
        )
        print("Service process launched.")
        db_init = subprocess.run(["python", "run_goose.py"], check=True)
        if db_init.returncode != 0:
            print("Metadata DB initialization failed!")
        else:
            print("Metadata DB initialized.")

    @modal.web_server(8080, startup_timeout=10 * 60)
    def serve_metadata(self):
        pass

    @modal.exit()
    def cleanup(self):
        self.server_process.terminate()
