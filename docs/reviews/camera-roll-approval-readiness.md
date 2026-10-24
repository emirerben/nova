# Camera-roll selected idea → approval readiness

Parent incident: KRI-731 (and KRI-733 current incident tracking). Scope: correct
one accepted, validated story handoff; no new planning feature or global gate change.

## Incident and cause established before repair

At deployed a38f2ce4e73725ab6ba2cd21c935c8eca2ff9865 the selection completes,
but its fresh thread has no brief. The special planner returns no readiness
manifest/brief updates. Common readiness rejects the server-generated selection
message as missing creator requirements. The actual saved readiness report has
brief_version0, no requirements and the SHA256 of null for brief/prepared evidence.
A zero-I/O offline assessment reproduces the exact message and both digests.
The physical phone upload → ideas → approval → export → editable reopen was not
verified by this lane. Previous execution fixtures explicitly disabled creative
brief and brief binding; simulator list tests never exercised this enabled gate.

The integration in #1685 introduced the incomplete selected-story contract after
#1683 introduced readiness enforcement; #1747 capacity and #1772 resume/UI did
not repair it. This is a local fix, not proof all KRI-731 acceptance is complete.

## Failure cases and acceptance before implementation

- F1: Production brief/readiness switches are enabled: select an owned idea,
  prepare its exact immutable execution, and offer one explicit approval without
  calling a model, granting approval, or dispatching a Job implicitly.
- F2: Bind actual accepted text, source selection/order/timing and original/muted
  sound. No invented instructions, empty delegated brief, special open-request
  exemption or global readiness/brief flag bypass.
- F3: Changing the story, source generation, approved strategy/window/digest or
  approval identity must refuse dispatch rather than replan, loosen a requirement,
  replace footage or resurrect stale approval.
- F4: Approved posting caption is exact, including spacing/newlines; an explicit
  empty caption stays empty and cannot trigger generated fallback copy. It is
  posting metadata, never an additional on-screen text requirement.
- F5: Prepare a synthetic QA copy from offline fixture footage through existing
  project/approval/render contracts. No original collection/draft/media mutation,
  paid providers, physical phone operation, merge or deployment.
- F6: Keep video readiness separate: READY clip/transcript rows and one historical
  failed story-query row do not establish which asset lacks usable current moments.
  Index/source/version/interval checks must identify the actual excluded member.

Target proof: existing real HTTP → SQL collection selection → runtime worker →
common readiness/approval path with enabled production gate settings, plus stale
story/source/approval controls, exact text/timing/audio and caption preservation.
Fixture/provider transport replay is allowed; the planner/readiness validator are real.

## Local implementation and verification

The selected-story planner now returns a real, revision-bound selection
requirement plus its resolved readiness manifest. The requirement identifies the
complete accepted prepared story by digest (source windows, text, audio, research
and posting caption); it does not delegate choices or ask a model to rewrite it.
Common readiness recompiles and checks that evidence, exact strategy and attached
source generations. A tagged story requirement with missing/changed evidence
cannot fall back to a generic checker for incidental words in its title.
The existing owner/story/source/compiler checks and explicit approval remain.

Posting caption reads the approved story before variant/generated fallback,
preserves reviewed internal whitespace/newlines, and treats empty as an explicit
choice. It suppresses speculative caption generation and checks content-plan
identity again at approval/installation. The default caption path is unchanged.

Before repair, the enabled-switch HTTP/SQL test completed with no approval;
then all8 original cases passed with the handoff fix. An added caption assertion
failed because no caption was recovered from the approved story. Final focused
matrix:17 passed, including readiness disabled, exact/empty caption, changed
collection/story/storage generation/window/caption/approval and missing evidence.
The source-generation control changes storage metadata at the transport boundary;
it does not disable the database's immutable-source trigger. The first broader
run exposed a caption helper that assumed optional assembly metadata on every
job view; fixing the boundary preserved the existing thread-only write test.
The adjacent readiness/binding/plan-feed/collection suites then passed179 cases.
Final current-main verification is recorded separately below before PR handoff.

All data is synthetic/local fixture data; live provider calls and original-data
writes are zero. The synthetic QA-copy fixture can be prepared with the existing
`scripts/ios/camera-roll-export-proof.py` harness, which creates correct and
wrong-window/audio/order cases. Preparing these inputs is not a phone export,
full app journey or editable Save/reopen proof.

## Remaining acceptance / separate diagnosis

The physical iPhone app/build and full positive app flow remain unverified here.
A historical failed story-query analysis is not proof of a failed clip analysis;
current exact source/spec and valid moment coverage must identify the actual
excluded member before any data retry. No retry or analysis-lifecycle patch is
part of this change.

An already held selection uses the same per-story selection identity and returns
its completed turn on re-entry. This change does not automatically requeue that
turn or modify the original failed thread/draft; fresh QA-copy selection is the
bounded verified path. Original-session recovery needs a separately explicit
creator action/recovery contract and positive test, preserving request idempotency.
KRI-731 physical/semantic fulfillment and rollout are not closed by this local fix.
