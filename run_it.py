from metaflow import Runner


with Runner("tests/flows/hello_world.py").run() as running:
    status = running.status
print(status)
