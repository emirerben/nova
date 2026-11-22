# KRI-831: the late-window rule and samples that match either frame

`phone-simple-cut-v5` answered `checked` for an export that was one frame late. A CI test
(`handheld25-1-frame-late`) found it on 2026-11-21: 2 failures in 480 repeats.

## The hole

Each picture sample may match the source frame displayed at its instant or the next one.
The next-frame tolerance absorbs proxy resampling. Since v3 a clip window on the source
frame grid failed when every sample needed its next frame.

A 30 fps analysis proxy of a 25 fps original shows every sixth picture twice. A sample that
lands on such a held frame matches the displayed frame and the next one. v5 counted it as
in place, so "every sample" was no longer true and the rule did not fire. A sample on slow
footage does the same without a held frame.

In the failing test the proxy is rate-capped and its next-frame scores sit at 0.948-0.952,
across the 0.95 bar. When four of five cleared the bar, the fifth sample was a held frame
and the late export passed. With a sharp proxy the same export passes in 5 of 6 timing
phases, every run.

## The rule

v6 changes only when a window on the grid counts as late (`_late_window`).

- A sample proves timing when it matches the displayed frame and not a different next one.
- A sample that matches both, or has no next frame, says nothing.
- A window fails when every sample needed the next frame (v3, unchanged), or when some
  sample needed the next frame, no sample proves timing, and `lead_next` is below 0.

`lead_next` is the mean of displayed SSIM minus next SSIM over the window, repeats left
out. It guards near-still footage, where codec noise can tip one sample to its next frame
while the window as a whole does not lean late. The v5 calibration already preferred a
window lead over a per-sample margin for the same reason
([kri-1042-picture-profile](kri-1042-picture-profile.md)).

Sample pass rules are unchanged, and every window v5 rejects, v6 rejects.

## Same media, both rules, on CI's FFmpeg

Run 44956879640. Twelve kinds of generated footage, a sharp proxy (crf 10) and the phone's
soft one (700 kbps), six timing phases, exports shifted by -2 to +2 frames: 720 windows,
each judged by the v5 rule and the v6 rule.

| Window | v5 passes | v6 passes | Of |
| --- | --- | --- | --- |
| Correct | 138 | 138 | 144 |
| One frame late | 115 | 68 | 144 |
| Other shifts (-2, -1, +2 frames) | 201 | 191 | 432 |

- No correct window changed. The 6 correct windows that fail are grain over a still against
  the soft proxy, which fails the exact bar in v5 too (no profile to pass on).
- No window that v5 rejects passes in v6.
- One-frame-late windows now rejected: all of handheld (5), calm pan (22), grain over a
  still (4), and 16 of 24 slow zoom.

| One frame late, by footage | v5 passes | v6 passes | Of |
| --- | --- | --- | --- |
| Handheld, 25 and 30 fps | 5 | 0 | 24 |
| Calm pan, 25 and 30 fps | 22 | 0 | 24 |
| Grain over a still | 4 | 0 | 12 |
| Slow zoom, 25 and 30 fps | 24 | 8 | 24 |
| Still, light grain, slow drift | 60 | 60 | 60 |

`lead_next` separates the two sides. Correct windows that pass: -0.0003 to +0.29. Late
windows v6 rejects: -0.29 to -0.0085. The lead condition lost no catch that the sample
rule alone made.

Why the 68 one-frame-late windows still pass: in every one, no sample needed its next
frame (each sample also matches the displayed frame). None passes because a sample was
taken as proof of timing, which is the route the original failure would need to survive.

The late-window, soft-proxy and frame-profile tests (37 per repeat) were also repeated on
CI: 258 of 258 repeats passed in run 44956879640, which ended at the 50-minute job limit
set for it. That shows no new failure. It does not prove the old one is gone, since a
1-in-240 failure stays hidden in a set this size about one time in three; the table and
the paragraph above do.

## Stored real reports

