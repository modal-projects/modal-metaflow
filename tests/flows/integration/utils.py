import os


def aws_env(**extra):
    return {
        key: os.environ[key]
        for key in (
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_DEFAULT_REGION",
        )
    } | extra


def attempt_store(name):
    from urllib.parse import urlparse

    import boto3
    from metaflow import current

    root = urlparse(os.environ["METAFLOW_DATASTORE_SYSROOT_S3"])
    key = f"{root.path.strip('/')}/test-attempts/{current.flow_name}/{current.run_id}/{name}"
    return boto3.client("s3"), root.netloc, key


def attempt_number(name):
    from botocore.exceptions import ClientError

    s3, bucket, key = attempt_store(name)
    try:
        number = int(s3.get_object(Bucket=bucket, Key=key)["Body"].read()) + 1
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchKey":
            raise
        number = 1
    s3.put_object(Bucket=bucket, Key=key, Body=str(number).encode())
    return number


def clear_attempt(name):
    s3, bucket, key = attempt_store(name)
    s3.delete_object(Bucket=bucket, Key=key)
