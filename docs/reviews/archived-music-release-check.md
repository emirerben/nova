# Archived music release check

The release inventory contained 73 archived tracks and zero published tracks. The old admin list still marked 66 eligible for generative matching.

For the negative control, two otherwise identical, ready, published rows with current label and section versions were inserted into an in-memory SQLite table. One had an archive timestamp. The WHERE clauses from the actual _load_matcher_candidates query were executed for both require_published values. Both returned only the active row. The existing admin music route suite passed all 15 tests. Production verification requires the worker deployment.
