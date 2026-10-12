"""KRI-594: the guided-voiceover downgrade asks only when it loses something asked for.

Prod thread f77a5c16 (turn 031026ee, 2026-10-11), an iPhone Voiceover edit with 7 clips, 1
Visuals photo and a 22 s voiceover: "Sira seslendirmeyi takip etsin: nehir, mutfak, cati
kati, Deniz, banyo. Iskele. 7 klibin hepsini kullan". Main Creator v53 answered with the
`guided_voiceover_v1` contract over `media_scope: "all"` (two local replays of the logged
input, 2026-10-11: no selection, no reaction beats, no clip intents -- nothing in the
strategy ties the bathtub photo to a word). Runtime v2 always downgrades that contract to
the native voiceover edit over the project's own clips. The policy counted the dropped
contract itself as a lost request and asked: "Visuals'taki fotoğraf ve videolar henüz
seslendirmeye göre zamanlanamıyor ... Daha sade bir sürüm yapayım mı?" -- a claim that is
false on this edit, where a Visuals photo pops in on a spoken word (KRI-519 reaction beats).

The creator HAD asked for that photo, in their first message: the live brief requirement r5
("Seslendirmede 'kırkayak' kelimesi duyulduğunda bu küvet fotoğrafı belirecek", scope
`clip:asset-fe1c…`). So the downgrade really does lose it, and the question must say so by
name instead of dropping it silently or blaming the voiceover.

Failure modes (written before the code):

* the contract swap alone (every clip kept, no Visual asked for) still asks "simpler version?";
* a Visual the creator asked for in an EARLIER turn (a live brief requirement targets it) is
  dropped without asking because this turn's strategy does not name it;
* the "all 7 clips" count reads the Visuals as not asked for even though a live brief
  requirement targets the photo;
* a requirement that asks to LEAVE a Visual out ("remove the bathtub photo") makes it count
  as asked for;
* a brief clip scope written without the "analysis-proxy-ios-" prefix names no clip;
* a Visuals photo the guided plan named, and that no reaction beat places, is dropped
  without asking;
* a photo that a reaction beat places is counted as lost, so the downgrade still asks;
* "use all my photos and videos" loses every Visuals photo without asking, because the
  "all" scope is read as clips only;
* narration labels or exact photo/video timing (which only the guided contract carries)
  are dropped without asking;
* the notice still claims Visuals can never be timed to a voiceover where photo beats work,
  or claims photo beats where the manifest has none (cloud voiceover);
* a caption-free ask or a stated title hold stops being asked about once a downgrade ran.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.agents._schemas.creator_agent import (
    CreativeStrategy,
    CreatorEditSnapshot,
    CreatorRenderIntentEvidence,
    ProposeStrategy,
)
from app.agents._schemas.creator_policy import GUIDED_VOICEOVER_EXECUTION_CONTRACT
from app.agents.main_creator import MainCreatorOutput
from app.config import settings
from app.kria import planner
from app.kria.brief import BriefRequirement, CreativeBrief, requested_media
from app.kria.reply_language import reply_language_for
from app.kria.strategy_policy import (
    GUIDED_VOICEOVER_DOWNGRADE_NOTICE,
    VISUALS_NEED_A_WORD_NOTICE,
    CheckedStrategy,
    RefusedStrategy,
    check_strategy_for_runtime_v2,
)
from app.schemas.clip_intents import ClipIntent
from app.services import creator_capabilities as capabilities
from tests._prod_profile import PROD_NARRATION_IDENTITY

# The project's own clips and its one Visuals photo, as in the logged manifest.
TIGRE_CLIPS = [
    "analysis-proxy-ios-8F1D55F0-5B98-49E7-962A-4D87727D0686.mp4",
    "analysis-proxy-ios-F4DBD4B3-0C0E-42DF-A249-70F332C3F22A.mp4",
    "analysis-proxy-ios-80DCD337-53E9-426D-8DC8-31AEC9359F76.mp4",
    "analysis-proxy-ios-CFBEC1AE-2523-49F2-B675-04576EAED824.mp4",
    "analysis-proxy-ios-A9859821-B374-42D6-9F07-F9C4734506C7.mp4",
    "analysis-proxy-ios-3F6FEFE1-E931-4D3F-8DEC-1EC7300D9E0F.mp4",
    "analysis-proxy-ios-ECCB47EC-0C2C-4F64-917F-85FB8039C9D7.mp4",
]
BATHTUB_PHOTO = "asset-fe1c1382-89e1-440e-b54d-f3a4d945f4ff"
# The creator's first message in thread f77a5c16, verbatim (smart quotes as typed).
TIGRE_REQUEST = (
    "Seslendirmem Türkçe ama altyazılar İngilizce olsun. Başlık: “Tigre’de bir gün”. "
    "Nehir klibiyle başla. “Deniz” dediğimde Deniz’i laptop başında göster. “kırkayak” "
    "dediğimde küvet fotoğrafını göster. “mutfak” dediğimde mutfağı göster. Başlık dışında "
    "başka yazı olmasın."
)
ORDER_REQUEST = (
    "Sira seslendirmeyi takip etsin: nehir, mutfak, cati kati, Deniz, banyo. Iskele. "
    "7 klibin hepsini kullan"
)
PHOTO_ASK = "Seslendirmede 'kırkayak' kelimesi duyulduğunda bu küvet fotoğrafı belirecek"
# The brief the turn planned against (Main Creator input `creator_request`, v1 + this turn).
TIGRE_BRIEF = CreativeBrief(
    version=1,
    requirements=[
        BriefRequirement(
            id="r1",
            kind="style",
            scope="global",
            description="İngilizce altyazı eklenecek (seslendirme Türkçe kalacak)",
        ),
        BriefRequirement(id="r2", kind="text", scope="title", literal="Tigre’de bir gün"),
        BriefRequirement(
            id="r3",
            kind="order",
            scope="global",
            description="Kurgu nehir klibiyle başlayacak",
            facts={"first_clip": "Nehir klibi"},
        ),
        BriefRequirement(
            id="r4",
            kind="timing",
            scope="clip:CFBEC1AE-2523-49F2-B675-04576EAED824.mp4",
            description="Seslendirmede 'Deniz' kelimesi duyulduğunda bu klip gösterilecek",
        ),
        BriefRequirement(
            id="r5", kind="timing", scope=f"clip:{BATHTUB_PHOTO}", description=PHOTO_ASK
        ),
        BriefRequirement(
            id="r6",
            kind="timing",
            scope="clip:80DCD337-53E9-426D-8DC8-31AEC9359F76.mp4",
            description="Seslendirmede 'mutfak' kelimesi duyulduğunda mutfak klibi gösterilecek",
        ),
        BriefRequirement(
            id="r7",
            kind="style",
            scope="global",
            description="Başlık dışında ekranda başka yazı kullanılmayacak",
        ),
        BriefRequirement(
            id="r8",
            kind="order",
            scope="global",
            description=(
                "Sıra seslendirmeyi takip etsin: nehir, mutfak, çatı katı, Deniz, banyo, iskele."
            ),
            facts={"first_clip": "nehir", "last_clip": "iskele"},
        ),
        BriefRequirement(
            id="r9", kind="select", scope="global", description="7 klibin hepsini kullan"
        ),
    ],
)
NOT_TIMED_TR = "henüz seslendirmeye göre zamanlanamıyor"
NEED_A_WORD_TR = "söylediğin bir kelimede beliriyor"
HALT = "(test: the strategy check passed)"


def _tigre_manifest(*, phone: bool = True):
    manifest = capabilities.resolve_creator_manifest(
        item_id="item-tigre",
        edit_format="narrated_planned",
        media=[
            {"media_id": media_id, "kind": "video", "duration_s": 6.0} for media_id in TIGRE_CLIPS
        ]
        + [{"media_id": BATHTUB_PHOTO, "kind": "image"}],
        **(
            {"phone_source_media_ids": TIGRE_CLIPS, "phone_rendering_allowed": True}
            if phone
            else {}
        ),
        has_voiceover=True,
        narration={**PROD_NARRATION_IDENTITY, "duration_s": 22.151837},
    )
    # The thread's first render had failed: a draft exists.
    return manifest.model_copy(
        update={"current_edit": CreatorEditSnapshot(revision=0, status="failed")}
    )


def _guided(**update) -> CreativeStrategy:
    """Main Creator v53's strategy for the order turn (local replay of the prod input)."""
    fields = {
        "direction": "native",
        "edit_format": "narrated_planned",
        "audio_strategy": "voiceover",
        "execution_contract": GUIDED_VOICEOVER_EXECUTION_CONTRACT,
        "media_scope": "all",
        "render_program": "guided",
        "selected_media_ids": [],
        "story_structure": [
            "Nehir manzarasıyla açılış",
            "Sırasıyla mutfak ve çatı katı görüntüleri",
            "Deniz'in tanıtılması ve ardından banyo ile küvet detayları",
            "İskele görüntüsüyle kapanış",
        ],
        "caption_style": "auto",
        "opening_title": "Tigre’de bir gün",
        "pacing": "balanced",
        "target_duration_s": 50,
        "video_reuse_policy": "once",
        "rationale": "7 klibin tamamı, seslendirmedeki sırayla.",
    }
    return CreativeStrategy(**{**fields, **update})


