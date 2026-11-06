# Event ideas statechart

What a creator sees on one camera-roll event's Ideas section ("Find ideas", "Another idea", Research on/off), modelled from `services/media_collection_stories.py`, `services/story_idea_research.py` and the iOS camera-roll surface. Each action cites its code line.

Rules (INVARIANTs in `EventIdeas.cfg`, each with the product sentence it encodes):

- An idea that passes the checks never ends in "Kria hit a problem".
- "No other idea found" and "No idea found yet" mean the AI actually looked this time.
- "Another idea" never removes ideas already shown.
- Once everything settles, the working card is gone.

`EventIdeas.tla` models the code before the fix. `EventIdeasFixed.tla` models the code after it (per-idea research review, empty answers never cached). Before the fix, the first two rules break. After it, all hold.

To regenerate the charts in `statechart/`, run this command. It needs Java 11+ and `tla2tools.jar`, plus the pstack `statechart` skill.

```bash
TLA2TOOLS=/path/to/tla2tools.jar JAVA=java node <pstack>/skills/statechart/scripts/statechart.mjs \
  from-spec EventIdeas.tla --fixed EventIdeasFixed.tla --label "PR fix" --title "Event ideas" --png --out statechart
```

The model leaves out the phone's 90-second timeout, concurrent requests, "personalization changed", and more than two rounds of ideas.
