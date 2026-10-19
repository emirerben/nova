# KRI-734: actual export verification

## First promoted adapter

The first slice verifies the existing immutable phone request: one contiguous
video track, hard cuts, rate 1, cover fit, static positioned text, and the clips'
original sound or silence. The analysis-proxy receipt already binds the original
fingerprint, full source timeline, preserved audio and immutable storage generation.
The verifier compares decoded output with those pinned proxies, not desired
metadata. No model call, new renderer or original upload is involved.

KRI-722/723/724/725 remain open: this slice does not claim their prepared snapshot
migration or full cloud/slide/effects coverage. Existing cloud guards stay intact.
Promotion is explicit and stamped when a supported phone request is pinned;
historical requests remain readable. Disabling enrollment cannot bypass an
already stamped request. Retry must retain the required verification policy.

## Acceptance cases, fixed before product changes

* Real H.264/AAC export, two sources/windows in approved order, static title in its
  approved place/window, original audio at approved volume: checked before Ready.
* Correct files with wrong source, source window, order, playback speed, missing
  title, moved/shortened title, doubled sound or unexpected sound: rejected.
* Dark scenes need local structure/luminance evidence as well as absolute pixel
  error; near-black wrong footage must not pass merely because every pixel is
  small. A matching H.264 round trip may differ by 1–2 decoded colour codes.
* Text must stop at its approved end, including before the fixed brand tail.
* Missing decoder/OCR/source evidence and unsupported operations: never checked.
* A file named `.mp4` containing Matroska, or a playlist referencing other files,
  cannot pass the upload boundary. Decode only local ISO media with explicit
  file/pipe protocols; never trust a filename or H.264/AAC stream labels alone.
* A failed replacement keeps the previous video, poster and URL. The rejected
  attempt cannot become successful on a repeated completion request.
* Stale completion after an editor revision cannot publish. Exact retry rechecks
  the bytes against its own request; it cannot inherit a predecessor's report.
* Reports bind output checksum/storage generation, request revision/digest,
  source receipt digest, verifier version and measured checks. No signed URLs,
  transcripts or source images are retained in reports.
* A measured match with stale request identity is rejected, including its saved
  report. Changed source receipts require a reviewed new edit, not an exact-retry
  loop. Unknown verification policies remain unsupported.

Picture checks sample the source/output clock, including near cuts; they are
sampled evidence, not an assertion that every pixel/frame has been inspected.
Local SSIM complements mean RGB error; the absolute 2/255 codec tolerance avoids
rejecting matching very dark pictures due to SSIM's relative luminance term.
Audio checks compare decoded PCM with the expected source and gain, including
silence. Text checks use local OCR in the approved region/window, including
outside-window controls. Conservative uncertainty blocks promotion.

## Timeline decisions

1. Duration/order owner: the pinned EditRecipe and its `duration` property;
   `BRAND_TAIL_SECONDS` owns the separately declared outro.
2. No lane timestamps are changed; ripple is not introduced.
3. Existing editor/playback bounds are unchanged; checks use recipe time only.
4. No resize behavior changes.
5. No authored state/undo changes; reports attach to an export attempt.
6. Paired real-export positive/negative fixtures prove cut order, source windows,
   text timing, audio and legacy publication fences independently of metadata.

## Evidence levels

Offline actual-export checks, native output, deployed behavior and human playback
are separate claims. This ticket remains open until each promoted adapter has its
required actual-output evidence; a healthy deployment is not that evidence.

The media cases run real encoders/decoders and OCR. Their explicit timeouts are
120 seconds for one check and 240 seconds for a failed attempt plus exact retry;
this changes no assertion and does not raise the production verifier's 90-second
decode budget. The initial parallel web timeline run hit cold Next.js chunk-load
timeouts; the identical assertions pass with one browser worker (28 desktop and
42 mobile cases). No web source or browser assertion was changed.
