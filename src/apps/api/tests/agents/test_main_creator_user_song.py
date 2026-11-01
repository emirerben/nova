"""KRI-374 lane C: the Main Creator learns about an uploaded song.

The song section of the prompt is rendered ONLY when the manifest carries a
usable song. The no-song hashes below are the KRI-506 v46 rebaseline for the
same `_input()` fixture with `creator_montage_shapes_enabled` off.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from app.agents import main_creator as main_creator_module
from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    ProposeStrategy,
    UserSongFacts,
)
from app.agents._skills import load_skill
from app.agents.main_creator import (
    MAIN_CREATOR_PROMPT_VERSION,
    MainCreatorAgent,
    MainCreatorInput,
)
from app.config import settings
from tests.agents.test_main_creator_agent import _input, _manifest

# (clip_intents_enabled, brief_enabled) -> sha256 of the deliberate v56 no-song baseline.
# v55 (KRI-598) adds the unavailable-`guided_voiceover` rule, so all four renders changed.
# v56 (KRI-614) adds the clip `filename` sentence to the base prompt: all four changed again.
# v57 (KRI-661) adds the mixed-Visuals `overlay_display` rule: all four changed again.
# v59 (KRI-669/KRI-667) combines heading relationships and language-neutral brief facts.
# v60 (KRI-700) adds the `footage_light` brief fact: the two brief-on renders changed.
# v63 adds source-backed explanations; no-song fixtures track the deliberate new baseline.
# v62 grounds a phone montage story in owned clip IDs and explanatory text goals.
# v64 adds typed unchanged-source audio intent; only the two brief-on renders change.
# v65 (KRI-793) adds the `spellings` caption fact: only the two brief-on renders change.
# v66 combines it with KRI-791 spoken-text `triggers` + `speaker`; legacy brief-off
# renders remain unchanged. Re-pin brief-on hashes only after reviewing both additions.
# v67 preserves those instructions and adds quoted start_line/end_line trim facts.
# v68 combines caption spellings, spoken triggers/speaker, quoted trims and named cuts.
# v69 retains editorial pairing with caption/spoken-text/quoted-trim/named-cut facts.
# v70 (KRI-851) edits only the Talking reaction-beats section, which these no-beats
# montage renders never include: all four hashes are unchanged.
# v71 (KRI-814) adds the typed `delegates_all_choices` declaration: brief-on hashes only.
# v72 (KRI-814) re-reads unrecorded earlier creator lines: brief-on hashes only.
# v74 combines subtitle semantics and delegation; brief-off matches subtitle v73 exactly.
# v76 (KRI-816) teaches the typed `starts_fresh` declaration: brief-on hashes only.
# v77 (KRI-827) adds `keep_text_off` facts to the brief section: brief-on hashes only.
# v77 (KRI-821) teaches the typed `source_muted` audio intent: brief-on hashes only.
# v78 (KRI-821) teaches `cut_line_occurrences` for a repeated named cut: brief-on hashes only.
# v80 combines KRI-827 keep_text_off with KRI-821: brief-on hashes re-pinned.
# v81 (KRI-881) types the research ask in the brief section (brief-on) and rewords the
# shared "Researched explanations" section (research happens before approval): all four
# hashes re-pinned after reviewing both edits.
# v92 merges main's v91 (KRI-724/840/841/842/847/849 brief facts) with KRI-881: brief-on
# hashes re-pinned (brief-off equal KRI-881's v81, main left those prompts unchanged).
# v95 (KRI-689): brief-on hashes re-pinned for the `playback_rate` fact; brief-off unchanged.
# v96 (exact-timed small cards) adds the `timed_card` fact to the brief section: brief-on
# hashes re-pinned; brief-off unchanged (the SMALL CARDS section renders only on a manifest
# that advertises `timed_cards`, which these renders never do).
# v97 (KRI-945) narrows the described-text exception to text on clips: clip-intents-on hashes
# re-pinned; clip-intents-off unchanged (the exception renders only with clip intents).
# v98 (KRI-942) adds clip sound on / off (`clip_sound` "on"/"off") to the brief section: brief-on
# hashes re-pinned; brief-off unchanged.
# v99 (KRI-957) teaches that an after-voice `triggers` line is said in the clip, never the
# creator's own last voiceover words: brief-on hashes re-pinned; brief-off unchanged.
# v100 (KRI-994) teaches the select media-kind facts (`only_media_kind`, `include_media_kind`):
# brief-on hashes re-pinned; brief-off unchanged.
# v102 (29 Ekim lane D, KRI-1038): text_sequence in the brief section (brief-on) and the
# "words to SHOW are on-screen text" rule in main_creator.txt (all four hashes re-pinned).
# v103 (KRI-1027) teaches the `landscape_fit` fact ("fill" / "fit") for how sideways clips sit in
# a vertical video: brief-on hashes re-pinned; brief-off unchanged.
# v105 (KRI-1077): brief-on re-pinned for registry-rendered animation names.
# v106 (prompt fidelity, animation): the two brief-enabled hashes re-pinned (one added paragraph).
# v104 (cache layout): all four re-pinned; content is the same lines in a new order
# (per-turn INPUT block moved to the end), plus two orientation lines and one closing line.
# v109 (KRI-1097 wave 2, planner boundary): all four re-pinned for the three evidence keys;
# with PLANNER_BOUNDARY_HONEST_ENABLED off the render is v108's, byte for byte (V108_PROMPT_SHA).
PRE_CHANGE_PROMPT_SHA = {
    (True, True): "9d8410bf788fa41a3bb23c1a3392e7195b3ff1ad2e43b8fdcecc98fed6d3cd7a",
    (True, False): "8d68bb724b5d10aeb66701f232b845d1a251d90b66beb20fbfb90d7acd84d986",
    (False, True): "e53dbcf60c5e61db394393305060b08bbd06c4595a939d77ccf01541265b9c46",
    (False, False): "6c349d5e284e93ffb249ed319d81f685fad03e243127f84db1c1ef27308332e1",
}
V108_PROMPT_SHA = {
    (True, True): "d457efa4baba63866bd42139f883bb07d8b691995109ecdb8168b601c4b6d6b9",
    (True, False): "e3de37c194b88bd906568375111a33a5d8ec8bdab981a29ff6f377162710e87e",
    (False, True): "1b9ba5be3204a86bce34fb042e62eb839bd628641481540f6d22cfaf30b243a6",
    (False, False): "e804c3135cb2e6eae9093216bee3ee0d97159aa120c2b1eef8e34e61a3d37240",
}


def _render(agent_input: MainCreatorInput) -> str:
    return MainCreatorAgent(None).render_prompt(agent_input)  # type: ignore[arg-type]


def _song_input() -> MainCreatorInput:
    manifest = _manifest().model_copy(
        update={
            "has_user_song": True,
            "user_song": UserSongFacts(duration_s=187.4, has_lyrics=True),
            "capabilities": {
                **_manifest().capabilities,
                "phone_source_audio": CapabilityAvailability(available=True),
                "user_song": CapabilityAvailability(available=True),
            },
        }
    )
    return _input().model_copy(update={"capability_manifest": manifest})


@pytest.mark.parametrize("honest", [True, False])
@pytest.mark.parametrize(("clip_intents", "brief"), sorted(PRE_CHANGE_PROMPT_SHA))
def test_prompt_without_a_song_preserves_base_under_editorial_skills(
    monkeypatch, clip_intents, brief, honest
) -> None:
    monkeypatch.setattr(settings, "clip_intents_enabled", clip_intents)
    monkeypatch.setattr(settings, "creator_montage_shapes_enabled", False)
    monkeypatch.setattr(settings, "planner_boundary_honest_enabled", honest)

    prompt = _render(_input().model_copy(update={"brief_enabled": brief}))

    skills = (
        load_skill("edit-story")
        + "\n\n"
        + load_skill("grounded-hook")
        + "\n\n"
        + load_skill("meaningful-moment")
        + "\n\n"
    )
    assert prompt.startswith(skills)
    base_prompt = prompt.removeprefix(skills)
    assert (
        hashlib.sha256(base_prompt.encode()).hexdigest()
        == (PRE_CHANGE_PROMPT_SHA if honest else V108_PROMPT_SHA)[(clip_intents, brief)]
    )
    assert "UPLOADED SONG" not in prompt
    assert "$user_song_section" not in prompt
    assert "user_song" not in prompt


def test_prompt_with_a_song_teaches_the_song_rules() -> None:
    prompt = _render(_song_input())

    assert main_creator_module._USER_SONG_PROMPT_SECTION in prompt
    assert "$user_song_section" not in prompt
    for cue in ("lip sync", "lip-sync", "lipsync", "singing along", "mouthing the words"):
        assert cue in prompt
    assert 'set\n`audio_strategy` to "user_song"' in prompt
    assert '"lipsync"' in prompt and '"background"' in prompt
    assert "EVERYTHING ELSE" in prompt
    assert "concert" in prompt and "dancing" in prompt
    assert "`summary` MUST say" in prompt
    # The section lives inside the strategy rules, before the JSON envelope.
    assert prompt.index("UPLOADED SONG") < prompt.index("Return ONLY JSON in this envelope")


def test_song_section_is_the_only_difference_when_a_song_is_attached() -> None:
    plain = _input()
    with_song = _song_input()
    song_prompt = _render(with_song)
    # Same everything-else: removing the section restores the plain render except
    # for the manifest JSON, which now (legitimately) carries the song facts.
    assert song_prompt.replace("\n" + main_creator_module._USER_SONG_PROMPT_SECTION, "") != (
        _render(plain)
    )
    assert '"has_user_song":true' in song_prompt
    assert '"user_song":{"duration_s":187.4,"has_lyrics":true}' in song_prompt


def test_prompt_version_is_bumped_and_wired_into_the_spec() -> None:
    assert MAIN_CREATOR_PROMPT_VERSION == "2026-11-01-v109"
    assert MainCreatorAgent.spec.prompt_version == MAIN_CREATOR_PROMPT_VERSION


def _raw(**strategy_update) -> str:
    strategy = {
        "direction": "fast_montage",
        "edit_format": "montage",
        "audio_strategy": "user_song",
        "render_program": "guided",
        "selected_media_ids": [],
        "target_duration_s": 24,
        "rationale": "Cut the clips to the creator's own song.",
        **strategy_update,
    }
    return json.dumps(
        {
            "action": {
                "kind": "propose_strategy",
                "strategy": strategy,
                "summary": "I'll use your song as the background music.",
            }
        }
    )


def test_parse_reads_the_model_authored_song_sync() -> None:
    agent_input = _song_input()
    for sync in ("lipsync", "background"):
        output = MainCreatorAgent(None).parse(_raw(song_sync=sync), agent_input)  # type: ignore[arg-type]
        assert isinstance(output.action, ProposeStrategy)
        assert output.action.strategy.audio_strategy == "user_song"
        assert output.action.strategy.song_sync == sync


def test_parse_reads_lip_sync_spellings_and_tolerates_junk() -> None:
    agent_input = _song_input()
    spelled = MainCreatorAgent(None).parse(_raw(song_sync="lip-sync"), agent_input)  # type: ignore[arg-type]
    assert isinstance(spelled.action, ProposeStrategy)
    assert spelled.action.strategy.song_sync == "lipsync"
    junk = MainCreatorAgent(None).parse(_raw(song_sync="karaoke"), agent_input)  # type: ignore[arg-type]
    assert isinstance(junk.action, ProposeStrategy)
    assert junk.action.strategy.song_sync is None  # repaired later, with a notice


def test_parse_never_trusts_model_authored_resolved_song_takes() -> None:
    output = MainCreatorAgent(None).parse(  # type: ignore[arg-type]
        _raw(song_sync="lipsync", resolved_song_takes=[{"media_id": "x", "delta_s": 4.2}]),
        _song_input(),
    )
    assert isinstance(output.action, ProposeStrategy)
    assert output.action.strategy.resolved_song_takes is None
    assert "resolved_song_takes" not in output.model_dump_json()


def test_parse_of_a_strategy_without_song_fields_stays_byte_identical() -> None:
    agent_input = _input()
    raw = _raw(audio_strategy="licensed_music")
    output = MainCreatorAgent(None).parse(raw, agent_input)  # type: ignore[arg-type]
    dumped = output.model_dump_json()
    assert "song_sync" not in dumped and "resolved_song_takes" not in dumped


def test_parse_of_user_song_without_a_usable_song_retries_with_a_rule() -> None:
    agent = MainCreatorAgent(None)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="invalid output"):
        agent.parse(_raw(song_sync="background"), _input())
    assert "manifest.user_song" in agent.schema_clarification()
