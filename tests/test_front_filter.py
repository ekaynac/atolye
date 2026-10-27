"""The front filter's redactor: a secret is never emitted, however the stream is chunked."""

import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "front_filter", Path(__file__).resolve().parents[1] / "stack" / "front" / "front_filter.py")
ff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ff)

SECRET = 'sk-live-AbC"123\\xyz'  # contains characters that JSON escapes


def stream(red, data: bytes, size: int) -> bytes:
    out = b"".join(red.feed(data[i:i + size]) for i in range(0, len(data), size))
    return out + red.flush()


class RedactorTest(unittest.TestCase):
    def test_secret_split_at_every_boundary_is_redacted(self):
        for form in ff.secret_forms([SECRET]):
            data = b'data: {"content": "key=' + form + b'"}\n\n'
            for size in range(1, len(data) + 1):
                red = ff.Redactor(ff.secret_forms([SECRET]))
                out = stream(red, data, size)
                self.assertNotIn(form, out, f"chunk size {size}")
                self.assertIn(b"[REDACTED]", out)
                self.assertEqual(red.count, 1)

    def test_text_without_secrets_passes_unchanged_whatever_the_chunking(self):
        data = b'data: {"choices":[{"delta":{"content":"hello world"}}]}\n\ndata: [DONE]\n\n'
        for size in (1, 3, 7, 64):
            self.assertEqual(stream(ff.Redactor(ff.secret_forms([SECRET])), data, size), data)

    def test_short_values_are_not_treated_as_secrets(self):
        self.assertEqual(ff.secret_forms(["", "abc", "1234567"]), [])


if __name__ == "__main__":
    unittest.main()
