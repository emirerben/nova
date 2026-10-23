## 1. 2 messages: 1 answered using 2, 2 about to start

Rule broken: Kria answers each message with only what you had said up to it; your next message gets its own answer, so it is not acted on twice.
Why: Kria planned its answer to the first message with the follow-up in view, and the follow-up then gets its own answer too, so it is acted on twice.
There are 72 ways to get here in the model. This is the shortest.

0. Start: Empty chat: nothing sent
1. You send message 1. Now: One message: 1 thinking
2. You send message 2. Now: 2 messages: 1 thinking, 2 waiting
3. Kria replies to message 1. Now: 2 messages: 1 answered using 2, 2 about to start

With the fix, step 3 (Kria replies to message 1) cannot happen, so you stay at: 2 messages: 1 thinking, 2 waiting.

## 2. 2 messages: 1 stopped, 2 stuck waiting

Rule broken: Once Kria is done, a follow-up you sent is never left waiting with nothing running, which refuses every new message with "A follow-up is already waiting".
Why: the server restarted after Kria finished or stopped one message and before it started the waiting one, and nothing ever starts it, so every new message is refused.
There are 22 ways to get here in the model. This is the shortest.

0. Start: Empty chat: nothing sent
1. You send message 1. Now: One message: 1 thinking
2. You send message 2. Now: 2 messages: 1 thinking, 2 waiting
3. You remove message 1. Now: 2 messages: 1 stopped, 2 about to start
4. The server restarts. Now: 2 messages: 1 stopped, 2 stuck waiting

With the fix, step 4 (the server restarts) takes you to: 2 messages: 1 stopped, 2 waiting for the sweep (up to a minute).
