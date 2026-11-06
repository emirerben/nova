---- MODULE EventIdeasFixed ----
\* What a creator sees on one camera-roll event's Ideas section, modelled from
\* services/media_collection_stories.py generate_stories (S), services/story_idea_research.py (R)
\* and iOS Core/CameraRollSurface.swift (C). Branch codex/event-ideas-steering @ 9944f07b5.
EXTENDS Naturals

VARIABLES screen, shown, peak, notice, req, kind, research, firstCache, out, asked, retried, usable

vars == <<screen, shown, peak, notice, req, kind, research, firstCache, out, asked, retried, usable>>

Init ==
    /\ screen = "empty" /\ shown = 0 /\ peak = 0 /\ notice = "none"
    /\ req = "idle" /\ kind = "first" /\ research = FALSE /\ firstCache = "none"
    /\ out = "none" /\ asked = FALSE /\ retried = FALSE /\ usable = FALSE

\* ---- creator ----
OpenFinder ==
    /\ screen = "empty" /\ req = "idle"   \* CameraRollEventPage.swift:197
    /\ screen' = "finder"
    /\ UNCHANGED <<shown, peak, notice, req, kind, research, firstCache, out, asked, retried, usable>>

ToggleResearch ==
    /\ screen = "finder"   \* CameraRollIdeaViews.swift:116
    /\ research' = ~research
    /\ UNCHANGED <<screen, shown, peak, notice, req, kind, firstCache, out, asked, retried, usable>>

FindIdeas ==
    /\ screen = "finder" /\ req = "idle"          \* CameraRollIdeaViews.swift:80
    /\ kind' = "first" /\ req' = "claiming" /\ screen' = "working" /\ notice' = "none"
    /\ out' = "none" /\ asked' = FALSE /\ retried' = FALSE /\ usable' = FALSE
    /\ UNCHANGED <<shown, peak, research, firstCache>>

AnotherIdea ==
    /\ screen = "ideas" /\ req = "idle"           \* CameraRollEventPage.swift:216
    /\ kind' = "another" /\ req' = "claiming" /\ screen' = "working" /\ notice' = "none"
    /\ out' = "none" /\ asked' = FALSE /\ retried' = FALSE /\ usable' = FALSE
    /\ UNCHANGED <<shown, peak, research, firstCache>>

\* ---- server ----
CachedAnswer ==
    \* The first request's digest repeats when nothing changed; "another" grows its exclude set.
    /\ req = "claiming" /\ kind = "first" /\ firstCache # "none"   \* media_collection_stories.py:438
    /\ req' = "filtering"
    /\ out' = IF firstCache = "ready" THEN "good" ELSE "empty"
    /\ UNCHANGED <<screen, shown, peak, notice, kind, research, firstCache, asked, retried, usable>>

AskModel ==
    /\ req = "claiming" /\ (kind = "another" \/ firstCache = "none")  \* media_collection_stories.py:440
    /\ req' = "asking"
    /\ UNCHANGED <<screen, shown, peak, notice, kind, research, firstCache, out, asked, retried, usable>>

ModelAnswers ==
    /\ req = "asking"
    /\ \E o \in {"good", "allFiltered", "empty"}:
        /\ out' = o
        /\ usable' = (o = "good")
        /\ req' = IF research /\ o # "empty" THEN "reviewing" ELSE "filtering"   \* story_idea_research.py:298
        \* Cached before filtering; an empty "another" answer is not cached (media_collection_stories.py:443).
        \* An empty answer is never cached (media_collection_stories.py:443, fix).
        /\ firstCache' = IF kind = "first" /\ o # "empty" THEN "ready" ELSE firstCache
    /\ asked' = TRUE
    /\ UNCHANGED <<screen, shown, peak, notice, kind, research, retried>>

ModelFails ==
    \* Timeout, or every proposed idea malformed: TerminalError -> 503 (media_collection_stories.py:450).
    /\ req = "asking"
    /\ asked' = TRUE /\ usable' = FALSE
    /\ notice' = "problem" /\ req' = "idle"
    /\ screen' = IF shown > 0 THEN "ideas" ELSE "empty"
    /\ UNCHANGED <<shown, peak, kind, research, firstCache, out, retried>>

ReviewApproves ==
    /\ req = "reviewing"
    /\ req' = "filtering"
    /\ UNCHANGED <<screen, shown, peak, notice, kind, research, firstCache, out, asked, retried, usable>>

ReviewRejects ==
    \* Each idea is reviewed alone; a flagged one is dropped, the rest go on (story_idea_research.py:307, fix).
    /\ req = "reviewing"
    /\ \E o \in {"good", "empty"}: out' = o /\ usable' = (o = "good")
    /\ req' = "filtering"
    /\ UNCHANGED <<screen, shown, peak, notice, kind, research, firstCache, asked, retried>>

KeepIdeas ==
    /\ req = "filtering" /\ out = "good"   \* media_collection_stories.py:585
    /\ shown' = IF shown < 2 THEN shown + 1 ELSE shown
    /\ peak' = IF shown < 2 THEN shown + 1 ELSE shown
    /\ screen' = "ideas" /\ notice' = "none" /\ req' = "idle"
    /\ UNCHANGED <<kind, research, firstCache, out, asked, retried, usable>>

RetryOnce ==
    /\ req = "filtering" /\ out = "allFiltered"   \* media_collection_stories.py:559
    /\ kind = "another" /\ ~retried /\ ~research  \* media_collection_stories.py:545
    /\ req' = "retrying" /\ retried' = TRUE
    /\ UNCHANGED <<screen, shown, peak, notice, kind, research, firstCache, out, asked, usable>>

RetryAnswers ==
    \* A failed retry is swallowed as an empty answer (media_collection_stories.py:567).
    /\ req = "retrying"
    /\ \E o \in {"good", "empty"}: out' = o /\ usable' = (o = "good")
    /\ req' = "filtering"
    /\ UNCHANGED <<screen, shown, peak, notice, kind, research, firstCache, asked, retried>>

NothingNew ==
    /\ req = "filtering"
    /\ \/ out = "empty"
       \/ out = "allFiltered" /\ (kind = "first" \/ retried \/ research)   \* media_collection_stories.py:559, CameraRollSurface.swift:602
    /\ req' = "idle"
    /\ screen' = IF shown > 0 THEN "ideas" ELSE "empty"
    /\ notice' = IF kind = "another" THEN "noOther" ELSE "noneYet"   \* CameraRollSurface.swift:603
    /\ UNCHANGED <<shown, peak, kind, research, firstCache, out, asked, retried, usable>>

System == CachedAnswer \/ AskModel \/ ModelAnswers \/ ModelFails \/ ReviewApproves
          \/ ReviewRejects \/ KeepIdeas \/ RetryOnce \/ RetryAnswers \/ NothingNew

Next == OpenFinder \/ ToggleResearch \/ FindIdeas \/ AnotherIdea \/ System

Spec == Init /\ [][Next]_vars

Settled == ~ENABLED System

\* "When the AI came up with an idea that passes the checks, I never see 'Kria hit a problem'."
UsableIdeaNeverShownAsProblem == notice = "problem" => ~usable

\* "'No other idea found' means the AI actually looked again this time."
NoOtherMeansAsked == notice = "noOther" => asked

\* "'No idea found yet' means the AI actually looked this time, not a replay."
NoIdeaYetMeansAsked == notice = "noneYet" => asked

\* "Asking for another idea never removes ideas I already have."
IdeasNeverDisappear == shown = peak

\* "Once everything settles, I am never left looking at the working card."
NeverStuckWorking == Settled => screen # "working"
====
