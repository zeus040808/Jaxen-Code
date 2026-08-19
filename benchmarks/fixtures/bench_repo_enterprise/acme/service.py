"""Application service functions."""

from .models import make_user
from .validators import is_email


def register_user(users, user_id, name, email):
    if not is_email(email):
        return None
    user = make_user(user_id, name)
    users.append(user)
    return user


def deactivate_user(user):
    user["active"] = False
    return user

