"""API-shaped pure functions."""

from .repository import page


def list_users(users, offset=0, limit=20):
    return {"items": page(users, offset, limit), "count": len(users)}

