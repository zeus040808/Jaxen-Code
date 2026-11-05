"""Plain-text reporting."""


def render_users(users):
    return "\n".join(user["name"] for user in users)

