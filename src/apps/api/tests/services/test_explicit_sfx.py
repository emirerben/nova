from types import SimpleNamespace

from app.agents._schemas.sfx_intent import LicensedSfxIntent
from app.services.explicit_sfx import (
    materialize_explicit_sfx_placements,
    trusted_visual_moments,
)


def test_trusted_visual_moments_project_analysis_onto_output_timeline() -> None:
    snapshot = SimpleNamespace(
        media=[
            SimpleNamespace(
                media_id="clip-1",
                user_context="Emir falls after the kick",
                source_filename="football.mov",
                analysis={
                    "description": "Football practice",
                    "best_moments": [
                        {"start_s": 4.0, "end_s": 5.0, "description": "Funny stumble"},
                        {"start_s": 20.0, "end_s": 21.0, "description": "Outside the cut"},
                    ],
                },
            )
        ]
    )

    moments = trusted_visual_moments(
        snapshot,
        [
            {
                "media_id": "clip-1",
                "source_start_s": 3.5,
                "source_end_s": 6.0,
                "output_start_s": 8.0,
                "output_end_s": 10.5,
            }
        ],
    )

    assert moments == [
        {
            "start_s": 8.0,
            "end_s": 10.5,
            "description": "Funny stumble · Football practice",
            "media_id": "clip-1",
        }
    ]


def test_explicit_sfx_materialization_enforces_grounding_spacing_keepout_and_cap() -> None:
    intent = LicensedSfxIntent(effect_id="sfx-fah", max_placements=3)
    effect = {
        "id": "sfx-fah",
        "name": "Fah",
        "audio_gcs_path": "sound-effects/sfx-fah/fah.mp3",
        "duration_s": 0.7,
    }
    moments = [
        {"start_s": 1.0, "end_s": 2.0, "description": "stumble"},
        {"start_s": 2.4, "end_s": 3.0, "description": "laugh"},
        {"start_s": 5.0, "end_s": 6.0, "description": "miss"},
        {"start_s": 8.0, "end_s": 9.8, "description": "reaction"},
    ]
    suggestions = [
        {"effect_id": "wrong", "at_s": 1.0},
        {"effect_id": "sfx-fah", "at_s": 1.1, "gain": 4.0},
        {"effect_id": "sfx-fah", "at_s": 2.4},  # too close to 1.0
        {"effect_id": "SFX-FAH", "at_s": 3.0},
        {"effect_id": "sfx-fah", "at_s": 5.1},
        {"effect_id": "sfx-fah", "at_s": 8.0},
        {"effect_id": "sfx-fah", "at_s": 9.8},  # final keepout
        {"effect_id": "sfx-fah", "at_s": 7.2},  # not a trusted mark
    ]

    placements = materialize_explicit_sfx_placements(
        suggestions,
        intent=intent,
        effect=effect,
        visual_moments=moments,
        duration_s=10.0,
    )

    assert [placement["at_s"] for placement in placements] == [1.0, 3.0, 5.0]
    assert all(placement["sound_effect_id"] == "sfx-fah" for placement in placements)
    assert all(placement["smart_role"] == "funny_moments" for placement in placements)
    assert placements[0]["gain"] == 1.5


def test_explicit_sfx_materialization_rejects_catalog_identity_drift() -> None:
    assert (
        materialize_explicit_sfx_placements(
            [{"effect_id": "sfx-fah", "at_s": 1.0}],
            intent=LicensedSfxIntent(effect_id="sfx-fah"),
            effect={"id": "replacement", "audio_gcs_path": "sound-effects/replacement.mp3"},
            visual_moments=[{"start_s": 1.0, "end_s": 2.0, "description": "fall"}],
            duration_s=5.0,
        )
        == []
    )


def test_explicit_sfx_materialization_matches_catalog_ids_case_insensitively() -> None:
    placements = materialize_explicit_sfx_placements(
        [{"effect_id": "FAH-CATALOG", "at_s": 1.0}],
        intent=LicensedSfxIntent(effect_id=" fah-catalog "),
        effect={
            "id": "Fah-Catalog",
            "name": "Fah",
            "audio_gcs_path": "sound-effects/fah.mp3",
        },
        visual_moments=[{"start_s": 1.0, "end_s": 2.0, "description": "missed kick"}],
        duration_s=5.0,
    )

    assert len(placements) == 1
    assert placements[0]["sound_effect_id"] == "Fah-Catalog"


def test_trusted_visual_moments_fail_closed_for_malformed_or_unrelated_rows() -> None:
    snapshot = SimpleNamespace(
        media=[SimpleNamespace(media_id="clip-1", analysis={"description": "A joke"})]
    )

    assert (
        trusted_visual_moments(
            snapshot,
            [
                {"media_id": "missing", "output_start_s": 0, "output_end_s": 1},
                {"media_id": "clip-1", "output_start_s": "bad", "output_end_s": 1},
                {"media_id": "clip-1", "output_start_s": 2, "output_end_s": 1},
            ],
        )
        == []
    )


def test_explicit_sfx_materialization_rejects_empty_or_nonfinite_marks() -> None:
    intent = LicensedSfxIntent(effect_id="sfx-fah")
    effect = {"id": "sfx-fah", "audio_gcs_path": "sound-effects/fah.mp3"}

    assert (
        materialize_explicit_sfx_placements(
            [{"effect_id": "sfx-fah", "at_s": float("nan")}],
            intent=intent,
            effect=effect,
            visual_moments=[],
            duration_s=10,
        )
        == []
    )
