"""Text helpers."""


def slugify(value):
    return value.lower().replace(" ", "-")


def parse_bool(value):
    return bool(value)


def unique_words(values):
    return list(set(values))


def truncate(value, limit):
    return value[:limit] + "..."