def _tigre_asks() -> dict[str, str]:
    return requested_media(TIGRE_BRIEF, [*TIGRE_CLIPS, BATHTUB_PHOTO])


def _ask(
    strategy: CreativeStrategy,
    *,
    creator_text: str = "",
    requested: dict[str, str] | None = None,
    phone: bool = True,
):
    return check_strategy_for_runtime_v2(
        _tigre_manifest(phone=phone),
        strategy,
        ask_before_simplifying=True,
        ask_about_stated_settings=True,
        creator_text=creator_text,
        **({"requested_media": requested} if requested is not None else {}),
    )


async def _plan_until_strategy_check(
    monkeypatch: pytest.MonkeyPatch,
    *,
    strategy: CreativeStrategy,
    user_message: str,
    history: tuple[str, ...],
    requested: dict[str, str] | None = None,
):
    """Run `_plan_creator_action` (brief binding + choice questions on, a Turkish chat) up to
    the REAL `check_strategy_for_runtime_v2`, then stop: a passing check becomes a sentinel
    question so nothing after it (clip intents, the song gate) runs."""

    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    seen: list = []
    real_check = planner.check_strategy_for_runtime_v2

    def _check(manifest, strategy, **kwargs):  # noqa: ANN001, ANN003, ANN202
        checked = real_check(manifest, strategy, **kwargs)
        seen.append(checked)
        if isinstance(checked, CheckedStrategy):
            return RefusedStrategy(question=HALT, code="test_halt")
        return checked

    monkeypatch.setattr(planner, "check_strategy_for_runtime_v2", _check)
    output = MainCreatorOutput(
        action=ProposeStrategy(
            kind="propose_strategy",
            strategy=strategy,
            summary="Seslendirmeli bir düzenleme.",
            render_intent_evidence=CreatorRenderIntentEvidence(
                opening_title="Başlık: “Tigre’de bir gün”"
            ),
        )
    )
    conversation = [{"role": "user", "content": text} for text in (*history, user_message)]
    with reply_language_for("tr"):
        result = await planner._plan_creator_action(
            None,
            thread_id=uuid.uuid4(),
            item_id=uuid.uuid4(),
            creator_id=uuid.uuid4(),
            user_message=user_message,
            manifest=_tigre_manifest(),
            inputs=SimpleNamespace(
                agent_input=SimpleNamespace(conversation=conversation),
                creative_copy_state={},
                creative_copy_digest="d" * 24,
            ),
            output=output,
            **({"requested_media": requested} if requested is not None else {}),
        )
    [check_result] = seen
    return result, check_result


