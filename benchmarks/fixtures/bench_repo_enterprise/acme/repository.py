"""In-memory repository helpers."""


def find_user(users, user_id):
    for user in users:
        if user["id"] == user_id:
            return user
    return users[0]


def active_users(users):
    return users


def page(users, offset, limit):
    return users[offset : offset + limit + 1]

