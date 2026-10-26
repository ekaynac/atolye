import unittest

from slugify import slugify


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_turkish(self):
        self.assertEqual(slugify("Çağrı İşığı Öğün Şükür"), "cagri-isigi-ogun-sukur")

    def test_runs_and_edges(self):
        self.assertEqual(slugify("  --a__b!!c--  "), "a-b-c")

    def test_digits(self):
        self.assertEqual(slugify("v2.0 release"), "v2-0-release")

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")


if __name__ == "__main__":
    unittest.main()
