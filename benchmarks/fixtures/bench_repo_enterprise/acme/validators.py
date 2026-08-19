"""Input validation."""


def is_email(value):
    return isinstance(value, str) and "@" in value


def is_positive_int(value):
    return isinstance(value, int) and value > 0

