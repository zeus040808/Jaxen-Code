"""Domain models represented as plain dictionaries in the starter code."""


def make_user(user_id, name, active=True):
    return {"id": user_id, "name": name, "active": active}


def make_item(name, price, quantity=1):
    return {"name": name, "price": price, "quantity": quantity}

