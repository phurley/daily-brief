#!/usr/bin/env python3
"""Offline tests for URL helpers (no network needed).

Run: ``python3 -m unittest extract.test_urls``
"""

from __future__ import annotations

import unittest

from . import urls


class TitleLinkTest(unittest.TestCase):
    TEXT = (
        "### **[Recovery and Resilience Music Festival]"
        "(https://www.downriverarts.org/events-2/rrfest092026)**\n"
        "[📍](https://s.w.org/images/core/emoji/17.0.2/svg/1f4cd.svg)\n"
        "**More:** [The Metro: Recovery welcomes a sober celebration]"
        "(https://wdet.org/2026/09/09/the-metro-recovery/)"
    )

    def test_picks_title_link(self):
        self.assertEqual(
            urls.title_link(self.TEXT, "Recovery and Resilience Music Festival"),
            "https://www.downriverarts.org/events-2/rrfest092026",
        )

    def test_exact_match_skips_page_and_emoji(self):
        text = ("[Witches Night Bazaar](https://x.example/e)\n"
                "[📍](https://x.example/emoji.svg)")
        self.assertEqual(
            urls.title_link(text, "Witches Night Bazaar", page_url="https://list.example/guide"),
            "https://x.example/e",
        )
        # never returns the listing page itself
        self.assertIsNone(urls.title_link(text, "Witches Night Bazaar", page_url="https://x.example/e"))

    def test_prefix_is_not_enough(self):
        # A different session's link must not be bound to the base title.
        text = "[Witches Night Bazaar: Autumnal Equinox](https://x.example/e)"
        self.assertIsNone(urls.title_link(text, "Witches Night Bazaar"))

    def test_skips_image_wrapped_link(self):
        text = "[![Big Sky Company](https://cdn.example/photo.jpg)](https://detail.example/show)"
        self.assertIsNone(urls.title_link(text, "Big Sky Company"))

    def test_no_confident_match(self):
        self.assertIsNone(urls.title_link(self.TEXT, "Unrelated Event Title"))
        self.assertIsNone(urls.title_link(self.TEXT, "Short"))
        self.assertIsNone(urls.title_link("", "Recovery and Resilience Music Festival"))


if __name__ == "__main__":
    unittest.main()