@pytest.mark.asyncio
async def test_tigre_order_turn_names_the_photo_the_creator_tied_to_a_word(
    prod_profile, monkeypatch
) -> None:
    """The prod turn, through the planner, with the brief it planned against: r5 (turn 1)
    asks for the bathtub photo on "kırkayak", the guided plan only covered it through "all",
    and the native edit cannot show it without a beat. The creator's own count ("7 klibin")
    must not hide that: the question names the photo instead of a silent draft."""

    result, checked = await _plan_until_strategy_check(
        monkeypatch,
        strategy=_guided(),
        user_message=ORDER_REQUEST,
        history=(TIGRE_REQUEST, "Evet baslik suresi onemli degil"),
        requested=_tigre_asks(),
    )

    assert isinstance(checked, RefusedStrategy)
    assert checked.code == "simplification_requires_choice"
    question = result.plan.response
    assert question == checked.question
    assert "kırkayak" in question and "küvet fotoğrafı" in question
    assert NOT_TIMED_TR not in question
    assert NEED_A_WORD_TR not in question
    assert question.endswith("Mevcut taslağın değişmedi. Daha sade bir sürüm yapayım mı?")


@pytest.mark.asyncio
async def test_all_seven_clips_with_no_photo_ask_is_made_not_asked_about(
    prod_profile, monkeypatch
) -> None:
    """Without any ask for the Visuals photo, "use all 7 clips" keeps every clip, so the
    contract swap loses nothing and the draft is made (the photo is left out with a note)."""

    result, checked = await _plan_until_strategy_check(
        monkeypatch,
        strategy=_guided(),
        user_message=ORDER_REQUEST,
        history=("Başlık: “Tigre’de bir gün”. Nehir klibiyle başla.",),
    )

    assert result.plan.response == HALT, result.plan.response
    assert isinstance(checked, CheckedStrategy)
    assert checked.strategy.execution_contract is None
    assert checked.strategy.render_program == "native"
    assert checked.strategy.selected_media_ids == TIGRE_CLIPS
    assert checked.strategy.opening_title == "Tigre’de bir gün"
    assert not any(NOT_TIMED_TR in notice for notice in checked.notices)