`python -m app.cli.replay_export_verdicts --fetch --prod --since 2026-10-25` on 2026-11-22
(GET only). Production held 9 reports in 7 of 84 phone-rendered jobs.

| Stored picture window | Count |
| --- | --- |
| Passed, still passes under v6 | 36 |
| Passed, would fail under v6 | 0 |
| Already failed | 9 |
| No per-sample evidence (v3) | 6 |

34 of the 36 passing windows have a sample beside a repeated frame, so real footage has
the held frames the hole needs. None of them is known to be a late export. One window had
samples on the next frame (5 of 14) with `lead_next` +0.006; six of its other samples
match only the displayed frame, so v6 passes it on both conditions. No passing window had
a negative `lead_next`. These v5 reports do not store whether a window
was on the grid, so the replay applied the rule to all of them, which can only overstate
flips.

The three Lisbon windows in
[kri-1042-picture-profile-replay.json](kri-1042-picture-profile-replay.json) have no
next-frame samples and leads of +0.05 to +0.37, so they pass unchanged.

## Failure modes of the picture check

This is the fourth fix to the export check's verdicts since KRI-1042 opened (text proof v4,
frame profile v5, evidence precision, and this), so the ways the picture check can still be
wrong are listed with the test that holds each.

Failure modes for KRI-1042 (after fix #3)
- correct export of detailed handheld footage against the soft proxy -> checked | covered by test_correct_export_against_a_soft_proxy_passes_on_its_frame_profile
- one or two frames late or early, wrong window, 2% zoom or 1% offset against the soft proxy -> rejected | covered by test_frame_profile_never_accepts_a_shifted_or_wrong_picture
- one frame late with a sample on a held frame (25 fps original, 30 fps proxy), held sample first, middle, last or twice -> rejected | uncovered - adding test_one_frame_late_is_rejected_when_samples_match_either_frame
- one frame late with a low-motion sample that matches both frames -> rejected | uncovered - adding test_one_frame_late_is_rejected_when_samples_match_either_frame (calm_25 and handheld_25-phase1)
- correct export whose samples land on held or slow frames -> checked | uncovered - adding test_correct_export_with_either_frame_samples_passes_against_a_sharp_proxy
- near-still footage where noise tips one sample to its next frame -> checked | uncovered - adding test_late_window_rule_reads_only_samples_that_tell_the_two_frames_apart
- a sample in a long hold with no next frame beside late samples -> rejected | uncovered - adding test_late_window_rule_reads_only_samples_that_tell_the_two_frames_apart
- every sample on the grid needed its next frame -> rejected | covered by test_on_grid_whole_clip_frame_shift_never_counts_as_checked
- correct export on the grid with a single next-frame sample -> checked | covered by test_on_grid_single_neighbour_frame_remains_allowed
- off-grid source start, where next frames are legitimate -> checked | covered by test_off_grid_source_start_compares_the_displayed_frame_without_widening_the_window
- variable-rate proxy with held frames, correct export -> checked | covered by test_variable_frame_rate_proxy_with_held_frames_matches_its_cfr_export
- still footage below the exact bar -> rejected, no profile to pass on | covered by test_still_footage_below_the_exact_bar_has_no_profile_to_pass_on
- a stored report that the current rule would reject goes unlisted before a bump -> listed, exit 1 | uncovered - adding test_a_checked_report_the_current_rule_rejects_is_listed_as_an_upper_bound

Still open, with no test because the check cannot see it: the cases under Limits.

## Limits

- Footage where consecutive frames both clear the exact bar in every sample (a still, light
  grain, a slow drift, part of a slow zoom) cannot show a one-frame shift. 68 of 144
  one-frame-late windows still pass, all of this kind.
- The real sample is small: 36 passing windows from production, because few phone
  exports are checked at all (9 reports in 84 jobs).
- Other shifts on slow zoom and drift still pass where they passed in v5 (191 of 432).
- An export one frame early is not covered by this rule. It fails the exact bar unless the
  footage is this slow.
