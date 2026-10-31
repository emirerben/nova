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

    expected_text = fixture.meta.get("text_vs_speech")
    if expected_text:
        # 29 Ekim / KRI-1038: words the creator wants SHOWN a piece at a time are a
        # `text_sequence`; a captions / subtitles / voiceover ask stays the speech lane.
        assert result.output is not None
        updates = result.output["brief_updates"]
        captions = [u for u in updates if (u.get("facts") or {}).get("captions")]
        assert len(captions) == expected_text["captions_filings"]
        sequences = [u for u in updates if u["kind"] == "text_sequence"]
        want = expected_text["text_sequence"]
        if want is None:
            assert not sequences
        else:
            assert len(sequences) == 1
            assert sequences[0]["literal"] == want["literal"]
            assert sequences[0]["facts"] == {"unit": want["unit"], "mode": want["mode"]}

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

    fullscreen_beats = fixture.meta.get("fullscreen_reaction_beats")
    if fullscreen_beats:
        # KRI-840: "show the photo full screen when he says X" is a reaction beat with
        # display_mode "fullscreen" and a short hold (never overlay_display, which spreads the
        # photos evenly); a corner card omits the field; the brief records both asks' display.
        assert result.output is not None
        strategy = result.output["action"]["strategy"]
        assert not strategy.get("overlay_display")
        beats = {beat["beat_id"]: beat for beat in strategy["reaction_beats"]}
        for beat_id, max_hold_s in fullscreen_beats["fullscreen"].items():
            assert beats[beat_id]["display_mode"] == "fullscreen", beats[beat_id]
            assert 0.5 <= beats[beat_id]["hold_s"] <= max_hold_s, beats[beat_id]
        for beat_id in fullscreen_beats["corner"]:
            assert beats[beat_id].get("display_mode", "pip") == "pip", beats[beat_id]
        asked = {
            trigger: update["facts"].get("display_mode")
            for update in result.output["brief_updates"]
            for trigger in update["facts"].get("triggers", [])
        }
        assert asked == fullscreen_beats["brief_display"]

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

    n2_fullscreen = fixture.meta.get("n2_fullscreen")
    if n2_fullscreen:
        assert result.output is not None
        strategy = result.output["action"]["strategy"]
        beats = strategy.get("reaction_beats") or []
        bathtub = next(beat for beat in beats if beat["beat_id"] == "bathtub")
        river = next(beat for beat in beats if beat["beat_id"] == "river")
        assert bathtub["visual_id"] == n2_fullscreen["bathtub_photo"]
        assert bathtub["display_mode"] == "fullscreen"
        assert river["visual_id"] == n2_fullscreen["river_photo"]
        assert river["display_mode"] == "fullscreen"
        assert river["occurrence"] == "first"
        assert river["following_clip_id"] == n2_fullscreen["river_clip"]
        assert river["trigger"] == "the river"
        assert n2_fullscreen["loft_clip"] in strategy["selected_media_ids"]
        assert any(
            intent["op"] == "include" and "the loft" in intent["attribute"]
            for intent in strategy.get("clip_intents") or []
        )
        assert strategy["opening_title"] == n2_fullscreen["title"]
        assert strategy["caption_style"] == "clean"
        assert any(
            intent["op"] == "order"
            and intent.get("position") == "last"
            and "Deniz" in intent["attribute"]
            for intent in strategy.get("clip_intents") or []
        )

    cues = fixture.meta.get("voiceover_cues")
    if cues:
        # KRI-598: on a runtime-v2 Voiceover edit a cue for an own clip ("X dediğimde Y'yi
        # göster") is `include`, never a described sequence (`order` without position, which
        # the narration alignment enforced against the voiceover); every own clip stays in
        # the edit ("use all 7 clips" included) and there is no guided contract.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert not strategy.get("execution_contract")
        intents = strategy.get("clip_intents") or []
        assert not [i for i in intents if i["op"] == "order" and not i.get("position")], intents
        assert sum(i["op"] == "include" for i in intents) >= cues["min_includes"], intents
        own = [
            media["media_id"]
            for media in fixture.input["capability_manifest"]["media"]
            if media["kind"] == "video" and not media["media_id"].startswith("asset-")
        ]
        assert sorted(strategy["selected_media_ids"]) == sorted(own)

    dropped_plan = fixture.meta.get("editorial_plan_dropped")
    if dropped_plan:
        # KRI-806: an editorial_plan beside kept clip sound cannot run, so the parser drops
        # it instead of refusing the reply (each refusal spends one of three attempts); the
        # kept lines, the creator's selection, title and length are the rest of the answer.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy.get("editorial_plan") is None
        assert strategy["montage_audio"]["preserve_source_audio"] is True
        assert (
            strategy["montage_audio"]["source_media_ids"]
            == dropped_plan["montage_audio_source_media_ids"]
        )
        assert strategy["selected_media_ids"] == dropped_plan["selected_media_ids"]
        assert strategy["opening_title"] and strategy["target_duration_s"] == 15

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

    trim_meta = fixture.meta.get("take_trim")
    if trim_meta:
        # KRI-789 (prompt v65): quoted start / end LINES inside the one take are a trim, never
        # a clip order (v64 filed them as a key-less `order` requirement, which asked "You
        # gave me a specific order for the clips" on a one-video Talking edit). The stated
        # length stays its own timing requirement. Words compare loosely for live replies.
        from app.kria.brief import apply_updates, parse_brief_updates
        from app.kria.brief_route import loose_text

        def _said(text: object) -> list[str]:
            return re.findall(r"\w+", loose_text(str(text or "")))

        assert result.output is not None
        live = apply_updates(
            None, parse_brief_updates(result.output.get("brief_updates")), source_turn_id="eval"
        ).live()
        assert not [r for r in live if r.kind == "order"], [r.facts for r in live]
        trims = [r for r in live if r.kind == "timing" and "start_line" in r.facts]
        assert len(trims) == 1, [r.facts for r in live]
        assert _said(trims[0].facts["start_line"]) == _said(trim_meta["start_line"])
        assert _said(trims[0].facts.get("end_line")) == _said(trim_meta["end_line"])
        assert "duration_s" not in trims[0].facts and "triggers" not in trims[0].facts
        lengths = [r.facts.get("duration_s") for r in live if r.kind == "timing"]
        assert trim_meta["duration_s"] in lengths

    unclear_meta = fixture.meta.get("unclear_word_ask")
    if unclear_meta:
        # KRI-849 (prompt v76): "if you can't tell what he says, ask me instead of guessing" is
        # the recorded fact `ask_if_unclear` (the creator's words for the slot) on the captions
        # requirement, never prose only; the server (not the words) decides which word is
        # unclear. No `unclear_answer` until the creator answers.
        from app.kria.brief import apply_updates, parse_brief_updates
        from app.kria.unclear_word_ask import ask_requirements, model_answer

        assert result.output is not None
        assert result.output["action"]["kind"] == "propose_strategy"
        live = apply_updates(
            None, parse_brief_updates(result.output.get("brief_updates")), source_turn_id="eval"
        ).live()
        asks = ask_requirements(live)
        assert len(asks) == 1, [r.facts for r in live]
        assert asks[0].kind == "style" and asks[0].facts.get("captions") == "on"
        assert asks[0].facts["ask_if_unclear"].strip()
        assert asks[0].facts.get("spellings") == unclear_meta["spellings"]
        assert model_answer(live) is None

    answer_meta = fixture.meta.get("unclear_word_answer")
    if answer_meta:
        # KRI-849: the creator's typed answer to the unclear-word question is recorded on the
        # SAME requirement as `unclear_answer` ("omit", or the exact words typed); the ask and
        # the spellings stay (a partial change merges its facts, KRI-805).
        from app.kria.brief import CreativeBrief, apply_updates, bind_updates, parse_brief_updates
        from app.kria.unclear_word_ask import ask_requirements, model_answer

        assert result.output is not None
        prior = CreativeBrief.model_validate(answer_meta["prior_brief"])
        updates = bind_updates(prior, parse_brief_updates(result.output.get("brief_updates")))
        live = apply_updates(prior, updates, source_turn_id="eval").live()
        (ask,) = ask_requirements(live)
        for key in answer_meta["keeps"]:
            assert key in ask.facts, (key, ask.facts)
        assert ask.facts["unclear_answer"] == answer_meta["expect"]
        answer = model_answer(live)
        assert answer is not None and answer.action == answer_meta["expect"]

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

    ending_meta = fixture.meta.get("ending_clip")
    if ending_meta:
        # KRI-841: "add the X video at the very end with its own sound" on a phone Talking edit
        # is the strategy's `ending_clip` (never a pop-in card or a closing photo), and the
        # brief records it as a `style` requirement with the `ending_clip` fact: never as a
        # `last_clip` ordering ask, which the render contract cannot verify on a Talking edit.
        assert result.output is not None
        action = result.output["action"]
        assert action["kind"] == "propose_strategy"
        strategy = action["strategy"]
        assert strategy["edit_format"] == "subtitled"
        assert strategy.get("ending_clip") == ending_meta
        assert all(
            beat.get("visual_id") != ending_meta["media_id"]
            for beat in strategy.get("reaction_beats") or []
        )
        assert strategy.get("closing_media") is None
        facts = [update.get("facts") or {} for update in result.output.get("brief_updates") or []]
        assert any(f.get("ending_clip") and f.get("keep_audio") is True for f in facts)
        assert not any(f.get("last_clip") for f in facts)

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

    card_meta = fixture.meta.get("kri1093_card")
    if card_meta:
        # KRI-1093: the Lip-sync picker card (`manifest.chosen_format`) defaults an attached
        # song to lipsync; the creator's own words still win. Grade the strategy the creator
        # ends up with after the server repair, and the model's own choice where it is pinned.
        from app.agents._schemas.creator_agent import ResolvedCreatorManifest
        from app.agents._schemas.creator_policy import repair_creator_user_song
        from app.agents.main_creator import MainCreatorOutput

        assert result.output is not None
        action = MainCreatorOutput.model_validate(result.output).action
        assert action.kind == "propose_strategy"
        assert action.strategy.audio_strategy == "user_song"
        pinned = card_meta.get("expect_model_song_sync")
        if pinned is not None:
            assert action.strategy.song_sync == pinned
        manifest = ResolvedCreatorManifest.model_validate(fixture.input["capability_manifest"])
        assert manifest.chosen_format == card_meta["chosen_format"]
        repaired, _notices = repair_creator_user_song(manifest, action.strategy)
        assert repaired.song_sync == card_meta["expect_final_song_sync"]

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