def test_the_brief_names_the_media_the_creator_asked_for() -> None:
    asks = _tigre_asks()

    assert asks[BATHTUB_PHOTO] == PHOTO_ASK
    # The brief drops the "analysis-proxy-ios-" prefix of the creator's own clips.
    assert set(asks) == {BATHTUB_PHOTO, TIGRE_CLIPS[2], TIGRE_CLIPS[3]}
    removal = CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="select",
                scope=f"clip:{BATHTUB_PHOTO}",
                description="Küvet fotoğrafını çıkar",
            ),
            BriefRequirement(
                id="r2",
                kind="select",
                scope=f"clip:{BATHTUB_PHOTO}",
                description="Don't use the bathtub photo",
            ),
            BriefRequirement(
                id="r3",
                kind="timing",
                scope=f"clip:{BATHTUB_PHOTO}",
                description=PHOTO_ASK,
                status="superseded",
            ),
        ],
    )
    assert requested_media(removal, [BATHTUB_PHOTO]) == {}
    assert requested_media(None, [BATHTUB_PHOTO]) == {}


def test_a_photo_placed_by_a_word_survives_the_downgrade_without_a_question(
    prod_profile,
) -> None:
    """The turn before (b9a5d1e9) put the bathtub photo on "kırkayak": a beat this edit
    draws, so the photo the creator asked for (r5) is still in the edit and nothing is lost."""

    checked = _ask(
        _guided(
            media_scope="selected",
            selected_media_ids=[*TIGRE_CLIPS, BATHTUB_PHOTO],
            reaction_beats=[
                {
                    "beat_id": "centipede-photo",
                    "trigger": "kırkayak",
                    "visual_id": BATHTUB_PHOTO,
                    "occurrence": "first",
                    "visual_role": "photo",
                }
            ],
        ),
        requested=_tigre_asks(),
    )

    assert isinstance(checked, CheckedStrategy), getattr(checked, "question", None)
    assert checked.strategy.execution_contract is None
    assert [beat.visual_id for beat in checked.strategy.reaction_beats] == [BATHTUB_PHOTO]
    assert checked.notices == ()


@pytest.mark.parametrize(
    ("strategy_update", "creator_text"),
    [
        # The guided plan named the photo, and no spoken word places it.
        ({"media_scope": "selected", "selected_media_ids": [*TIGRE_CLIPS, BATHTUB_PHOTO]}, ""),
        # "All my photos and videos" covers the Visuals photo too.
        ({}, "Use all my photos and videos with my voiceover"),
        ({}, "Fotoğrafların ve videoların hepsini kullan"),
        # A count naming every item (7 clips + 1 photo) covers it as well.
        ({}, "use all 8"),
    ],
)
def test_a_visual_the_creator_asked_for_is_still_asked_about_honestly(
    prod_profile, strategy_update, creator_text
) -> None:
    english = _ask(_guided(**strategy_update), creator_text=creator_text)
    with reply_language_for("tr"):
        turkish = _ask(_guided(**strategy_update), creator_text=creator_text)

    assert isinstance(english, RefusedStrategy) and isinstance(turkish, RefusedStrategy)
    assert english.code == turkish.code == "simplification_requires_choice"
    assert english.question.startswith(VISUALS_NEED_A_WORD_NOTICE)
    assert "can't be timed" not in english.question
    assert NOT_TIMED_TR not in turkish.question
    assert "kelime" in turkish.question and turkish.question.endswith("?")


