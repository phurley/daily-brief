"""Offline regressions for out-of-state scopes and Michigan place homonyms."""
import unittest

from . import locality


class LocalityTest(unittest.TestCase):
    def test_out_of_state_url_overrides_matching_local_name_and_feed_prior(self):
        for state, city in [("new-york", "Chelsea"), ("ohio", "Canton"),
                            ("illinois", "Plymouth"), ("california", "Oakland")]:
            with self.subTest(state=state, city=city):
                result = locality.detect(
                    f"News from {city}", f"https://patch.com/{state}/{city.lower()}/news",
                    source_tier=0, source_location="Canton, Michigan",
                )
                self.assertTrue(result["out_of_area"])
                self.assertIsNone(result["tier"])
                self.assertEqual(result["reason"], f"url state: {state}")

    def test_state_qualified_homonyms_disambiguate_repeated_title_mentions(self):
        for place in ["Chelsea, NY", "Canton, OH", "Canton, Ohio", "Plymouth, Massachusetts"]:
            with self.subTest(place=place):
                city = place.split(",")[0]
                result = locality.detect(f"New development in {city}\n{place}", source_tier=0)
                self.assertTrue(result["out_of_area"])
                self.assertIsNone(result["tier"])

    def test_generic_michigan_mention_does_not_rescue_qualified_foreign_city(self):
        self.assertTrue(locality.detect("Michigan visitors attend a show in Chelsea, NY")["out_of_area"])

    def test_michigan_homonyms_are_retained(self):
        for place, tier in [("Chelsea, MI", 2), ("Chelsea, Michigan", 2),
                            ("Canton, MI", 0), ("Plymouth, Michigan", 0)]:
            with self.subTest(place=place):
                result = locality.detect(place, "https://patch.com/michigan/canton/news")
                self.assertFalse(result["out_of_area"])
                self.assertEqual(result["tier"], tier)
                self.assertEqual(locality.detect(place)["tier"], tier)

    def test_local_events_and_cross_state_reporting_keep_separate_local_evidence(self):
        cases = [
            ("New York artist performs at The Ark in Ann Arbor", 2),
            ("Detroit delegation visits Canton, OH", 3),
            ("Chelsea, MI residents meet their counterparts in Chelsea, NY", 2),
            ("Michigan compares statewide policies with Ohio", 4),
            ("Millington, Michigan artist receives an award in Nashville, Tennessee", 4),
            ("Baraga County, MI firefighters assist their counterparts in Wisconsin", 4),
            ("Michigan, Indiana, Ohio and New York", 4),
            ("Detroit, MI\nWayne County", 3),
            ("Wayne, MI", 1),
        ]
        for text, tier in cases:
            with self.subTest(text=text):
                result = locality.detect(text, "https://news.example/story", source_tier=0)
                self.assertFalse(result["out_of_area"])
                self.assertEqual(result["tier"], tier)

    def test_query_and_fragment_paths_are_not_article_locations(self):
        result = locality.detect(
            "Ann Arbor concert", "https://venue.example/show?next=/new-york/#/ohio/",
        )
        self.assertFalse(result["out_of_area"])
        self.assertEqual(result["tier"], 2)
        self.assertTrue(locality.detect("Chelsea", "https://patch.com/new%2Dyork/chelsea/")["out_of_area"])

    def test_source_location_is_not_evidence_and_unknown_uses_only_the_prior(self):
        result = locality.detect("A neighborhood update", source_tier=1,
                                 source_location="Canton, Michigan")
        self.assertEqual(result["tier"], 1)
        self.assertEqual(result["reason"], "source prior")
        self.assertIsNone(locality.detect("A neighborhood update")["tier"])
        self.assertFalse(locality.detect("Washington Township orchard", source_tier=3)["out_of_area"])


if __name__ == "__main__":
    unittest.main()
