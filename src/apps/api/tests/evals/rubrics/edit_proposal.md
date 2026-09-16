# Guided edit proposal rubric

Score each dimension from 1 (poor) to 5 (excellent). A passing response averages at least 4.0
with no score below 3.

1. **Complete-media use** — selects at least seven distinct valid sources when available and uses
   both photos and videos without letting one generic clip dominate.
2. **Story coherence** — groups media into a clear, intentional sequence with at least three
   supported topics for a rich travel set.
3. **Creator direction** — follows the requested goal, pace, duration, and story/montage/explainer
   direction instead of returning a generic montage.
4. **Safe draft thoughts** — thoughts are concise, visibly useful starting points and never invent
   food taste, visited places, companions, feelings, or other personal facts absent from context.
5. **Editability** — title, beats, text, media choices, layouts, and durations form a concrete plan
   a creator could review and correct before rendering.

**Creator text contract.** When the input carries `shot_labels` (optionally with `opening_title`,
`opening_title_duration_s`, and `closing_title`), those are exact creator-authored words the server
burns verbatim: the opening title over the first `opening_title_duration_s` seconds (3.2s when
unstated) and the closing title over the final ~2 seconds. They are never beats or
`montage_text_bindings`. The correct plan returns one labeled beat per label, in order, with `thought`
equal to the label; the first and last labeled beat may be longer because they also carry the title
holds, or an unlabeled beat (empty `thought`) may carry a hold when a spare source exists. For
example, a 2s opening title, six 1.5s labeled shots, and a 2s closing title over six sources
correctly yields beat durations 3.5, 1.5, 1.5, 1.5, 1.5, 3.5: each label is still visible for
1.5s because the title and closing title cover the extra 2s of the first and last beat. Score
Creator direction and Editability on label wording, order, per-shot timing, and how well each shot
matches its label. Score Safe draft thoughts only on non-label thoughts, and Story coherence on the
creator's labeled sequence rather than self-chosen topics.
