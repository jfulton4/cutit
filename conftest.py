"""Test setup.

create.py reads TABLE_NAME and constructs a boto3 resource at import time,
so both must be satisfied before the module is imported. No network calls are
made -- boto3 resource construction is lazy -- but it does require a region.
"""
import os

os.environ.setdefault("TABLE_NAME", "cutit-test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
