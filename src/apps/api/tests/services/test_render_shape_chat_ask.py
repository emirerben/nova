"""KRI-1027: the creator's bars/crop words preselect the confirm screen's shape."""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.services.render_shape import RenderShapeOffer, apply_creator_ask, readable_brief

MONTAGE = RenderShapeOffer(
    orientations=("portrait", "landscape"),
    fit_choices=("fit", "fill"),
    default_orientation="landscape",
    default_fit="fit",
)


def _brief(*asks: tuple[str, str | None]) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id=f"r{i}",
                kind="style",
                scope="global",
                description=text,
                facts={"landscape_fit": fit} if fit else {},
            )
            for i, (text, fit) in enumerate(asks, 1)
        ],
    )


def test_vertical_ask_preselects_crop_on_a_vertical_canvas():
    offer = apply_creator_ask(MONTAGE, _brief(("make it all vertical", "fill")))
    assert (offer.default_orientation, offer.default_fit) == ("portrait", "fill")
    assert (offer.orientations, offer.fit_choices) == (MONTAGE.orientations, MONTAGE.fit_choices)


def test_keep_bars_preselects_bars_on_a_vertical_canvas():
    offer = apply_creator_ask(MONTAGE, _brief(("keep the black bars", "fit")))
    assert (offer.default_orientation, offer.default_fit) == ("portrait", "fit")


def test_no_ask_or_no_fit_choice_leaves_the_offer_alone():
    assert apply_creator_ask(MONTAGE, _brief(("faster cuts", None))) is MONTAGE
    assert apply_creator_ask(MONTAGE, None) is MONTAGE
    closed = RenderShapeOffer(("portrait",), (), "portrait", "fit", reason="format_unsupported")
    assert apply_creator_ask(closed, _brief(("make it all vertical", "fill"))) is closed


def test_readable_brief_never_raises():
    brief = _brief(("make it all vertical", "fill"))
    binding = BriefBinding.create("thread", brief, full_creator_request="make it all vertical")
    assert readable_brief(binding) == brief
    assert readable_brief(binding.model_dump(mode="json")) == brief
    tampered = {**binding.model_dump(mode="json"), "digest": "0" * 64}
    assert readable_brief(tampered) is None
    assert readable_brief(None) is None
    assert readable_brief({"nonsense": True}) is None
