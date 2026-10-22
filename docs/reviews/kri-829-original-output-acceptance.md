# KRI-829 original-output acceptance, 2026-10-22

**All four parent cases remain open.** This is an evidence ledger, not a
request-fulfillment or deployment claim. The original requests, local source
assets, incident job records and existing pull requests were refreshed before
editing. Completed fixes were reused; shared work owned by Emir was not replaced.

Nova's [release process](../runbooks/release-metadata.md) applies. The autoship
package-release workflow does not fit this repository. Feature PRs leave
release metadata to the post-merge workflow. No production rollout or paid
model call was performed in this continuation.

## Dependencies and ownership

The existing sequence is [#1730](https://github.com/emirerben/nova/pull/1730)
→ [#1728](https://github.com/emirerben/nova/pull/1728)
→ [#1738](https://github.com/emirerben/nova/pull/1738)
→ [#1741](https://github.com/emirerben/nova/pull/1741)
→ [#1744](https://github.com/emirerben/nova/pull/1744).
[#1723](https://github.com/emirerben/nova/pull/1723) updates the replay after
the functional dependencies land. [#1745](https://github.com/emirerben/nova/pull/1745)
is the independent label-recovery continuation after merged #1739. #1732
is already merged. A copied dependency commit or a green local suite does not
mean a dependency has landed.

Emir owns KRI-723 (actual named actions and event-relative placement), KRI-724
(executable phrase/speaker edits), KRI-828 (freeze engine), KRI-826 (text/layout),
and KRI-827 (face/plate avoidance). This continuation's new implementation is
the Yasin-owned KRI-551 consented-retake path, default off; see its
[bounded evidence](kri-551-consented-retakes.md). KRI-798 is a separate open race.

## Source and request checks

Raw media and source-derived transcripts/exports remain outside git. Local
scratch evidence is under `/private/tmp/kri-829-evidence`; it is not a durable
download or a native-export claim. The findings below retain the source identity
and reviewed observations needed to distinguish real requested content from
matching counts or plausible labels.

| Case | Original request and observed failure | Independent source expectation | Remaining acceptance |
| --- | --- | --- | --- |
| Merope M2 / KRI-775 | Six roughly two-second moments in order; Cotopaxi photo after hello for one second; one-second bite freeze; original sound/no music; exact title, about 14 s. Jobs `417eecd4-681b-4c7f-8075-4a1dddfe6e85` and `50f67a06-2c78-4691-831c-5754e3bf9f74` refused a 4.7 s plan against 14 s and produced no export. | Reviewed source intervals: tray arrival 0–2 s; hello 5.5–7.5; plate close 37.5–39.5; bite 57–59.5; thumbs/OK-like gesture 61.3–63.3; bye 65.5–67.5. The uploaded portrait photo is Cotopaxi. The ordinal candidate starts 12.04/18.09/30.59/43.09/50.04 mostly show similar plate-holding moments, despite the correct count; 57.44 shows the bite. This wrong-content control fails actual action fulfillment. | Actual action grounding, photo after the real hello, freeze duration/content, exact title and photo label, original audio and 14 s timing. Owners KRI-723/828/826; physical export and recovery remain KRI-775/829. |
| Merope T1 / KRI-787 | Start at Caro's introduction and finish just after “I'm gonna be here all week”; remove “with bread, no,” and “how are you gonna”; caption both voices with requested spellings; MEROPE on Caro's first mention; no text over face/plate, about 35 s. Incident jobs `0701fcc9…` and `f88555bc…` produced no output. | Original source is the same bytes as M2. The local original-source trim diagnostic starts at 12.265 and ends 50.380 s, yielding 38.133333 s encoded duration (38.115 s plan). Local transcription confirms the requested endpoints but still hears both requested removals. Cleanup-on planned duration is 35.335 s; duration alone does not establish correct speech removal. The first-any-speaker MEROPE trigger is Yasin's earlier mention, not Caro's at about 40.04 s. | Exact phrase removal, speaker-specific trigger, both-voice captions/spellings, safe text placement, picture/audio inspection and actual iPhone export. Owners KRI-724/826/827; KRI-790 fixes only the source window. |
| Don Julio M1 / KRI-802 | Waiter smile and full name exchange, sauces, menus, full audible Cheers under the sign, red-cap smile; exclude blurry table; sauce label correction from `+` to `&`; exact title; 13–17 s accepted. A tango suggestion is not permission to add a song. Incident notes record lost clip binding, 27-character label versus 24-character cap, inappropriate pop-in treatment of speech and invalid optional editorial data. | Nine original asset hashes match the captured contract. Reviewed M1_05 at 1.8 s shows the waiter/menu exchange; M1_08 at 1.35 shows three bowls; M1_03 at 1.75 points/stirs bowls; M1_09 at 1.25 shows wooden menus; M1_01 at 1.4 shows raised glass; M1_02 at 1.1 shows the red-cap smile; M1_04 at 1.3 is the excluded blurred table. Existing M1_09 metadata mislabeled menus as a waiter tray. | Full spoken “What's your name?”/“Lucas” and Cheers boundaries, actual ordered scenes, corrected sauce label on the right footage, title, audio permission and final timing; correction/retry/Save/reopen and phone export. Shared action/speech dependencies remain KRI-723/724. |
| Voiceover N3 / KRI-551 | Remove pauses and the abandoned kilometer-30 start; preserve kilometer-32 delivery; medal photo on “the medal,” watch photo on “four hours and twelve minutes”; quiet crowd under voice, clean captions. Job `8a0ced82-6b45-46ff-8b56-126b893a4617` used analysis `a9628062-b1e6-4f1d-9f50-df5738fa46c0`: 15 removals (14 pauses, one filler), 14.308 s, zero retakes. | Original voiceover is 47.420 s. The recovered original analysis places the abandoned take at word indexes 28..32, ending 17.400 s, and the intended “At kilometer 32” at 19.700–21.420 s. A replacement local transcript placed it later: its trial cuts overlap retained speech on the original clock, so those initial audio checks are rejected as acceptance evidence. Using those original words, the current combined replay removes 13.900–19.580 s, keeps the whole intended phrase byte-for-byte, and produces 31.092125 s audio versus 33.112125 s baseline. Output retranscription retains kilometer 32 and later medal/time words. The detector answer is manually injected. | Live detector accuracy, caption picture/timing, phrase-bound photos, voice/crowd mix, correction/retry/Save/reopen and physical export. No complete-case pass is claimed. |

M2/T1 source SHA-256:
`66b42816bfdab0cb46b935a14a7d08bddd5ef85b6b9b911328d6d3306af0ae99`
(55,291,210 bytes, 1080×1920, 68.37 s, audio present).
N3 source SHA-256:
`c9d0e89a84022d74ac8405d91f83f05cdede46d69d3422bcc94c0ed019adfc59`.

## Export and recovery evidence

- **Label renderer:** #1745 head `40cc2179c83fde9d200c2966b82bf6238162d2b3`
  passed the production-image overlay gate: 124 PASS, zero FAIL/WARN/SKIPPED.
  The report and montage, including enlarged center/left/right sauce/place
  labels, were inspected. [Exact-head artifact](https://github.com/emirerben/nova/actions/runs/39766617602/artifacts/12421025714).
  This is a renderer-only gate without OCR; it is not the Don Julio export.
- **T1 local FFmpeg:** original-source trim diagnostics and endpoint frames
  were inspected. They expose remaining named-cut failures. The prepared
  phone proof also includes wrong-window and wrong-audio controls. It is an
  operator-supplied recipe, not the full signed-in chat journey.
- **Physical iPhone:** paired hardware is available, but the separate QA app
  cannot be provisioned because Xcode lacks the required developer account.
  The user was asked to sign in. No QA app was installed and no physical export
  was produced. The user's normal app was not replaced.
- **Recovery:** focused recorded tests cover label correction/source affinity,
  current-analysis consent reuse and stale-policy refusal. These support the
  individual fixes. They do not prove correction, retry, Save and reopen for
  any of the four original production cases.

## Deployment observation, not rollout

Read-only API and speech-worker profiles at 10:45:45 and 10:51:12 UTC on
2026-10-22 reported release 1766 / revision
`6d734e34deafdbe35e28cb7abd824f12d340be20`. Guided voiceover cleanup, runtime v2,
phone rendering, legacy retake cutting, budget clamping and Gemini reference
transcription were enabled; minimum phone protocol was 2. The new preflight
retake flag was not deployed. API and relevant worker identities were checked
separately. These observations do not establish exposure to any new change.

Close a parent case only after its original exported picture and audio meet
every request above and its correction/retry/Save/reopen flow is observed.
GitHub checks, replay counts and renderer fixtures alone cannot close it.
