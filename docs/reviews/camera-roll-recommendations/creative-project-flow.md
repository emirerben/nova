# Creative idea → approved editable project

The creator clarified the product on 19 October 2026: recommendations must propose TikTok content with an angle, audience, hook/payoff, reasoning tied to the footage, and explicit suggested wording. A plain joined montage does not meet this goal. Choosing an idea creates a normal editable project; separate render approval still controls the exact output. No prediction of views or performance is presented as fact.

## Decisions before implementation

- Duration: `EditProposalSnapshot.frame_schedule`, prepared through `refresh_snapshot_schedule`, owns ordered source windows and 30fps duration. The existing guided/phone compiler owns all text windows.
- Ripple: source replacement/reordering uses the existing complete-interval correction contract. Beat reasons follow their moment IDs. Proposed opening/closing wording stays unchanged unless explicitly edited. No new lane insertion or continuous music lane is added.
- Scrub bounds: existing native project/editor duration and recipe; no separate recommendation timeline or transport.
- Resize: existing editor trimming and text timing; no new resize interaction. Source ranges quantize on the shared frame grid before approval.
- Undo: recommendation changes create durable story revisions; project/editor undo remains its existing snapshot boundary. A change invalidates an old render approval.
- Parity: PostgreSQL selection → draft → explicit approval → frozen worker plan tests must preserve exact proposed copy and source windows; production phone export must show that copy and retain original-resolution bindings. Legacy no-copy recommendations remain no-copy.

## Failure cases to reproduce first

1. An idea's reviewed wording disappears in preparation or its rationale is substituted as a caption.
2. Choosing the idea is mistaken for permission to render or silently generates different words.
3. Reordering/removing moments attaches a reason to the wrong clip; a replacement retains a false reason.
4. Old cached ideas, old native decoders, and legacy no-copy approved snapshots change behavior.
5. A proxy is passed off as an original, or visually similar/edited Photos bytes bind without matching stored SHA-256 and byte count.
6. Relinking scans outside explicit selection, reads media without bounds, loses a working original, or crosses accounts.
7. The iPhone demo uses an intercepted API or a prebuilt recipe instead of the collection → idea → project → approval → device-render HTTP path.

## Device evidence required

Use only this creator's authorized selected Photos entries and owned uploads. Match full-quality files with their immutable original receipts, keep originals on device, and report unmatched originals honestly. Exercise real HTTP handlers, authentication, durable project/approval records, and production native rendering against an isolated development copy of the creator account. Record the local environment distinction, versions, calls/spend under the existing shared $2 cap, the chosen idea/reasoning/copy, rendered output, and reopened editable project. Do not claim a production rollout or a broader creator pilot.

## Findings and corrections

The first full-quality upload passed all duration, text and audio checks, but a picture check failed. Before changing verification, audit the native brand rectangle: failures include masking the wrong corner, scaling its position and size with different axes, hiding unrelated footage under an oversized mask, or changing picture thresholds to accept the example. The native compositor's approved tile is `(30, 1386, 193, 119)` on 1080×1920. Verification must mask only that furniture with the compositor's single-axis-constrained scale, and retain all picture thresholds and wrong-export controls.

The next normal export reproduced a text-reader failure before any correction: 24 of 25 measured checks passed, but the closing sample at 8.6s was rejected. The actual frame visibly contains `Which experience do you prefer?`. At the verifier's forced 2× enlargement, OCR reads `hich...` or adds a leading curly quote; reading the actual colour-isolated pixels at native size yields the exact words above the unchanged confidence threshold. A correction must retain exact matching, absent/wrong-copy and wrong-timing rejection, both unprocessed and colour-isolated pixel views, the overall verification deadline, and the rejected attempt's history. It must never paint the expected text or remove punctuation to accept this case.

- Real PostgreSQL red controls reproduced disappearing approved text, prompt-version story identity collisions, unhandled provider validation errors, and reuse of a settled paid-call identifier on retry. The fixes preserve exact copy through the shared compiler, include the agent version in idea identity, return a private retry response, and bind each paid request to its acquired fact-lease attempt token. The shared run-spend cap remains authoritative.
- Live generation exposed generic travel copy and duplicated explanations exceeding review limits. The model now returns one concise creative plan; existing public hook/payoff/rationale fields derive from its explicit opening/closing/angle/reasoning. No model-generated source description is substituted as text.
- Twelve originals were matched by SHA-256 and byte count from only the owner's existing selection; no original upload or whole-library scan was needed. The first native creative export uses three 3840×2160 source receipts and contains the reviewed opening and closing text. Its 1080×1920 H.264/AAC output is 10.1 seconds including the standard brand tail.
- The real hardware run reached separate approval and native rendering. Its first upload failed because fixture storage lacked signed PUT support. The private local adapter now accepts single-use, expiring, exact-size, create-only uploads only behind the approved source-address restriction. The DEBUG client accepts only that configured private origin and upload prefix; release uploads remain HTTPS-only. No API response, creative recipe, renderer or verification result is injected.
- An older failed sync loop was stopped through the ordinary failure endpoint, preserving the native file. The phone reported a serious thermal state during that run; it is not a successful energy benchmark. The explicit QA launch now stays awake during the test. Complete upload verification and editable-project reopen evidence are still pending.
- A fresh native output uploaded through the real reservation/PUT/completion routes. Its first check accepted duration, all nine text visibility/timing checks, and all nine audio chunks, but rejected one picture sample. The verifier masked the bottom-right corner while the actual native brand tile sits at the approved bottom-left position. The corrected tile mask is smaller and compositor-aligned; picture thresholds are unchanged. Read-only rechecking of those exact uploaded bytes now accepts all 22 checks. Forty-eight actual-export/HTTP/publication controls pass, including wrong windows/order/text/rate/audio. The final normal publication/reopen run remains pending because Xcode explicitly requires the owner to unlock the phone.

## Current checks

32 focused HTTP/PostgreSQL/agent contract checks, 48 actual-export checks, 1,244 free replays (15 documented skips), 209 web timeline checks, 28 desktop checks and 582 backend timeline checks pass. Mobile timeline checks passed 41/42 initially; the one decoder-start failure passed its isolated recheck. The latest pre-PR run passes all 1,043 mapped reliability checks. Native hardware build and generated contract consistency checks pass. The signed-in full hardware journey remains incomplete until the final synchronization/reopen run passes.

At this checkpoint, app-model ledger settlement is $0.025966 total across the earlier proxy examples and this continuation, under the existing shared $2 authorization. Failed model responses are included. No original upload, production write, broader participant enrollment or production rollout occurred. The 9.6s uploaded native example and the 10.1s earlier native example are distinct exports; the 22-check recheck describes the 9.6s file.
