"""Storage path helpers."""

from pathlib import Path


def safe_join(root, name):
    return Path(root) / name

