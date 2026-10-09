import unittest
from datetime import datetime, timezone
from html import escape
from reddit import parse_feed, collect_reddit, clean_url
from unittest.mock import patch

AT = "2026-10-09T18:00:00+00:00"
def feed(title="Starship next flight could be in November", body="Month-level community guess, not a schedule.", published="2026-10-08T12:00:00+00:00", link="https://www.reddit.com/r/spacex/comments/abc/discussion/", links=""):
    return f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>{escape(title)}</title><published>{published}</published><updated>2026-10-09T17:00:00Z</updated><link href="{link}"/><content type="html">{escape(body + links)}</content></entry></feed>'

class RedditTests(unittest.TestCase):
    def test_preserves_guess_without_making_target(self):
        claims, _ = parse_feed(feed(), AT)
        self.assertEqual(len(claims), 1)
        e = claims[0]
        self.assertEqual(e["verification"], "unverified")
        self.assertEqual(e["sourceType"], "community")
        self.assertEqual(e["claimType"], "discussion")
        self.assertEqual(e["communityKind"], "speculation")
        self.assertIn("November", e["excerpt"])
        self.assertNotIn("target", e)
        self.assertNotIn("authorization", e)

    def test_official_flair_and_primary_link_never_verify_claim(self):
        xml = feed(title="Starship Flight 15 Official launch discussion", links='<a href="https://www.spacex.com/launches/starship-flight-15?utm_source=reddit">operator</a>')
        e = parse_feed(xml, AT)[0][0]
        self.assertEqual(e["linkedSourceUrls"], ["https://www.spacex.com/launches/starship-flight-15"])
        self.assertEqual(e["originId"], e["linkedSourceUrls"][0])
        self.assertEqual(e["verification"], "unverified")
        self.assertEqual(e["missionId"], "starship-flight-15")

    def test_old_missing_future_dates_and_other_subreddits_are_excluded(self):
        for value in ("2025-10-08T12:00:00Z", "2026-10-10T12:00:00Z", "", "2026-10-08"):
            self.assertEqual(parse_feed(feed(published=value), AT)[0], [])
        self.assertEqual(parse_feed(feed(link="https://www.reddit.com/r/other/comments/abc/topic/"), AT)[0], [])
        self.assertEqual(parse_feed(feed(title="Starlink satellites", body="Falcon 9 launch"), AT)[0], [])
        self.assertEqual(parse_feed(feed(title="Dragon launch thread", body="Statistics include all Starship launches"), AT)[0], [])

    def test_link_identity_parameters_are_preserved(self):
        self.assertEqual(clean_url("https://www.youtube.com/watch?v=flight15&utm_source=reddit"), "https://www.youtube.com/watch?v=flight15")

    def test_bad_feeds_fail_closed(self):
        with self.assertRaises(ValueError):
            parse_feed("<html>Sign in</html>", AT)

    def test_deleted_comments_and_unsafe_links(self):
        self.assertEqual(parse_feed(feed(body="[deleted]"), AT)[0], [])
        e = parse_feed(feed(links='<a href="javascript:alert(1)">bad</a><a href="https://user:secret@example.org/path">bad</a>'), AT)[0][0]
        self.assertEqual(e["linkedSourceUrls"], [])

    def test_active_thread_comments_have_their_own_dates(self):
        thread = "https://www.reddit.com/r/spacex/comments/abc/development/"
        e = parse_feed(feed(title="Comment", body="I guess the next flight is months away", link=thread+"/def/"), AT, thread_url=thread)[0][0]
        self.assertEqual(e["publishedAt"], "2026-10-08T12:00:00+00:00")
        self.assertEqual(parse_feed(feed(title="Comment", body="Thanks!"), AT, thread_url=thread)[0], [])

    def test_reposts_share_origin_not_reddit_permalink(self):
        a = parse_feed(feed(links='<a href="https://www.spacex.com/launches/example">source</a>'), AT)[0][0]
        b = parse_feed(feed(link="https://www.reddit.com/r/spacex/comments/def/repost/", links='<a href="https://www.spacex.com/launches/example?utm_source=other">source</a>'), AT)[0][0]
        self.assertEqual(a["originId"], b["originId"])
        self.assertNotEqual(a["sourceUrl"], b["sourceUrl"])

    def test_partial_outage_is_visible_and_keeps_available_leads(self):
        with patch("reddit.time.sleep"), patch("reddit.fetch_html", side_effect=[feed(published=datetime.now(timezone.utc).isoformat()), OSError("HTTP 403")]):
            claims, health = collect_reddit({"id":"reddit-spacex", "url":"https://www.reddit.com/r/spacex/"})
        self.assertEqual(health["state"], "error")
        self.assertIn("403", health["detail"])
        self.assertEqual(len(claims), 1)

if __name__ == "__main__": unittest.main()
