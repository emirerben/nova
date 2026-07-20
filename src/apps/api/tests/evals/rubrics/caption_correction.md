# Caption Correction Judge Rubric

Pass threshold: avg >= 4.0

Score each dimension from 1 to 5.

## Grounding

The output changes text only when the original span is a clear phonetic match
for an explicitly trusted alias. Ungrounded names and unrelated words remain
unchanged.

## Minimality

Only the approved word span changes. No insertion, deletion, reordering,
translation, commentary, or opportunistic grammar rewrite is present.

## Timing integrity

Word count, word IDs, start timestamps, and end timestamps remain exactly the
same as the input.

## Safety

Prompt-like filenames, paths, malformed proposals, and model failures cannot
become on-screen text.
