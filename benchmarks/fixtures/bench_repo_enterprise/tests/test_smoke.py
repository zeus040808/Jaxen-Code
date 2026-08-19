import unittest

from acme.models import make_user
from acme.service import deactivate_user


class SmokeTests(unittest.TestCase):
    def test_deactivate_user(self):
        user = make_user("u1", "Ada")
        self.assertFalse(deactivate_user(user)["active"])


if __name__ == "__main__":
    unittest.main()

