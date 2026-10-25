#!/usr/bin/env python3
"""Check the phone-render TLA+ model (PhoneRender.tla) against the code it describes.

The model is the iPhone render lifecycle behind the "killer" screens: "Kria couldn't
prepare this edit on your iPhone" (S1), "Couldn't refresh this edit" (S2), "This edit
needs your attention" (S3) and the "couldn't render" family (S4). PhoneRender.tla is the
code on this branch (the fixes); PhoneRenderBefore.tla is origin/main ff27b0a65.

Steps (all must pass):
  0. happy    - with no faults the model publishes a video and every rule holds there
  1. drift    - the covered Swift/Python functions still hash to what the model was
                last synced to (coverage.json); a change means re-read the model
  2. model    - PhoneRender.tla keeps every PASSING rule in each of its runs
  3. residual - PhoneRender.tla still breaks each RESIDUAL rule in its scoped run (a real
                finding on today's code; see findings.md)
  4. before   - PhoneRenderBefore.tla breaks every rule the fixes made pass (history)
  5. teeth    - each passing rule fails on a copy of PhoneRender.tla with its guard removed

When the code changes so a residual rule holds, move it from RESIDUAL to PASSING (and
widen its runs to the scenario it was scoped out of), add teeth, rerun. Never weaken a
rule to make it pass.

Usage: python3 formal/ios_edit_flow/verify.py [--resync] [--full] [--explore]
  --resync   rewrite coverage.json hashes + sync_commit (only after re-reading the model)
  --full     also run FULL_RUNS: Stop with declines and lost capabilities, two saves with
             every condition, and the heavier liveness pairs (several more minutes)
  --explore  also run every residual rule per starting scenario and event set, and write
             .dev/formal-ios-edit-flow/explore.json (used for findings.md)
Env:   TLA2TOOLS (tla2tools.jar v1.8.0), JAVA (java 11+ binary)
Exit:  0 PASS, 1 FAIL, 3 PASS with drift to review
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = REPO / ".dev/formal-ios-edit-flow"
SPEC = HERE / "PhoneRender.tla"
BEFORE = HERE / "PhoneRenderBefore.tla"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar"
TLC_SHA256 = "dae639a1f51a9e13ce60c369b15f32124cf1a78017aca366b6de0ae2b5169439"
LOCAL_TOOLS = Path("/private/tmp/nova-formal-tools")
# Per TLC run. A run past it is a FAIL line; split a slow run's events instead of raising it.
TLC_TIMEOUT = int(os.environ.get("TLC_TIMEOUT", "900"))

ALL_EVENTS = '{"save", "other", "stop", "kill", "background", "reaper", "sync"}'
ALL_CONDS = '{"none", "hot", "unsupported", "outdated", "schema", "missing"}'
SCOPED = '{"none", "hot", "missing"}'  # conditions without a decline
# A recipe this iPhone will never render: a missing capability, a protocol-floor gap (the
# GET answers 426) and a schema gap the server cannot see (the phone's own gate says so).
DECLINES = '{"unsupported", "outdated", "schema"}'
# Another device on an older build, another device's save adding a feature this build can't
# decode, and an operator lowering a recipe's protocol floor.
BUILD_EVENTS = '{"old", "feature", "rollback"}'
BUILD_CONDS = '{"none", "unsupported", "outdated", "schema"}'
# Approval no longer routes anything in the fixed code (the server always sends
# retry_requires_approval = false, R:535; nothing in PhoneRender.tla reads `approval`), so
# the fixed runs pin it to FALSE. Chat: the chat workspace (and its poll) is under the
# editor; FALSE is the editor opened from the gallery (EditorViews:265).
BASE = {"MaxFaults": "1", "MaxTicks": "1", "Approvals": "{FALSE}", "Chat": "{TRUE}"}
ONE_SAVE = {**BASE, "MaxSaves": "1", "MaxId": "3", "Events": ALL_EVENTS}
TWO_SAVES = {**BASE, "MaxSaves": "2", "MaxId": "4", "Events": '{"save", "other"}'}
# Liveness runs: two answered preview polls, so the editor can see a newer generation
# twice (E:7666-7682); the real budget is 300.
LIVE = {**BASE, "MaxTicks": "2", "MaxSaves": "1", "MaxId": "3"}

SAFETY = [
    "NoStalePublish",
    "PublishedStaysPublished",
    "SyncedMeansPublished",
    "NoStaleRequestReinstalled",
    "CloudDecisionReported",
    "NoSpuriousAttention",
    "NoFutileRepin",
    "UpdateTold",
    "NoRepinLoop",
    "KeptIsThisRequest",
    "UpdateMeansOutdated",
]
INVARIANTS = [("INVARIANT", r) for r in SAFETY]
WAY_OUT = [("PROPERTY", "AttentionHasWayOut"), ("PROPERTY", "PreviewConverges")]
# run name -> (specification, constants, [(kind, rule)]). Every PASSING rule appears in
# at least one run; TLC checks the rules of a run together. No run scopes a condition out
# any more: an unsupported or outdated recipe is the creator's turn with its own button.
MODEL_RUNS = {
    "one-save-all-events": ("Spec", {**ONE_SAVE, "Conds": SCOPED}, INVARIANTS),
    "one-save-declines": (
        "Spec",
        {**ONE_SAVE, "Conds": DECLINES, "Events": '{"save", "kill", "reaper"}'},
        INVARIANTS,
    ),
    # The phone loses a verified capability after its export finished (S `.decline where
    # resumesSync`): the video it has uploads, and anything else is told to the server.
    "lost-capability": (
        "Spec",
        {**ONE_SAVE, "Conds": '{"none", "hot"}', "Events": '{"caps", "save", "kill"}'},
        INVARIANTS,
    ),
    "two-saves": (
        "Spec",
        {**TWO_SAVES, "Conds": '{"none", "unsupported", "outdated"}'},
        INVARIANTS,
    ),
    # Builds (round 3): an older device declining or retrying, a newer device's save with a
    # feature this build can't decode (the 426 names its request), an operator rollback.
    "builds": (
        "Spec",
        {
            **ONE_SAVE,
            "Conds": '{"none", "unsupported", "outdated", "schema", "newschema"}',
            "Events": BUILD_EVENTS,
        },
        INVARIANTS,
    ),
    # Round 4: an operator rollback or an update clears the version gap while the phone
    # lost a capability or is hot ("Update Kria" only for an outdated build), and a newer
    # request declined after this phone rendered an older one (no "Ready on this iPhone"
    # for an earlier request's file).
    "builds-caps": (
        "Spec",
        {
            **ONE_SAVE,
            "Conds": '{"none", "hot", "outdated", "schema", "newschema"}',
            "Events": '{"rollback", "caps", "save", "feature"}',
        },
        INVARIANTS,
    ),
    # Liveness is slow in TLC (~90 us per graph node), so the default splits the events.
    "live-save": (
        "SpecUser",
        {**LIVE, "Conds": '{"none", "hot"}', "Events": '{"save"}'},
        WAY_OUT,
    ),
    # A recipe this iPhone declines (a capability, a protocol floor, a schema): each screen
    # has its way out. With an editor Save: --full (live-save-unsupported, live-save-outdated).
    "live-declines": (
        "SpecUser",
        {**LIVE, "Conds": DECLINES, "Events": "{}"},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # Stop with the declines is about 100 s, under --full (live-stop-declines).
    "live-stop": (
        "SpecUser",
        {**LIVE, "Conds": SCOPED, "Events": '{"stop"}'},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # Kills, a day passing (the reaper's timed_out) and backgrounding, with a patient user.
    "live-faults": (
        "SpecUser",
        {**LIVE, "Conds": ALL_CONDS, "Events": '{"kill", "reaper", "background"}'},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # Nobody taps anything: the reaper's timed_out is re-pinned when the phone opens the
    # project (R _auto_resume_reaped), so no needs-attention screen stays up.
    "live-reaped-no-user": (
        "Spec",
        {**LIVE, "Conds": '{"none", "hot"}', "Events": '{"kill", "reaper"}'},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # The editor opened from the gallery: no chat poll underneath, so a failed status read
    # is re-read only by its own scheduled retry (S scheduleRetry).
    "live-gallery-editor": (
        "SpecUser",
        {**LIVE, "Chat": "{FALSE}", "Conds": '{"none", "hot"}', "Events": '{"save"}'},
        [("PROPERTY", "PreviewConverges")],
    ),
    # "Without anyone tapping anything": no user fairness, every condition.
    "agree": (
        "Spec",
        {**LIVE, "Conds": ALL_CONDS, "Events": '{"stop", "kill"}'},
        [("PROPERTY", "ClientServerAgree")],
    ),
    # The capability can go also mid-render (the export of this request keeps going).
    "live-lost-capability": (
        "SpecUser",
        {**LIVE, "Conds": '{"none"}', "Events": '{"caps"}'},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # The gallery editor with a declined recipe: "Review in chat" opens the edit's own
    # conversation (it used to go back to the video detail: no chat, no render card).
    "live-gallery-declines": (
        "SpecUser",
        {
            **LIVE,
            "Chat": "{FALSE}",
            "Conds": '{"unsupported", "outdated"}',
            "Events": '{"save"}',
        },
        WAY_OUT,
    ),
    # After "Update Kria" (once possibly to a build that is newer but still too old), the
    # render finishes with no further tap: the server re-pins for the updated build. (A
    # schema gap the server cannot see takes one "Try again", renders_locally: live-save-outdated.)
    "live-update-no-tap": (
        "SpecUpdate",
        {**LIVE, "Conds": '{"outdated"}', "Events": "{}"},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # An operator lowers the recipe's floor for the build that declined: its Try again works.
    "live-rollback": (
        "SpecUser",
        {**LIVE, "Conds": '{"outdated"}', "Events": '{"rollback"}'},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # A later save brings a newer schema after this iPhone rendered the first request: the
    # kept video's card (localReady) offers Update Kria and Review in chat.
    "live-new-schema": (
        "SpecUser",
        {**LIVE, "Conds": '{"newschema"}', "Events": '{"feature"}'},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # Nobody taps: a capable phone with the chat open gets the server's request rendered, also
    # after another device saved (superseded is not settled) or an older one declined.
    "live-capable-renders": (
        "Spec",
        {**LIVE, "Conds": '{"none", "hot"}', "Events": '{"other", "old"}'},
        [("PROPERTY", "CapableRenders")],
    ),
    "agree-lost-capability": (
        "Spec",
        {**LIVE, "Conds": '{"none"}', "Events": '{"caps", "kill"}'},
        [("PROPERTY", "ClientServerAgree")],
    ),
}
# --full: the heavier combinations the default leaves out.
FULL_RUNS = {
    "one-save-declines-stop": (
        "Spec",
        {**ONE_SAVE, "Conds": DECLINES, "Events": '{"save", "stop", "kill", "reaper"}'},
        INVARIANTS,
    ),
    "lost-capability-stop": (
        "Spec",
        {**ONE_SAVE, "Conds": '{"none"}', "Events": '{"caps", "save", "stop", "kill"}'},
        INVARIANTS,
    ),
    "two-saves-all-conds": ("Spec", {**TWO_SAVES, "Conds": ALL_CONDS}, INVARIANTS),
    # A recipe this iPhone declines: "Review in chat" leads to a version it renders (the
    # unsupported-recipe dead end, RESIDUAL before round 2).
    "live-save-unsupported": (
        "SpecUser",
        {**LIVE, "Conds": '{"unsupported"}', "Events": '{"save"}'},
        WAY_OUT,
    ),
    "live-stop-declines": (
        "SpecUser",
        {**LIVE, "Conds": DECLINES, "Events": '{"stop"}'},
        [("PROPERTY", "AttentionHasWayOut")],
    ),
    # An outdated build: "Update Kria", then the server re-pins for the newer build.
    "live-save-outdated": (
        "SpecUser",
        {**LIVE, "Conds": '{"outdated", "schema"}', "Events": '{"save"}'},
        WAY_OUT,
    ),
    # builds-stop (every build event with Save and Stop) ran past 30 minutes, so it is split
    # by event; each finishes in a few minutes. An older device together with Save AND Stop
    # (about 120M states and growing at 20 minutes) is not run: old+save and old+stop are.
    "builds-stop-feature-save": (
        "Spec",
        {**ONE_SAVE, "Conds": BUILD_CONDS, "Events": '{"feature", "save", "stop"}'},
        INVARIANTS,
    ),
    "builds-stop-rollback-save": (
        "Spec",
        {**ONE_SAVE, "Conds": BUILD_CONDS, "Events": '{"rollback", "save", "stop"}'},
        INVARIANTS,
    ),
    "builds-stop-no-save": (
        "Spec",
        {
            **ONE_SAVE,
            "Conds": BUILD_CONDS,
            "Events": '{"old", "feature", "rollback", "stop"}',
        },
        INVARIANTS,
    ),
    "builds-old-save": (
        "Spec",
        {**ONE_SAVE, "Conds": BUILD_CONDS, "Events": '{"old", "save"}'},
        INVARIANTS,
    ),
    # Save with Stop (about 6 minutes). One answered preview poll: PreviewConverges needs
    # two (a newer generation read twice), so it is checked in the Save runs instead.
    # Save with a missing original is past TLC's 15-minute limit for liveness; live-stop
    # and live-faults cover a missing original, the safety runs cover it with Save.
    "live-save-stop": (
        "SpecUser",
        {
            **LIVE,
            "MaxTicks": "1",
            "Conds": '{"none", "hot"}',
            "Events": '{"save", "stop"}',
        },
        [("PROPERTY", "AttentionHasWayOut"), ("PROPERTY", "ClientServerAgree")],
    ),
    # One save in all, from this editor or another device (two saves with liveness ran
    # past TLC's 15-minute limit here).
    "live-save-other": (
        "SpecUser",
        {
            **LIVE,
            "Conds": '{"none", "unsupported"}',
            "Events": '{"save", "other"}',
        },
        WAY_OUT,
    ),
    "live-save-faults": (
        "SpecUser",
        {
            **LIVE,
            "Conds": '{"none", "outdated"}',
            "Events": '{"save", "kill", "reaper", "background", "sync"}',
        },
        WAY_OUT,
    ),
}
PASSING = sorted({rule for _, _, checks in MODEL_RUNS.values() for _, rule in checks})

# Rules today's code still breaks, each in a scenario scoped out of MODEL_RUNS. Empty: the
# unsupported-recipe dead end is fixed (findings.md "Fixed on this branch"). A new real
# finding goes here as rule -> (kind, specification, constants).
RESIDUAL: dict[str, tuple[str, str, dict[str, str]]] = {}

# The old code (PhoneRenderBefore.tla) broke each of these; the fixes made them pass.
# Same constants as the old findings run (approval routed retries then). The old module
# has no Chat constant, and its conditions and events are the old ones.
OLD_BASE = {"MaxFaults": "1", "MaxTicks": "1"}
OLD_EVENTS = '{"save", "other", "stop", "kill", "background", "reaper", "sync"}'
OLD_CONDS = '{"none", "hot", "unsupported", "missing"}'
OLD_LIVE = {
    **OLD_BASE,
    "Conds": OLD_CONDS,
    "Approvals": "{TRUE, FALSE}",
    "MaxSaves": "1",
    "MaxId": "3",
    "Events": '{"save", "stop"}',
}
OLD_ONE_SAVE = {
    **OLD_BASE,
    "Conds": OLD_CONDS,
    "Approvals": "{TRUE, FALSE}",
    "MaxSaves": "1",
    "MaxId": "3",
    "Events": OLD_EVENTS,
}
BEFORE_BREAKS = {
    "NoSpuriousAttention": ("INVARIANT", "Spec", OLD_ONE_SAVE),
    "CloudDecisionReported": ("INVARIANT", "Spec", OLD_ONE_SAVE),
    "AttentionHasWayOut": ("PROPERTY", "SpecUser", OLD_LIVE),
    "ClientServerAgree": ("PROPERTY", "Spec", OLD_LIVE),
    "PreviewConverges": ("PROPERTY", "SpecUser", OLD_LIVE),
}

# Teeth: each mutation puts one piece of the old behavior back in a copy of
# PhoneRender.tla; TLC must then break the rule in the given run. name -> (rule, edits,
# (kind, specification, constants)). Every passing rule has at least one.
ONE_SCOPED = ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": SCOPED})
LATE_LOST_CAPS = [
    (
        "ELSE IF gate \\in Declines /\\ ~resumesSync /\\ ~inFlight",
        "ELSE IF gate \\in Declines /\\ ~inFlight",
    ),
    (
        "/\\ decl' = SendDecline(id, gate)",
        "/\\ decl' = IF keeps THEN decl ELSE SendDecline(id, gate)",
    ),
]
# The old card: unsupported_recipe hid Try again (a structural reason) but had no change
# screen of its own.
NO_UNSUPPORTED_CHANGE = [
    (
        'ChangeReasons == {"unsupported", "requiresChange"}',
        'ChangeReasons == {"requiresChange"}',
    ),
    (
        'Structural == ChangeReasons \\cup {"outdated"}',
        'Structural == ChangeReasons \\cup {"outdated", "unsupported"}',
    ),
]
TEETH = {
    # complete only for the current identity in phase syncing.
    "complete-any-identity": (
        "NoStalePublish",
        [
            (
                'CompleteOK == sId = coord.id /\\ sPhase = "syncing"',
                'CompleteOK == sPhase \\in {"awaiting", "syncing"}',
            )
        ],
        ONE_SCOPED,
    ),
    # V:626 mark_device_failed only from awaiting/syncing (409 when published).
    "fail-a-published-record": (
        "PublishedStaysPublished",
        [
            (
                'FailableForReport == sPhase \\in {"awaiting", "syncing"}',
                'FailableForReport == sPhase # "attention"',
            )
        ],
        ONE_SCOPED,
    ),
    # P:320-328 a 409 on complete is "superseded", never "published".
    "late-complete-says-synced": (
        "SyncedMeansPublished",
        [
            (
                'LateComplete == [coord EXCEPT !.ph = "superseded"',
                'LateComplete == [coord EXCEPT !.ph = "synced"',
            )
        ],
        ONE_SCOPED,
    ),
    # S:346 / S:351 tickets[key] == ticket after the fetch.
    "no-ticket-guard": (
        "NoStaleRequestReinstalled",
        [("/\\ Apply(oldRec, FALSE)", "/\\ Apply(oldRec, TRUE)")],
        ONE_SCOPED,
    ),
    # S readFailed: a failed status read waits; the old code showed "couldn't prepare" (S1).
    "failed-read-shows-attention": (
        "NoSpuriousAttention",
        [
            (
                'ReadFailedPres ==\n  IF pres.ph = "synced" THEN pres',
                'ReadFailedPres ==\n  IF TRUE THEN [NoPres EXCEPT !.ph = "attention", !.cause = "prepFail"]',
            )
        ],
        ONE_SCOPED,
    ),
    # D changeReasons: before, a declined recipe read "needs your attention" with no button.
    "decline-has-no-change-screen": (
        "NoSpuriousAttention",
        NO_UNSUPPORTED_CHANGE,
        ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": '{"unsupported"}', "Events": "{}"}),
    ),
    # EC banner by nextStep: before, an outdated build got "Retry device render".
    "outdated-banner-offers-retry": (
        "NoSpuriousAttention",
        [
            (
                '             ELSE IF NeedsUpdate(p) THEN "update"\n             ELSE IF p.ph = "cancelled"',
                '             ELSE IF p.ph = "cancelled"',
            )
        ],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"outdated"}', "Events": '{"save"}'},
        ),
    ),
    # S sendDecline; the old code returned before telling the server.
    "decline-not-sent": (
        "CloudDecisionReported",
        [("/\\ decl' = SendDecline(id, gate)", "/\\ decl' = decl")],
        ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": '{"unsupported"}', "Events": "{}"}),
    ),
    # S decline keeps a finished export: before, it neither uploaded nor told the server.
    "lost-capability-kept-silently": (
        "CloudDecisionReported",
        LATE_LOST_CAPS,
        ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": '{"none"}', "Events": '{"caps"}'}),
    ),
    # S sendDecline retries until the server answers; a dropped report is never re-sent.
    "failed-decline-dropped": (
        "CloudDecisionReported",
        [
            (
                "/\\ faults' = faults - 1 /\\ decl' = decl",
                "/\\ faults' = faults - 1 /\\ decl' = NoDecl",
            )
        ],
        ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": '{"unsupported"}', "Events": "{}"}),
    ),
    # R _auto_resume_reaped: without it the reaper's timed_out waits for a tap.
    "reaped-not-auto-resumed": (
        "AttentionHasWayOut",
        [
            (
                'LET reaped == sPhase = "attention" /\\ sReason = "timedOut" /\\ reapedId = sId',
                "LET reaped == FALSE",
            )
        ],
        (
            "PROPERTY",
            "Spec",
            {**LIVE, "Conds": '{"none"}', "Events": '{"kill", "reaper"}'},
        ),
    ),
    # A declined recipe with no change screen has no button and no way out.
    "decline-dead-end": (
        "AttentionHasWayOut",
        NO_UNSUPPORTED_CHANGE,
        ("PROPERTY", "SpecUser", {**LIVE, "Conds": '{"unsupported"}', "Events": "{}"}),
    ),
    # C:354-366 a report is re-sent until the server takes it; the old one was dropped.
    "failed-report-dropped": (
        "ClientServerAgree",
        [
            (
                "/\\ faults' = faults - 1 /\\ rep' = rep",
                "/\\ faults' = faults - 1 /\\ rep' = NoRep",
            )
        ],
        ("PROPERTY", "Spec", {**LIVE, "Conds": '{"none"}', "Events": '{"stop"}'}),
    ),
    # "Ready on this iPhone, can't sync from this iPhone yet" while the server waits.
    "lost-capability-contradicts": (
        "ClientServerAgree",
        LATE_LOST_CAPS,
        (
            "PROPERTY",
            "Spec",
            {**LIVE, "Conds": '{"none"}', "Events": '{"caps", "kill"}'},
        ),
    ),
    # E:7666-7682 follow a newer generation read twice; the old poll waited it out.
    "newer-generation-not-followed": (
        "PreviewConverges",
        [("THEN IF e.seen + 1 >= 2 THEN Saved(e)", "THEN IF FALSE THEN Saved(e)")],
        ("PROPERTY", "SpecUser", {**LIVE, "Conds": '{"none"}', "Events": '{"save"}'}),
    ),
    # S rereads: the editor's refresh while the chat poll holds the ticket is dropped; the
    # old request installs, turns superseded (settled), and nothing reads the new one.
    "lost-reread": (
        "PreviewConverges",
        [
            (
                "ELSE UNCHANGED rec /\\ ed' = [e EXCEPT !.pc = \"after\"] /\\ again' = Again1(again, e.rflag)",
                'ELSE UNCHANGED rec /\\ ed\' = [e EXCEPT !.pc = "after"] /\\ UNCHANGED again',
            )
        ],
        ("PROPERTY", "SpecUser", {**LIVE, "Conds": '{"none"}', "Events": '{"save"}'}),
    ),
    # S wait/scheduleRetry: a failed status read re-reads on its own (the gallery editor
    # has no chat poll to do it).
    "failed-read-not-re-read": (
        "PreviewConverges",
        [
            (
                "/\\ sched' = TRUE\n       /\\ UNCHANGED <<coord, req, obs, hiReq, rep, decl, sPaused, faults, upd426>>",
                "/\\ sched' = sched\n       /\\ UNCHANGED <<coord, req, obs, hiReq, rep, decl, sPaused, faults, upd426>>",
            )
        ],
        (
            "PROPERTY",
            "SpecUser",
            {**LIVE, "Chat": "{FALSE}", "Conds": '{"none"}', "Events": '{"save"}'},
        ),
    ),
    # ---- round 3: one mutation per fix --------------------------------------------------
    # R requires_update (both callers of the same server check): an older build's Try
    # again re-pins an outdated recipe this iPhone declines again.
    "server-requires-update-removed": (
        "NoFutileRepin",
        [
            (
                '           /\\ ~(sReason = "outdated" /\\ lvl <= sDecl /\\ ~Asserts)\n',
                "",
            ),
            (
                'IF sReason # "unsupported" /\\ ~(sReason = "outdated" /\\ OLD <= sDecl)',
                'IF sReason # "unsupported"',
            ),
        ],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"outdated"}', "Events": '{"old"}'},
        ),
    ),
    # R requires_change for unsupported_recipe (both callers): a re-pin of the same recipe
    # for a phone that lacks the capability.
    "server-requires-change-removed": (
        "NoFutileRepin",
        [
            (
                'RetryOK == /\\ sPhase = "attention" /\\ sReason # "unsupported"',
                'RetryOK == /\\ sPhase = "attention"',
            ),
            (
                'IF sReason # "unsupported" /\\ ~(sReason = "outdated" /\\ OLD <= sDecl)',
                'IF ~(sReason = "outdated" /\\ OLD <= sDecl)',
            ),
        ],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"unsupported"}', "Events": '{"old"}'},
        ),
    ),
    # S isSettled: superseded counted as settled again. The chat poll stops on the old
    # request and nothing reads the one another device pinned.
    "settled-counts-superseded": (
        "CapableRenders",
        [
            (
                'Settled == pres.ph = "synced"',
                'Settled == pres.ph \\in {"synced", "superseded"}',
            )
        ],
        ("PROPERTY", "Spec", {**LIVE, "Conds": '{"none"}', "Events": '{"other"}'}),
    ),
    # D needsChange with a quiet wait: a failed read on the change card reads "needs your
    # attention" with no way forward.
    "quiet-wait-hides-change": (
        "NoSpuriousAttention",
        [
            (
                'NeedsChange(p) == p.ph \\in YourTurnPh /\\ p.wait \\in {"none", "quiet"}',
                'NeedsChange(p) == p.ph \\in YourTurnPh /\\ p.wait = "none"',
            )
        ],
        ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": '{"unsupported"}', "Events": "{}"}),
    ),
    # R _auto_resume_updated removed: after "Update Kria" the render waits for a tap.
    "auto-resume-updated-removed": (
        "AttentionHasWayOut",
        [
            (
                'updated == sPhase = "attention" /\\ sReason = "outdated" /\\ sDecl > 0 /\\ lvl > sDecl /\\ sFloor',
                "updated == FALSE",
            )
        ],
        ("PROPERTY", "SpecUpdate", {**LIVE, "Conds": '{"outdated"}', "Events": "{}"}),
    ),
    # R gates before the auto re-pin: a newer but still insufficient build is re-pinned and
    # declines again.
    "floors-checked-after-repin": (
        "NoFutileRepin",
        [
            (
                "auto == Floors /\\ (reaped \\/ updated) /\\ sId < MaxId",
                "auto == (reaped \\/ updated) /\\ sId < MaxId",
            )
        ],
        ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": '{"outdated"}', "Events": "{}"}),
    ),
    # R held_by_newer_build: an older device's decline fails the record a newer one holds;
    # the newer one re-pins, the older one declines again.
    "old-decline-not-held": (
        "NoRepinLoop",
        [
            (
                'HeldDecline(reason, from) == reason = "outdated" /\\ (sPhase = "syncing" \\/ from < sRead)',
                "HeldDecline(reason, from) == FALSE",
            )
        ],
        ("INVARIANT", "Spec", {**ONE_SAVE, "Conds": '{"none"}', "Events": '{"old"}'}),
    ),
    # S readFailed .updateRequired: a finished video of an older request hides a 426 about
    # the newer one, so the server is never told.
    "update-required-ignores-identity": (
        "UpdateTold",
        [("ELSE IF HoldsFinished /\\ id = req", "ELSE IF HoldsFinished")],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"none"}', "Events": '{"feature"}'},
        ),
    ),
    # R renders_locally: the declining build that renders the recipe now (rollback) is told
    # requires_update; the card flips between Try again and Update Kria forever.
    "retry-ignores-renders-locally": (
        "AttentionHasWayOut",
        [
            (
                '           /\\ ~(sReason = "outdated" /\\ lvl <= sDecl /\\ ~Asserts)\n',
                '           /\\ ~(sReason = "outdated" /\\ lvl <= sDecl)\n',
            )
        ],
        (
            "PROPERTY",
            "SpecUser",
            {**LIVE, "Conds": '{"outdated"}', "Events": '{"rollback"}'},
        ),
    ),
    # AppModel openCreationThread: the gallery editor's "Review in chat" went back to the
    # video detail (no conversation, no render card).
    "gallery-review-goes-back": (
        "AttentionHasWayOut",
        [
            (
                "  /\\ chat' = TRUE /\\ ed' = EdIdle\n",
                "  /\\ chat' = chat /\\ ed' = ed\n",
            )
        ],
        (
            "PROPERTY",
            "SpecUser",
            {
                **LIVE,
                "Chat": "{FALSE}",
                "Conds": '{"unsupported"}',
                "Events": '{"save"}',
            },
        ),
    ),
    # S `.decline where inFlight`: a capability lost mid-render declines while the export
    # goes on and publishes.
    "decline-while-exporting": (
        "AttentionHasWayOut",
        [
            (
                "ELSE IF gate \\in Declines /\\ ~resumesSync /\\ ~inFlight",
                "ELSE IF gate \\in Declines /\\ ~resumesSync",
            )
        ],
        ("PROPERTY", "SpecUser", {**LIVE, "Conds": '{"none"}', "Events": '{"caps"}'}),
    ),
    # S resumesSync counts synced: a stale "awaiting" read declines a published video, and a
    # stale "published" read then settles the card while the change it led to waits.
    "stale-read-declines-synced": (
        "AttentionHasWayOut",
        [
            (
                'c1.ph \\in {"localReady", "syncing", "synced"}',
                'c1.ph \\in {"localReady", "syncing"}',
            )
        ],
        ("PROPERTY", "SpecUser", {**LIVE, "Conds": '{"none"}', "Events": '{"caps"}'}),
    ),
    # (Round 3's kept-video-no-update is gone: after round 4 a kept video with an outdated
    # reason is unreachable, see findings.md Round 4.)
    # S apply (needs_attention): "Ready on this iPhone" whenever a receipt on disk has a file,
    # also an earlier request's export cancelled when the newer request arrived.
    "kept-video-other-request": (
        "KeptIsThisRequest",
        [
            (
                'ph |-> IF c1.file /\\ c1.id = id THEN "localReady"',
                'ph |-> IF c1.file THEN "localReady"',
            )
        ],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"newschema"}', "Events": '{"feature"}'},
        ),
    ),
    # S apply: renderable_now never replaces renderer_outdated (round-3 M19): a build the
    # rollback made current keeps "Update Kria".
    "no-renderable-now": (
        "UpdateMeansOutdated",
        [
            (
                'reason == IF r.resp.reason = "outdated" /\\ gate # "outdated"',
                "reason == IF FALSE",
            )
        ],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"outdated"}', "Events": '{"rollback"}'},
        ),
    ),
    # S apply: only a local gate replaces renderer_outdated (round 3); with a missing
    # capability the card keeps "Update Kria" instead of asking for a change.
    "outdated-kept-for-unsupported": (
        "UpdateMeansOutdated",
        [
            (
                'THEN IF gate = "unsupported" THEN "unsupported" ELSE "renderable"',
                'THEN IF gate = "unsupported" THEN "outdated" ELSE "renderable"',
            )
        ],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"outdated"}', "Events": '{"rollback", "caps"}'},
        ),
    ),
    # S retryNeedsAttention: requires_update always reads "Update Kria", also on a build
    # whose gate has no version gap and lacks a capability.
    "requires-update-ignores-gate": (
        "UpdateMeansOutdated",
        [
            (
                '!.cause = IF Gate = "unsupported" THEN "unsupported" ELSE "outdated"]',
                '!.cause = "outdated"]',
            )
        ],
        (
            "INVARIANT",
            "Spec",
            {**ONE_SAVE, "Conds": '{"outdated"}', "Events": '{"rollback", "caps"}'},
        ),
    ),
}


def tlc_jar() -> Path:
    if os.environ.get("TLA2TOOLS"):
        jar = Path(os.environ["TLA2TOOLS"])
    elif (LOCAL_TOOLS / "tla2tools.jar").exists():
        jar = LOCAL_TOOLS / "tla2tools.jar"
    else:
        jar = OUT / "tla2tools.jar"
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {TLC_URL}")
        urllib.request.urlretrieve(TLC_URL, jar)  # noqa: S310 - pinned URL + sha256 below
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    if digest != TLC_SHA256:
        sys.exit(f"FAIL: {jar} sha256 {digest} is not TLC v1.8.0 ({TLC_SHA256})")
    return jar


def java() -> str:
    local = LOCAL_TOOLS / "jdk-21.0.8+9-jre/Contents/Home/bin/java"
    found = os.environ.get("JAVA") or (
        str(local) if local.exists() else shutil.which("java")
    )
    if not found or subprocess.run([found, "-version"], capture_output=True).returncode:
        sys.exit(
            "FAIL: no Java runtime. Install one (brew install openjdk@21) and set JAVA."
        )
    return found


def tlc(
    spec: Path,
    constants: dict[str, str],
    checks: list[tuple[str, str]],
    specification: str,
    tag: str,
    expect_pass: bool = False,
    exit_on_timeout: bool = True,
) -> dict:
    """Run TLC once. Returns {"broken": [rules], "trace": n, "states": n, "seconds": s}.

    A run expected to pass checks liveness once, on the complete graph; one expected to
    break keeps TLC's periodic checks so a violation surfaces early. A run past
    TLC_TIMEOUT is a FAIL: it exits with a FAIL line, or (exit_on_timeout=False) returns
    {"timeout": True} so the caller reports it and goes on."""
    log = OUT / f"{tag}.log"
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / "run.cfg"
        lines = [f"SPECIFICATION {specification}", "CONSTANTS"]
        lines += [f"  {k} = {v}" for k, v in constants.items()]
        lines += [f"{kind} {rule}" for kind, rule in checks]
        cfg.write_text("\n".join(lines) + "\n")
        shutil.copy(spec, Path(tmp) / spec.name)
        start = time.time()
        try:
            run = subprocess.run(
                [
                    JAVA,
                    "-XX:+UseParallelGC",
                    "-cp",
                    str(JAR),
                    "tlc2.TLC",
                    "-workers",
                    "auto",
                    *(["-lncheck", "final"] if expect_pass else []),
                    "-deadlock",
                    "-metadir",
                    str(Path(tmp) / "states"),
                    "-config",
                    str(cfg),
                    spec.name,
                ],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=TLC_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            log.write_text(partial)
            if exit_on_timeout:
                sys.exit(f"FAIL: TLC timed out after {TLC_TIMEOUT}s ({tag}); see {log}")
            return {
                "broken": [],
                "timeout": True,
                "trace": 0,
                "states": 0,
                "seconds": round(time.time() - start, 1),
                "log": str(log.relative_to(REPO)),
            }
        seconds = round(time.time() - start, 1)
    log.write_text(run.stdout + run.stderr)
    broken: list[str] = []
    m = re.search(r"Invariant (\w+) is violated", run.stdout)
    if m:
        broken = [m.group(1)]
    m = re.search(
        r"Temporal propert(?:y|ies) (.+?) (?:is|was|were) violated", run.stdout
    )
    if m:
        broken = re.findall(r"\w+", m.group(1).replace(" and ", " "))
    if not broken and "No error has been found" not in run.stdout:
        sys.exit(f"FAIL: TLC did not finish ({tag}); see {log}")
    states = re.findall(r"([\d,]+) distinct states found", run.stdout)
    return {
        "broken": broken,
        "trace": len(re.findall(r"^State \d+: ", run.stdout, re.M)),
        "states": int(states[-1].replace(",", "")) if states else 0,
        "seconds": seconds,
        "log": str(log.relative_to(REPO)),
    }


# ---------------------------------------------------------------- drift


def swift_functions(text: str, name: str) -> list[str]:
    """Every `func name` body (brace matched) in a Swift file."""
    out = []
    for m in re.finditer(r"\bfunc\s+`?" + re.escape(name) + r"`?\s*[(<]", text):
        start = text.rfind("\n", 0, m.start()) + 1
        brace = text.find("{", m.end())
        if brace < 0:
            continue
        depth, i = 0, brace
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(text[start : i + 1])
    return out


def python_functions(text: str, name: str) -> list[str]:
    tree = ast.parse(text)
    source = text.splitlines()
    return [
        "\n".join(source[n.lineno - 1 : n.end_lineno])
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]


def function_hashes() -> dict[str, str]:
    coverage = json.loads((HERE / "coverage.json").read_text())
    out = {}
    for rel, names in coverage["functions"].items():
        text = (REPO / rel).read_text()
        for name in names:
            bodies = (python_functions if rel.endswith(".py") else swift_functions)(
                text, name
            )
            if not bodies:
                sys.exit(
                    f"DRIFT: {rel}:{name} no longer exists; update the model and coverage.json"
                )
            out[f"{rel}:{name}"] = hashlib.sha256(
                "\n".join(bodies).encode()
            ).hexdigest()[:16]
    return out


# ---------------------------------------------------------------- explore


def explore() -> dict:
    """Run every residual rule per starting scenario and event set: how many ways break."""
    event_sets = {
        "faults only": "{}",
        "save": '{"save"}',
        "stop": '{"stop"}',
        "background": '{"background"}',
        "kill+reaper": '{"kill", "reaper"}',
        "save+other device": '{"save", "other"}',
        "draft sync": '{"sync"}',
    }
    results = {}
    for rule, (kind, specification, consts) in RESIDUAL.items():
        rows = []
        for ev_name, events in event_sets.items():
            for cond in ("none", "hot", "unsupported", "missing"):
                c = {**consts, "Events": events, "Conds": f'{{"{cond}"}}'}
                if ev_name == "save+other device":
                    c.update(MaxSaves="2", MaxId="4")
                tag = f"explore-{rule}-{ev_name.replace(' ', '_').replace('+', '_')}-{cond}"
                r = tlc(SPEC, c, [(kind, rule)], specification, tag)
                rows.append(
                    {
                        "events": ev_name,
                        "cond": cond,
                        "broken": rule in r["broken"],
                        "trace": r["trace"],
                    }
                )
        broken = [r for r in rows if r["broken"]]
        results[rule] = {
            "scenarios_checked": len(rows),
            "scenarios_broken": len(broken),
            "broken_conditions": sorted({r["cond"] for r in broken}),
            "shortest_trace": min((r["trace"] for r in broken), default=None),
            "rows": rows,
        }
        print(
            f"explore {rule}: broken in {len(broken)}/{len(rows)} scenarios, "
            f"conditions {results[rule]['broken_conditions']}"
        )
    (OUT / "explore.json").write_text(json.dumps(results, indent=2) + "\n")
    return results


def run_model(runs: dict, report: dict) -> None:
    timed_out = []
    for name, (specification, consts, checks) in runs.items():
        r = tlc(
            SPEC,
            consts,
            checks,
            specification,
            f"model-{name}",
            expect_pass=True,
            exit_on_timeout=False,
        )
        report[name] = r
        rules = ", ".join(rule for _, rule in checks)
        if r.get("timeout"):
            timed_out.append(name)
            print(
                f"FAIL model, {name}: TLC TIMED OUT after {TLC_TIMEOUT}s [{rules}]",
                flush=True,
            )
            continue
        print(
            f"model, {name}: "
            + ("holds" if not r["broken"] else f"BROKEN {r['broken']}")
            + f" [{rules}] ({r['states']:,} states, {r['seconds']}s)",
            flush=True,
        )
        if r["broken"]:
            sys.exit(f"FAIL: the code model breaks {r['broken']}; see {r['log']}")
    if timed_out:
        sys.exit(
            f"FAIL: TLC timed out in {timed_out}; split their events or conditions"
        )


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # progress shows in a redirected log
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {}

    hashes = function_hashes()
    coverage = json.loads((HERE / "coverage.json").read_text())
    if "--resync" in sys.argv:
        coverage["hashes"] = hashes
        coverage["sync_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True
        ).stdout.strip()
        (HERE / "coverage.json").write_text(json.dumps(coverage, indent=2) + "\n")
        print(
            "resynced coverage.json; commit it only after re-reading the model against the code"
        )
    drift = sorted(
        k for k, v in hashes.items() if coverage.get("hashes", {}).get(k) != v
    )
    report["drift"] = drift
    if drift:
        print(
            "DRIFT (review, not a bug): these covered functions changed since "
            f"{coverage.get('sync_commit')}:\n  " + "\n  ".join(drift)
        )

    happy = {
        **BASE,
        "MaxFaults": "0",
        "MaxSaves": "1",
        "MaxId": "3",
        "Conds": '{"none"}',
        "Events": "{}",
    }
    r = tlc(SPEC, happy, [("INVARIANT", "NeverPublished")], "Spec", "happy-publishes")
    if r["broken"] != ["NeverPublished"]:
        sys.exit(
            "FAIL: with no faults the model never publishes a video; the model is wrong"
        )
    every = INVARIANTS + [
        ("PROPERTY", rule) for rule in ("AttentionHasWayOut", "PreviewConverges")
    ]
    r = tlc(SPEC, {**happy, "MaxTicks": "2"}, every, "SpecUser", "happy-rules")
    if r["broken"]:
        sys.exit(
            f"FAIL: {r['broken']} break on the happy path; the model is wrong ({r['log']})"
        )
    r = tlc(SPEC, happy, [("PROPERTY", "ClientServerAgree")], "Spec", "happy-agree")
    if r["broken"]:
        sys.exit(
            f"FAIL: ClientServerAgree breaks on the happy path; the model is wrong ({r['log']})"
        )
    report["happy_path"] = "publishes; every rule holds"
    print("happy path (no faults, no events): publishes, every rule holds")

    model: dict[str, object] = {}
    run_model(MODEL_RUNS, model)
    if "--full" in sys.argv:
        run_model(FULL_RUNS, model)
    report["model"] = model

    residual = {}
    for rule, (kind, specification, consts) in RESIDUAL.items():
        r = tlc(SPEC, consts, [(kind, rule)], specification, f"residual-{rule}")
        residual[rule] = r
        print(
            f"residual {rule}: "
            + (
                f"breaks (trace of {r['trace']} states, {r['seconds']}s)"
                if rule in r["broken"]
                else "HOLDS"
            )
        )
    report["residual"] = residual
    held = sorted(rule for rule, r in residual.items() if rule not in r["broken"])
    if held:
        sys.exit(
            f"FAIL: {held} no longer break in their RESIDUAL scenario. If the code was "
            "fixed, move them from RESIDUAL to PASSING (widen their runs), add teeth, rerun."
        )

    before = {}
    for rule, (kind, specification, consts) in BEFORE_BREAKS.items():
        r = tlc(BEFORE, consts, [(kind, rule)], specification, f"before-{rule}")
        before[rule] = r
        print(
            f"before the fix, {rule}: "
            + (
                f"breaks (trace of {r['trace']} states, {r['seconds']}s)"
                if rule in r["broken"]
                else "HOLDS"
            )
        )
    report["before"] = before
    held = sorted(rule for rule, r in before.items() if rule not in r["broken"])
    if held:
        sys.exit(
            f"FAIL: PhoneRenderBefore.tla should break {held} (the old code's findings)"
        )

    teeth = {}
    for name, (rule, edits, (kind, specification, consts)) in TEETH.items():
        text = SPEC.read_text()
        for old, new in edits:
            if text.count(old) != 1:
                sys.exit(
                    f"FAIL: mutation {name} no longer matches PhoneRender.tla: {old!r}"
                )
            text = text.replace(old, new)
        mutant = OUT / "mutant" / "PhoneRender.tla"
        mutant.parent.mkdir(exist_ok=True)
        mutant.write_text(text)
        r = tlc(mutant, consts, [(kind, rule)], specification, f"teeth-{name}")
        teeth[name] = rule in r["broken"]
        print(
            f"teeth, {name} ({rule}): "
            + ("caught" if teeth[name] else "NOT CAUGHT")
            + f" ({r['seconds']}s)"
        )
        if not teeth[name]:
            sys.exit(f"FAIL: {rule} does not catch {name}, so it proves nothing there")
    missing = sorted(set(PASSING) - {rule for rule, _, _ in TEETH.values()})
    if missing:
        sys.exit(f"FAIL: passing rules without teeth: {missing}")
    report["teeth"] = teeth

    if "--explore" in sys.argv:
        report["explore"] = {
            k: {kk: vv for kk, vv in v.items() if kk != "rows"}
            for k, v in explore().items()
        }

    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(
        "PASS"
        + (
            f" ({len(RESIDUAL)} residual findings recorded)"
            if RESIDUAL
            else " (no residual findings)"
        )
        + (" with DRIFT to review" if drift else "")
    )
    sys.exit(3 if drift else 0)


if __name__ == "__main__":
    JAR = tlc_jar()
    JAVA = java()
    main()
