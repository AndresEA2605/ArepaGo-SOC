import unittest

from services.internal_auth import IDENTITY_HEADER, sign_identity, verify_identity

SECRET = b"test-secret-with-at-least-32-bytes!!"
USER_ID = "8f14e45f-ceea-4e8f-9f0b-2b1f3f4a5b6c"


class InternalAuthTests(unittest.TestCase):
    def test_round_trip_returns_the_signed_identity(self):
        token = sign_identity(USER_ID, "admin", secret=SECRET, now=1000)
        self.assertEqual(verify_identity(token, secret=SECRET, now=1001),
                         {"id": USER_ID, "role": "admin"})

    def test_rejects_a_tampered_payload(self):
        token = sign_identity(USER_ID, "customer", secret=SECRET, now=1000)
        payload, signature = token.split(".")
        self.assertIsNone(verify_identity(f"{payload}x.{signature}", secret=SECRET, now=1001))

    def test_rejects_an_expired_token(self):
        token = sign_identity(USER_ID, "customer", secret=SECRET, ttl=10, now=1000)
        self.assertIsNone(verify_identity(token, secret=SECRET, now=1011))

    def test_rejects_a_token_signed_with_another_secret(self):
        token = sign_identity(USER_ID, "admin", secret=SECRET, now=1000)
        self.assertIsNone(verify_identity(token, secret=b"another-secret-with-32-bytes!!!!", now=1000))

    def test_rejects_garbage(self):
        for value in ("", "abc", "a.b.c", None):
            with self.subTest(value=value):
                self.assertIsNone(verify_identity(value, secret=SECRET, now=1000))

    def test_refuses_to_sign_unknown_roles(self):
        with self.assertRaises(ValueError):
            sign_identity(USER_ID, "root", secret=SECRET)

    def test_header_name_is_stable(self):
        self.assertEqual(IDENTITY_HEADER, "X-ArepaGo-Identity")


if __name__ == "__main__":
    unittest.main()
