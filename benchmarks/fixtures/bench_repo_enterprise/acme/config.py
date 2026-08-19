"""Runtime configuration."""

import os


DEFAULT_TIMEOUT = 30
DEFAULT_RETRIES = 3


def timeout_from_env():
    value = os.environ.get("ACME_TIMEOUT")
    return value if value is not None else DEFAULT_TIMEOUT