def test_cloud_voiceover_without_photo_beats_keeps_the_old_true_notice(prod_profile) -> None:
    refused = _ask(
        _guided(media_scope="selected", selected_media_ids=[*TIGRE_CLIPS, BATHTUB_PHOTO]),
        phone=False,
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.question.startswith(GUIDED_VOICEOVER_DOWNGRADE_NOTICE)


@pytest.mark.parametrize(
    "strategy_update",
    [
        {
            "clip_intents": [
                ClipIntent(
                    intent_id="names",
                    op="label",
                    attribute="who I talk about",
                    label_source="transcript",
                    transcript_kind="participant",
                )
            ]
        },
        {
            "mixed_media_timing": {
                "image_hold": "very_fast",
                "video_hold": "longer",
                "boundary_style": "cut",
            }
        },
    ],
    ids=["narration-labels", "exact-photo-video-timing"],
)
def test_asks_that_only_the_guided_contract_carries_are_still_asked_about(
    prod_profile, strategy_update
) -> None:
    refused = _ask(_guided(**strategy_update), creator_text=ORDER_REQUEST)

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "simplification_requires_choice"


def test_other_asks_are_still_compared_after_a_lossless_downgrade(prod_profile) -> None:
    caption_free = _ask(_guided(caption_style="none"), creator_text=ORDER_REQUEST)
    assert isinstance(caption_free, RefusedStrategy)
    assert caption_free.code == "simplification_requires_choice"

    held = _ask(_guided(opening_title_duration_s=2.0), creator_text=ORDER_REQUEST)
    assert isinstance(held, RefusedStrategy)
    assert "exactly 2 seconds" in held.question


# --- PR 1550 review: leaving a medium out vs restricting how it looks ---------------------


def _photo_select(words: str) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1", kind="select", scope=f"clip:{BATHTUB_PHOTO}", description=words
            )
        ],
    )


@pytest.mark.parametrize(
    "words",
    [
        "Show the bathtub photo, no zoom",
        "Show the bathtub photo without zoom",
        "Remove the zoom on the bathtub photo",
        "Skip the music on the bathtub photo",
        "küvet fotoğrafını göster, zoom olmasın",
        "fotoğrafı göster ama müzik olmasın",
        "Küvet fotoğrafındaki efekti çıkar",
        "zoom'u kaldır, küvet fotoğrafı kalsın",
        "Leave the bathtub photo in and take the music and the zoom effect out",
        # Turkish "çıkar" is also "comes out": a photo coming onto the screen is an ask.
        "Kırkayak deyince küvet fotoğrafı ekrana çıkar",
        # PR 1550 re-review: the removal's own object is something else, and the photo is
        # asked for after it, or only its treatment is removed.
        "Remove the black bars and show the bathtub photo",
        "Skip the intro and show the bathtub photo",
        "Remove the bathtub photo's zoom",
        "Remove the black bars, then show the bathtub photo",
        "Remove the black bars to show the bathtub photo",
        "Drop the intro then show the bathtub photo",
        "Intro'yu atla ve küvet fotoğrafını göster",
        "Siyah bantları kaldır ve küvet fotoğrafını göster",
    ],
)
def test_a_select_that_only_restricts_how_the_photo_looks_still_asks_for_it(words) -> None:
    """The negation governs a treatment (zoom, music, an effect), not the photo."""

    assert requested_media(_photo_select(words), [BATHTUB_PHOTO]) == {BATHTUB_PHOTO: words}


@pytest.mark.parametrize(
    "words",
    [
        "Remove the bathtub photo",
        "Don't use the bathtub photo",
        "Show it without the bathtub photo",
        "leave the photo out",
        "Get rid of it",
        "küvet fotoğrafını çıkar",
        "küvet fotoğrafını kullanma",
        "fotoğraf olmasın",
        "Küvet fotoğrafını koyma",
        # The removal's bounded object is the photo itself.
        "Remove the photo of the bathtub",
        "Remove the bathtub photo and the river clip",
        "Skip it and show the river clip instead",
        "Skip",
        "No photo here",
        "Bunu çıkar",
    ],
)
def test_a_select_that_leaves_the_photo_out_is_not_an_ask(words) -> None:
    assert requested_media(_photo_select(words), [BATHTUB_PHOTO]) == {}


def test_a_no_zoom_photo_the_downgrade_would_drop_is_asked_about(prod_profile) -> None:
    """The reviewer's case end to end: "Show the bathtub photo, no zoom" is an ask, so a
    guided plan downgraded without a beat for that photo asks instead of dropping it."""

    words = "Show the bathtub photo, no zoom"
    refused = _ask(
        _guided(),
        creator_text=ORDER_REQUEST,
        requested=requested_media(_photo_select(words), [*TIGRE_CLIPS, BATHTUB_PHOTO]),
    )

    assert isinstance(refused, RefusedStrategy)
    assert refused.code == "simplification_requires_choice"
    assert refused.question.startswith(
        f'This version leaves out the photo you asked for: "{words}".'
    )
