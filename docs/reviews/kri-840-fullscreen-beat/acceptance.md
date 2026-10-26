# KRI-840 — full-screen photo at a spoken word (acceptance)

Expected-output record for the two real test-kit requests. Written from the ticket's
Done-when and the kits' own measured timings, before reading any result. Raw media stays
outside git (`~/Downloads/kria-ecuador-test`, `~/Downloads/kria-julio-test`).

## Requests

| Case | Exact request (phone Talking) | Word time in the footage |
|---|---|---|
| Café T2 | "'traditional Colombian dessert' dediğinde oblea fotoğrafı tam ekran gelsin" + "'Ecuador' dediğinde Cotopaxi fotoğrafı köşede küçük bir kart olarak çıksın" | "traditional" 5.64 s (whisper.cpp words, source 17.44 s − 11.8 s footage start); "Ecuador" 2.55 s |
| Don Julio T2 | "When he says "Argentinian", show the grill photo full screen for a moment. When he says chimichurri, show the chimichurri photo as a small card in the corner." | "Argentinian" ≈ 5.0–5.6 s, "chimichurri" ≈ 8.0–8.8 s (kit README, ±0.3 s) |

## Expected outcome

1. The full-screen photo (oblea / grill) is a full-frame cutaway on the device recipe's
   full-screen track, starting within 0.5 s of the word.
2. It lasts the requested hold: "for a moment" ≤ 1.5 s; no hold named → a short cutaway
   (≤ 1.5 s), not the 3 s corner-card default.
3. The speaker's track and original audio keep playing under it; captions keep drawing on
   top of it.
4. The corner-card photo (Cotopaxi / chimichurri) stays a small card at its own word.
5. The receipt never calls a corner card "full screen" (and never calls a full-screen photo
   the corner card that was asked). A full-screen ask covered only by the even-slot
   `fullscreen_images` sequence is "partial", with a reason that says it is not at the word.

## Wrong-content controls

- The same request planned as a corner card must be reported "partial", not "met".
- A full-screen photo where a corner card was asked must be reported "partial".
- Even-slot `fullscreen_images` placement (café 6.6–9.6 s, Don Julio 5.78–8.78 s in the
  original probes) must not be reported as the word-timed request.

## Not covered here

An actual iPhone export of either case. The device already draws this track (KRI-297,
`DeviceMontageRenderE2ETests.testSubtitledFullscreenVisualsRenderOnTheIPhone`), but a
re-send of both kit prompts on the phone is still owed (KRI-829).
