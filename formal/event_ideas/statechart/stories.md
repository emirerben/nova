## 1. Research on: no ideas yet, “Kria hit a problem”

Rule broken: "When the AI came up with an idea that passes the checks, I never see 'Kria hit a problem'."
Why: with Research on, the fact checker rejects the whole batch when it flags one idea, so good ideas are thrown away and the request fails.
There are 3 ways to get here in the model. This is the shortest.

0. Start: Research off: no ideas yet
1. Tap Find ideas. Now: Research off: direction panel open
2. Toggle Research. Now: Research on: direction panel open
3. Tap Find ideas from N ready. Now: Research on: Find ideas, checking saved answers
4. No saved answer, AI asked. Now: Research on: Find ideas, AI is writing ideas
5. AI answers with usable ideas. Now: Research on: Find ideas, fact checker reviewing ideas
6. Fact checker flags one idea. Now: Research on: no ideas yet, “Kria hit a problem”

With PR fix, step 6 (fact checker flags one idea) takes you to: Research on: Find ideas, dropping repeats.

## 2. Research off: no ideas yet, “No idea found yet”

Rule broken: "'No idea found yet' means the AI actually looked this time, not a replay."
Why: an empty first answer is saved, so tapping Find ideas again with the same direction replays it without asking the AI.
There are 2 ways to get here in the model. This is the shortest.

0. Start: Research off: no ideas yet
1. Tap Find ideas. Now: Research off: direction panel open
2. Tap Find ideas from N ready. Now: Research off: Find ideas, checking saved answers
3. No saved answer, AI asked. Now: Research off: Find ideas, AI is writing ideas
4. AI answers with nothing. Now: Research off: Find ideas, dropping repeats
5. Nothing new survives. Now: Research off: no ideas yet, “No idea found yet”
6. Tap Find ideas. Now: Research off: direction panel open, “No idea found yet”
7. Tap Find ideas from N ready. Now: Research off: Find ideas, checking saved answers
8. Same request as last time, saved answer reused. Now: Research off: Find ideas, dropping repeats
9. Nothing new survives. Now: Research off: no ideas yet, “No idea found yet”

With PR fix, step 8 (same request as last time, saved answer reused) cannot happen, so you stay at: Research off: Find ideas, checking saved answers.
