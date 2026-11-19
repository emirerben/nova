# KRI-1445: one live check of `MainCreatorOutput.conversation_language`

Owner-approved one-off run, 2026-11-16, development ledger run `kria-conv-lang-2026-11-16`.
Six Main Creator calls on `gemini-3.1-pro-preview` (thinking "high"), no judge, settled cost
$0.3005 in total ($0.0653, $0.0593, $0.0393, $0.0383, $0.0468, $0.0515). The run's test file
was not committed. No prompts, tokens or URLs are kept here.

**Not covered:** production runs `MAIN_CREATOR_MODEL=gpt-6.1-sol` (default `json` output mode).
No Sol call was made, so prod-model behaviour is a pending check.

| Case | Stored chat language | Message | Accepted | Reported | Action |
| --- | --- | --- | --- | --- | --- |
| 1 | tr | The incident's first message: an English request around Turkish lyrics | en | en | propose_strategy |
| 2 | en | Tadic: Turkish lyrics pasted after "Could you paste the lines?" | null or en | en | propose_strategy |
| 3 | en | "Yap" | null or en | null | ask_user |
| 4 | tr | "Do it" | null, en or tr | null | propose_strategy (reply in Turkish) |
| 5 | en | A Turkish sentence to Kria | tr | tr | ask_user (reply in Turkish) |
| 6 | en | English message with a Turkish title after a label | en | en | propose_strategy |

All six reports were within the accepted set. Where a case accepts null, null means "could not
tell": the chat keeps its stored language.
