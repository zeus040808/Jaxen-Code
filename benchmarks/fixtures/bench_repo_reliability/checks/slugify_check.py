import unittest

from hhapp.text import slugify


class SlugifyTests(unittest.TestCase):
    def test_strips_outer_whitespace(self):
        self.assertEqual(slugify(" Hello World "), "hello-world")


if __name__ == "__main__":
    unittest.main()
