# kri_tadic_length

Prod thread aa36d140 (2026-11-14). One 14.03 s clip and the creator's song lyrics as text. No
creator message named a length. The planner chose 14.005 s, and the server's check cut it to 14 s
(the once-only cap rounds down to whole seconds). It then counted that cut as a lost creator ask:
five turns ended "Klipler toplam yaklaşık 14 saniye, bu yüzden video 14.005 saniye olamıyor."

    make kria-replay FIXTURE=kri_tadic_length

The scorecard is `scripts/kria/score_kri_tadic_length.py`. It runs the real policy check and the
real approval-readiness report, offline. The lyrics are replaced by a placeholder and the ids
are scrubbed. The trace has no draft row, so the strategy is rebuilt from the readiness sentence
(see `source.reconstructed`). Before the fix, checks 1-3 and 5 fail with the prod sentence.
