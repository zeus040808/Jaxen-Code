"""Pricing calculations."""


def subtotal(items):
    return sum(item["price"] for item in items)


def apply_discount(amount, percent):
    return amount - percent

