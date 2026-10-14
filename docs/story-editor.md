# News and science editorial selection

The news and science collectors review a bounded shortlist against the original
article before an item can become featured. The shared `story-editorial.mjs`
selector supplies publication, browser views and editorial context. Full archives
remain available through the report's More controls, labeled as additional coverage.

The news editor checks up to 24 candidates after collection; the independent science
job checks up to 18. It also checks that the fetched headline matches and isolates the main article
body from related-story cards. It reads only public source material and the public `editorial`
policy in `brief-preferences.json`. No private calendar or browser feedback enters
the model context. Set `COLLECT_NEWS_EDITORIAL_LIMIT` or `SCIENCE_EDITORIAL_LIMIT`
to change these caps (maximum 40); the reviewer uses at most three workers. An accepted item with a word-count or
numeric-evidence problem gets at most one copy repair; substantive rejections
are never retried to force inclusion.

An accepted item needs relevance and substantive novelty scores of at least 3/5,
a concise summary supported by article evidence, and an official publication date.
News also needs evidence for its local connection. Publication metadata is read
mechanically; modification dates, event dates and related articles cannot supply it.
Conflicting, missing or future dates fail review. Date-only sources retain explicit
`datePrecision: date` metadata. Numeric summary claims must occur in cited evidence. A separate grounding audit
checks all material claims and distinguishes targets, planned experiments and grants
from demonstrated findings; ungrounded or non-substantive copy cannot feature.
The editor rejects remote logistics, celebrity gossip, routine arrests, sports,
listicles and routine institutional/funding announcements. Science interests are
configured explicitly rather than inferred from source diversity.

`news-editorial.json` and `science-editorial.json` carry accepted copy, provenance,
internal selection reasons and dated selected IDs. News has up to five picks and
science up to three; a shortage never promotes an unreviewed item. Selection retains
eligible existing choices during the day, deduplicates the underlying development,
and prepares yesterday/today/tomorrow views. Science keeps that order across its
former four-hour rotation slots. A changed source input invalidates the associated
review in both the publisher and browser. Reviews expire after 72 hours; news ages
out after seven days and science after fourteen. Unavailable review data fails
publication rather than silently reverting to formula-ranked highlights.

Each collector publishes its source feed and matching editorial file together.
Separate files prevent concurrent news and science jobs from overwriting one another.
Cached accept/reject decisions and supporting article excerpts stay in git-ignored
`data-collect/processed/{news,science}-editorial-state.json`. Unchanged inputs and
policy reuse accepted reviews; rejects retry after six hours. Transient outages
preserve an unexpired matching review, while changed facts and semantic rejections
cannot reuse it. Verified publication-date corrections also follow matching source inputs into
archive browsing, including stories rejected for relevance. Unverified news archive
dates are omitted. Article text is never published. The model uses the existing
`OPENROUTER_EDITORIAL_MODEL` setting (default `google/gemini-2.5-flash`); `OPENROUTER_EDITORIAL_CHECK_MODEL` controls
the grounding audit with the same default.

Run a preview in an isolated checkout with its existing API configuration:

```sh
cd data-collect
.venv/bin/python review_stories.py --root .. --section news --limit 24
.venv/bin/python review_stories.py --root .. --section science --limit 18
cd ..
node scripts/build_edition.mjs
```

These commands write local artifacts without pushing. A custom root must contain
the public source feeds and `brief-preferences.json`. Collector no-push modes also
run the review, so they can make bounded model calls.

Offline regression checks:

```sh
node --test scripts/story-editorial.test.mjs scripts/brief-edition.test.mjs
(cd data-collect && .venv/bin/python -m unittest test_review_stories)
python3 scripts/test_publish_brief.py
```

Tests cover official-date correction, unrelated metadata, missing/future/conflicting
dates, fabricated evidence and numeric claims, empty selections, cached decisions,
source/policy invalidation, bounded calls, deduplication, daily stability, expiry,
archive preservation and publication races. Existing saved editions keep their
legacy display until a complete edited edition is available.
