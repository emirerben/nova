# iPhone app in German and Turkish

The Kria iPhone app shows its screens in English, German or Turkish, following the phone's
language (Settings → Kria → Language overrides it per app). Chat replies are separate: the
server answers in the language the creator types (see `src/apps/api/app/kria/reply_language.py`).

## Where the text lives

- `src/apps/ios/Kria/Resources/Localizable.xcstrings`: every screen string (Apple String Catalog,
  source language English). Keys are the English text; `%@` / `%lld` mark interpolated values.
- `src/apps/ios/Kria/Resources/InfoPlist.xcstrings`: the permission prompts (camera, microphone,
  photos, speech).
- Plural forms (`%lld clips`) are catalog variations; Turkish uses one form after numbers.
- German and Turkish were drafted by AI on 2026-11-14 and are marked `needs_review` in the catalog.
  After a native speaker checks a string, set its state to `translated`.

## Writing new screen text

- `Text("…")`, `Button("…")`, `Label("…")`, `.accessibilityLabel("…")` and other SwiftUI APIs that
  take a string literal are localized automatically.
- Anything that is a `String` first (a property, a function result, an error message, a parameter of
  our own components) must be `String(localized: "…")`. Interpolate whole sentences
  (`String(localized: "Saved \(count) slides")`); never glue translated fragments together.
- Numbers shown to people use format styles inside the interpolation, for example
  `String(localized: "\(seconds, format: .number.precision(.fractionLength(1))) s")`, so German and
  Turkish get a decimal comma.
- Do not localize what the server or tests read: accessibility identifiers, chat messages the app
  sends for the creator, values compared with server text, log lines.
- Text that only exists in UI-test fixtures, previews or debug screens stays English.

## Checks (CI runs all three)

```bash
python3 scripts/ios/localization.py check   # catalog: German + Turkish present, placeholders match
python3 scripts/ios/localization.py scan    # source: no new English-only screen text
python3 scripts/ios/localization.py built src/apps/ios/.derived-data   # after a build: no string missing from the catalog
```

Command-line builds do not update the catalog (only Xcode's IDE does). After adding text:

```bash
python3 scripts/ios/localization.py sync src/apps/ios/.derived-data   # adds new English keys
python3 scripts/ios/localization.py import translations.json            # {"English": {"de": "…", "tr": "…"}}
```

`scan` flags a literal that looks like screen text but is not passed to a localizing API. If it really
is never shown to people (a server value, a file name, video content), add it to
`scripts/ios/localization-allowlist.json` (or run `scan --update` after checking every new entry).

## Wording

Match the chat's German and Turkish (`src/apps/api/app/kria/copy/{de,tr}.json`): German uses
informal "du", Turkish informal "sen". Format names: Talking / Konuşmalı, Montage / Montaj,
Voiceover / Seslendirmeli. Keep "Kria" untranslated.

## Not covered yet

- Some chat-language pairs inside the app are still English/Turkish only and follow the chat
  language, not the phone (`SteerNow.swift`, `MomentOrderQuestion.swift`, `VoiceoverScriptCard.swift`,
  `ResearchPreview.swift`). German needs a third branch there.
- Push notification text comes from the server and is English.
- Error text the server sends (shown as-is in the app) follows the server's language rules.
