"""Replay/live eval gate for nova.creator.main."""

import re
from pathlib import Path

import pytest

from .runners.eval_runner import discover_fixtures, load_fixture, run_eval

AGENT_DIR = "main_creator"
FIXTURE_PATHS = discover_fixtures(AGENT_DIR)


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda path: path.stem)
def test_main_creator_eval(
    fixture_path: Path,
    eval_mode: str,
    with_judge: bool,
    judge_for,
    live_model_client,
    live_input_normalizer,
    shadow_prompts_dir,
) -> None:
    fixture = load_fixture(fixture_path)
    judge = judge_for(fixture.agent) if with_judge else None
    result = run_eval(
        fixture,
        model_client=live_model_client if eval_mode == "live" else None,
        judge=judge,
        shadow_prompts_dir=shadow_prompts_dir,
        live_input_normalizer=live_input_normalizer,
    )
    assert result.passed, f"{result.summary()}: {result.structural_failures}"

    copy_meta = fixture.meta.get("creative_copy_decision")
    if copy_meta:
        assert result.output is not None
        decision = result.output.get("creative_decision")
        assert decision is not None, "creative copy metadata must survive MainCreator parsing"
        for field in ("target", "status"):
            assert decision[field] == copy_meta[field]
        for field in ("proposed_text", "source_evidence"):
            if field in copy_meta:
                assert decision.get(field) == copy_meta[field]

    if fixture.meta.get("phone_original_audio"):
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        assert action["strategy"]["render_program"] == "guided"
        assert action["strategy"]["montage_audio"]["preserve_source_audio"] is True
        assert action["strategy"]["montage_audio"]["source_media_ids"] == []

    pinned = fixture.meta.get("pinned_texts")
    if pinned:
        # KRI-523: corner / whole-video text is `pinned_texts` (in stack order), never a
        # (<=10 s, centred) opening title; the creator's own words ground each pin.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        assert action["strategy"]["pinned_texts"] == pinned
        assert not action["strategy"].get("opening_title")
        from app.agents._schemas.creator_agent import CreatorRenderIntentEvidence
        from app.kria.planner import ground_pinned_texts
        from app.schemas.edit_proposal import PinnedText

        evidence = CreatorRenderIntentEvidence.model_validate(action["render_intent_evidence"])
        _kept, dropped = ground_pinned_texts(
            [PinnedText(**pin) for pin in pinned],
            evidence=evidence.pinned_texts or "",
            user_sources=[fixture.input["user_message"]],
        )
        assert dropped == 0
        # KRI-525: any seconds the pin carries must be numbers the creator wrote.
        from app.routes.creator_agent import _pin_range_is_grounded

        assert all(
            _pin_range_is_grounded(PinnedText(**pin), evidence.pinned_texts or "") for pin in pinned
        )

    if fixture.meta.get("manual_visual_removal"):
        assert result.output is not None
        assert result.output["action"]["kind"] == "ask_user", (
            "A strategy cannot remove manual media layers; it must not clear text instead"
        )

    voiceover_beats = fixture.meta.get("voiceover_reaction_beats")
    if voiceover_beats:
        # KRI-519: photos timed to an iPhone voiceover are reaction beats, never the
        # guided contract runtime v2 cannot run. Triggers compare as plain lowercase
        # words (the grounding module itself needs the render stack, absent on the
        # eval runner).
        def _words(text: object) -> list[str]:
            return re.findall(r"[a-z0-9]+", str(text or "").casefold())

        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert not strategy.get("execution_contract")
        beats = strategy.get("reaction_beats") or []
        for trigger, visual_id in voiceover_beats.items():
            assert any(
                _words(beat.get("trigger")) == _words(trigger)
                and beat.get("visual_id") == visual_id
                for beat in beats
            ), f"no beat shows {visual_id} on {trigger!r}: {beats}"

    expected = fixture.meta.get("text_intent")
    if expected:
        from app.agents._schemas.creator_agent import (
            CreativeStrategy,
            CreatorRenderIntentEvidence,
        )
        from app.routes.creator_agent import _apply_explicit_render_intent

        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["opening_title"] == expected["title"]
        assert strategy["text_color"] == expected["color"]
        assert strategy["target_duration_s"] <= 12
        evidence = CreatorRenderIntentEvidence.model_validate(action["render_intent_evidence"])
        validated = _apply_explicit_render_intent(
            CreativeStrategy.model_validate(strategy),
            fixture.input["creator_request"],
            render_intent_evidence=evidence,
        )
        assert validated.opening_title == expected["title"]
        assert validated.text_color == expected["color"]

    clip_intents_meta = fixture.meta.get("clip_intents")
    if clip_intents_meta:
        # Structural-only: the eval cassette ignores the rendered prompt (see
        # CassetteModelClient), so this fixture does not exercise
        # `settings.clip_intents_enabled` -- it pins that a model response
        # using the new open-vocabulary `clip_intents` shape (for a request
        # the coded sport/participant/score fields cannot express) still
        # parses under the current `CreativeStrategy` contract, and that no
        # per-clip answer or resolved assignment ever appears in raw model
        # output.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy.get("resolved_clip_intents") is None
        intents = strategy.get("clip_intents") or []
        assert sorted(intent["op"] for intent in intents) == sorted(clip_intents_meta["ops"])
        if eval_mode == "live":
            # Open vocabulary: the live model's wording varies. Live mode needs
            # `CLIP_INTENTS_ENABLED=true` in the env (the prompt teaches the
            # field only when the flag is on; off, the model asks instead).
            keywords = clip_intents_meta.get("live_keywords") or []
            for intent, keyword in zip(intents, keywords, strict=True):
                assert keyword.casefold() in intent["attribute"].casefold(), (
                    keyword,
                    intent["attribute"],
                )
        else:
            assert [intent["attribute"] for intent in intents] == clip_intents_meta["attributes"]
        for intent in intents:
            assert "assignments" not in intent
            assert "media_id" not in intent

    transcript_kinds = fixture.meta.get("transcript_label_kinds")
    if transcript_kinds:
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["execution_contract"] == "guided_voiceover_v1"
        intents = strategy.get("clip_intents") or []
        assert {intent.get("transcript_kind") for intent in intents} == set(transcript_kinds)
        assert all(intent.get("label_source") == "transcript" for intent in intents)
        assert all(intent["op"] == "label" and not intent.get("creator_text") for intent in intents)
        assert strategy.get("resolved_clip_intents") is None

    exact_copy = fixture.meta.get("exact_copy_intent")
    if exact_copy:
        from app.agents._schemas.creator_agent import (
            CreativeStrategy,
            CreatorRenderIntentEvidence,
        )
        from app.routes.creator_agent import _apply_explicit_render_intent

        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        evidence = CreatorRenderIntentEvidence.model_validate(action["render_intent_evidence"])
        grounded = _apply_explicit_render_intent(
            CreativeStrategy.model_validate(action["strategy"]),
            fixture.input["creator_request"],
            render_intent_evidence=evidence,
        )
        # Every exact copy field must survive the verbatim-evidence boundary.
        for field, value in exact_copy.items():
            assert getattr(grounded, field) == value, field

    sfx_intent = fixture.meta.get("sfx_intent")
    if sfx_intent:
        from app.agents._schemas.creator_agent import (
            CreativeStrategy,
            CreatorRenderIntentEvidence,
            ResolvedCreatorManifest,
        )
        from app.routes.creator_agent import (
            _apply_explicit_render_intent,
            _creator_sources,
            _grounded_excerpt,
        )

        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        evidence = CreatorRenderIntentEvidence.model_validate(
            action.get("render_intent_evidence") or {}
        )
        request = fixture.input.get("creator_request") or fixture.input["user_message"]
        if sfx_intent.get("grounded"):
            # The regex misreads negation; the model's grounded reading must decide.
            assert _grounded_excerpt(evidence, "licensed_sfx", _creator_sources(request, request))
        grounded = _apply_explicit_render_intent(
            CreativeStrategy.model_validate(action["strategy"]),
            request,
            manifest=ResolvedCreatorManifest.model_validate(fixture.input["capability_manifest"]),
            latest_user_message=fixture.input["user_message"],
            render_intent_evidence=evidence,
        )
        effect_id = grounded.licensed_sfx.effect_id if grounded.licensed_sfx else None
        if "effect_words" in sfx_intent:
            from app.services.sfx_catalog import content_words

            # A described effect stays the creator's words here; the planning
            # turn matches those same content words against the live library.
            assert effect_id is not None
            assert content_words(effect_id) == content_words(sfx_intent["effect_words"])
        else:
            assert effect_id == sfx_intent["effect_id"]

    brief_meta = fixture.meta.get("brief_updates")
    if brief_meta:
        # KRI-188: with the Creative Brief on, every requirement the message
        # states comes back as a typed update (kind/scope), literal only for
        # creator-written text, and the plan itself is still a normal strategy.
        assert result.output is not None
        assert result.output["action"]["kind"] == "propose_strategy"
        updates = result.output["brief_updates"]
        assert [[u["kind"], u["scope"]] for u in updates] == brief_meta["kinds"]
        if ["text", "title"] in brief_meta["kinds"]:
            title = next(u for u in updates if u["scope"] == "title")
            assert title["literal"] == "20K Koşu · Arnavutköy → Eminönü"
        per_clip = next(u for u in updates if u["scope"] == "per_clip")
        assert per_clip["literal"] is None and per_clip["description"]
        # KRI-190 (prompt v39): a stated route/distance/sequence is never dropped
        # into prose; it lands as structured facts and an order requirement.
        expected_facts = brief_meta.get("facts")
        if expected_facts:
            for key, value in expected_facts["per_clip"].items():
                assert per_clip["facts"].get(key) == value, key
            order = next(u for u in updates if u["kind"] == "order")
            for key, value in expected_facts["order"].items():
                assert order["facts"].get(key) == value, key

    shot_meta = fixture.meta.get("per_shot_texts")
    if shot_meta:
        # KRI-422 (prompt v42): text dictated for several described shots is one
        # per_clip entry per shot (literal = the words, description = the shot),
        # and the ledger keeps every one of them live instead of only the last.
        from app.kria.brief import apply_updates, parse_brief_updates

        assert result.output is not None
        assert result.output["action"]["kind"] == "propose_strategy"
        updates = parse_brief_updates(result.output.get("brief_updates"))
        shots = [u for u in updates if u.kind == "text" and u.scope == "per_clip"]
        assert [u.literal for u in shots] == shot_meta["literals"]
        assert all(u.description for u in shots)
        live = apply_updates(None, updates, source_turn_id="eval").live()
        assert [r.literal for r in live if r.is_shot_text] == shot_meta["literals"]
        # Title, closing text, duration and six shots: one more than the old cap
        # of 8, so whichever came last was cut.
        assert {(u.kind, u.scope) for u in updates} >= {("timing", "global"), ("text", "global")}
        assert len(updates) == len(shots) + 3

    if fixture.meta.get("general_context_only"):
        # KRI-244: describing when/how two sets of footage were captured is
        # useful creative context, but it does not authorize a durable brief
        # requirement or a server-resolved clip operation.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        updates = result.output.get("brief_updates") or []
        assert all(update["kind"] == "style" and update["scope"] == "global" for update in updates)
        assert not (action["strategy"].get("clip_intents") or [])

    if fixture.meta.get("reaction_beats"):
        # KRI-178: a phone-Talking request naming specific photo/sticker and
        # sound moments must come back with a non-empty `reaction_beats` list
        # and a `closing_media` pin, never a `licensed_sfx` request (beats own
        # sound placement on this manifest), and the edit format must stay
        # the phone-Talking `subtitled` archetype.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["edit_format"] == "subtitled"
        beats = strategy.get("reaction_beats") or []
        assert beats, "expected at least one reaction beat"
        for beat in beats:
            assert beat.get("visual_id") or beat.get("sound"), beat
        assert strategy.get("closing_media") is not None
        assert strategy.get("licensed_sfx") is None

    if fixture.meta.get("single_clip_talking_scope"):
        # KRI-238: "all" media on a one-clip phone Talking edit is that clip.
        # Guided proposals never exist there, so an unrepaired "all" was
        # refused on every attempt and the turn failed.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["edit_format"] == "subtitled"
        assert strategy["render_program"] == "native"
        # Only "all" is refused. With CLIP_INTENTS_ENABLED on, the live model
        # omits the scope instead, and the planner still captions the one clip
        # (test_flag_on_answer_without_media_scope_still_captions_the_clip).
        assert strategy.get("media_scope") != "all"
        if strategy.get("media_scope") == "selected":
            manifest_media = fixture.input["capability_manifest"]["media"]
            assert strategy["selected_media_ids"] == [manifest_media[0]["media_id"]]

    fullscreen_meta = fixture.meta.get("fullscreen_overlays")
    if fullscreen_meta:
        # KRI-297: the planner emits `overlay_display="fullscreen"` (with the
        # overlays treatment, subtitled format kept) only when the manifest
        # advertises `media_overlays:fullscreen`; otherwise it stays null. It
        # never describes full-screen Visuals as "transitions".
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        advertised = (
            fixture.input["capability_manifest"]["capabilities"]
            .get("media_overlays:fullscreen", {})
            .get("available", False)
        )
        assert strategy["edit_format"] == "subtitled"
        assert "transitions" not in strategy.get("optional_treatments", [])
        assert strategy.get("overlay_display") == fullscreen_meta["expect_overlay_display"]
        assert advertised == (fullscreen_meta["expect_overlay_display"] == "fullscreen")
        if strategy.get("overlay_display") == "fullscreen":
            assert "overlays" in strategy["optional_treatments"]
        else:
            copy = f"{strategy.get('rationale', '')} {action.get('summary', '')}".casefold()
            assert "full-screen transition" not in copy

    user_song_meta = fixture.meta.get("user_song")
    if user_song_meta:
        # KRI-374: with an uploaded song on the manifest the song IS the music,
        # and the creator's words alone pick the mode. An explicit lip-sync /
        # sing-along request => "lipsync"; anything else, including merely
        # mentioning a concert or dancing => "background". The summary says
        # which mode was chosen in plain words.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["audio_strategy"] == "user_song"
        assert strategy.get("song_sync") == user_song_meta["expect_sync"]
        assert strategy.get("resolved_song_takes") is None
        summary = action["summary"].casefold()
        if user_song_meta["expect_sync"] == "lipsync":
            assert re.search(r"\blip|\bsing", summary)
            assert not strategy.get("archetype")
            assert strategy.get("execution_contract") is None
        else:
            assert not re.search(r"\blip", summary)
            assert any(word in summary for word in ("background", "music", "beat"))
