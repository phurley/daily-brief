import unittest
from sources import parse_source, mission_id

AT = "2026-10-09T18:00:00+00:00"
class SourceTests(unittest.TestCase):
    def parse(self, name, html):
        return parse_source({"id": name, "type": "operator" if name == "spacex" else "regulator", "url": "https://example.com/"}, html, AT)

    def test_shell_and_copyright_are_not_evidence(self):
        claims, health = self.parse("spacex", '<app-root></app-root><footer>Copyright 2026. October 9, 2026</footer>')
        self.assertEqual(claims, [])
        self.assertEqual(health["state"], "no-dated-evidence")

    def test_old_county_notices_are_stale_despite_current_footer(self):
        claims, health = self.parse("cameron", '<table><tr><td>Oct 13, 2025</td><td>Closure</td></tr></table><footer>2026</footer>')
        self.assertEqual(claims, [])
        self.assertEqual(health["state"], "stale")

    def test_county_notice_does_not_claim_publication_date_or_mission(self):
        claims, health = self.parse("cameron", '<table><tr><td>October 10, 2026</td><td>Closure scheduled</td></tr></table>')
        self.assertEqual(claims[0]["noticeDate"], "2026-10-10")
        self.assertIsNone(claims[0]["publishedAt"])
        self.assertIsNone(claims[0]["missionId"])

    def test_faa_background_is_never_launch_authorization(self):
        claims, _ = self.parse("faa", '<p>The FAA released the Starship assessment September 2, 2026.</p>')
        self.assertFalse(claims[0]["authorization"])
        self.assertIsNone(claims[0]["publishedAt"])

    def test_dated_operator_target_preserves_day_and_net(self):
        claims, _ = self.parse("spacex", '<meta property="article:published_time" content="2026-10-09T12:00:00Z"><main><h1>Starship Flight 14</h1><p>SpaceX is targeting no earlier than October 10, 2026.</p></main>')
        self.assertEqual(claims[0]["missionId"], "starship-flight-14")
        self.assertEqual(claims[0]["target"]["precision"], "day")
        self.assertTrue(claims[0]["target"]["net"])
        self.assertEqual(claims[0]["target"]["lower"], "2026-10-10")

    def test_undated_future_and_negated_announcement_are_rejected(self):
        for meta in ('', '<meta property="article:published_time" content="2026-10-11T12:00:00Z">'):
            self.assertEqual(self.parse("spacex", meta + '<h1>Starship Flight 14</h1><p>SpaceX is targeting October 10, 2026.</p>')[0], [])
        html = '<meta property="article:published_time" content="2026-10-09T12:00:00Z"><h1>Starship Flight 14</h1><p>SpaceX is no longer targeting October 10, 2026.</p>'
        self.assertEqual(self.parse("spacex", html)[0], [])

    def test_ordinal_flight_names(self):
        self.assertEqual(mission_id("Starship's fourteenth flight test"), "starship-flight-14")

if __name__ == "__main__": unittest.main()
