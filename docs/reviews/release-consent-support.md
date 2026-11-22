# Release consent and support checks

Production edit chat selects Claude. The release disclosure names Anthropic and explains that edit requests and project context are shared for suggested changes. The account-scoped consent key advances from current1 to current2 so returning accounts see the new disclosure.

Both localization checks passed for the English, German and Turkish catalog. The support and privacy routes each returned HTTP 200 from the local Next server. The rendered support page contained the usekria@gmail.com mail link, and the privacy table contained Anthropic (Claude) and the edit chat purpose. Web lint and TypeScript passed. The complete web suite passed 1,019 tests. Verification of the public URLs and replacement TestFlight upload requires deployment.

At the largest accessibility text size the first draft placed the continue button too far down for the consent journey. Shortened repeated text in all three languages. On the iPhone 17 simulator, all three AIConsentTests and both account privacy UI journeys passed, including explicit opt-in and declining at large text.
