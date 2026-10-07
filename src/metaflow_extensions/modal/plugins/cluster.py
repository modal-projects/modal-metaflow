import json
import os
import socket
import time
from dataclasses import asdict, dataclass

# modal returns rank 0's result even when a follower fails, so followers send
# their exit status to rank 0 on this port and rank 0 fails if any of them did.
PORT = 29501


@dataclass(frozen=True)
class ModalCluster:
    cluster_id: str
    node_rank: int
    node_ips: tuple[str, ...]

    @property
    def world_size(self):
        return len(self.node_ips)

    @classmethod
    def from_env(cls):
        value = os.environ.get("METAFLOW_MODAL_CLUSTER")
        return cls(**json.loads(value)) if value else None

    def to_env(self):
        return json.dumps(asdict(self))

    def report(self, returncode, stderr):
        message = {
            "rank": self.node_rank,
            "returncode": returncode,
            "stderr": stderr[-10000:],
        }
        while True:
            try:
                with socket.create_connection((self.node_ips[0], PORT)) as connection:
                    connection.sendall(json.dumps(message).encode())
                return
            except OSError:  # rank 0 has not started its step yet
                time.sleep(1)

    def wait_for_followers(self, step_func):
        def step(*args, **kwargs):
            with socket.create_server(("::", PORT), family=socket.AF_INET6) as server:
                result = step_func(*args, **kwargs)
                for _ in range(self.world_size - 1):
                    connection, _ = server.accept()
                    with connection, connection.makefile("rb") as stream:
                        follower = json.load(stream)
                    if follower["returncode"]:
                        raise RuntimeError(
                            f"Modal cluster node {follower['rank']} failed:\n"
                            f"{follower['stderr']}"
                        )
            return result

        return step
