"""Unit tests for case-insensitive hype moment token matching."""

import unittest

from detector import message_matches_moment_group


class MomentTypePatternTests(unittest.TestCase):
    def test_requested_funny_matches(self):
        for text in (
            "asdasd", "asdasdasd", "ahsjdh", "sjsjsj", "asjdhaskd",
            "ахахаха", "ㅋㅋㅋ", "LMFAO", "KEKW",
        ):
            with self.subTest(text=text):
                self.assertTrue(message_matches_moment_group(text, "FUNNY"))

    def test_requested_non_matches(self):
        for text in ("dakika", "sakla", "kalkan", "haklı", "salaksın", "Lapeace", "alaska"):
            with self.subTest(text=text):
                self.assertFalse(
                    any(
                        message_matches_moment_group(text, group)
                        for group in ("FUNNY", "HYPE", "SHOCK")
                    )
                )


if __name__ == "__main__":
    unittest.main()
