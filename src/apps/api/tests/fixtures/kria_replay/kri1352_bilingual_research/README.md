# KRI-1352 bilingual research fixture

The request: an informal but respectful explanation of a public date, shown word by word in the
middle of the screen, with the same text translated into English at the bottom, also word by
word, shown together. Montage, four archival clips, 19.6 s. In production the render failed as
`phone_plan_failed`; the text never reached the video.

`expectation.json` is the acceptance record that
`tests/evals/request_following/test_research_feeds_text_sequences.py` reads. It holds the typed
brief, the output windows and the observable expectations. Its `must_fail` list names the
negative-control tests, and the test module checks each one exists.

    cd src/apps/api && .venv/bin/python -m pytest tests/evals/request_following/test_research_feeds_text_sequences.py

Offline and free: provider answers are stubbed, and the composer parse, op coercion, text replay
and plan compile are the real pipeline. A stubbed reviewer approval proves orchestration only,
not translation or factual accuracy.

## Recorded (structure only, from the production job)

- the typed brief: two `text_sequence` requirements without a literal, `unit: word`,
  `mode: agent_choice`, positions `middle` and `bottom`, and no `facts.research`
- four selected clips with output windows 0-3.4, 3.4-6.4, 6.4-17.4 and 17.4-19.6 s
- research was decided at render; the approval carried no prepared research

## Synthetic (never production data)

- the clips, facts, sources and wording in the tests: an invented "Founders Day in Exampleland"
- the creator words, kept out of git; only their structure is described here

The live replay of the real request is a separate, approved, ignored local harness. Its result is
recorded in `docs/reviews/kri-1352-researched-text-layers.md` once it runs.
