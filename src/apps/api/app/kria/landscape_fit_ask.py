"""How the creator asked sideways clips to sit in a vertical video (KRI-1027).

"Some of the clips are horizontal and some vertical, make it all vertical" landed in
the Creative Brief as a ``style`` requirement with no facts. Nothing read it: the
item's remembered bars/crop preference (``PlanItem.landscape_fit``, default "fit")
preselected black bars on the confirm screen, the app sent that default back with
the approval, and the montage letterboxed every sideways clip.

The model that reads the message records the meaning (KRI-667): the shared brief
section (Main Creator v101 / Brief Extractor v40) puts ``{"landscape_fit": "fill"}``
on a style requirement asking for the sideways clips cropped to fill the frame ("make
it all vertical", "no black bars") and ``{"landscape_fit": "fit"}`` on one asking to
keep them whole with black bars ("keep the black bars", "don't crop the clips"). The
server checks that field and adds no word reading of its own; the one fallback is
the KRI-547 vertical / full-screen / face-in-frame reader Talking already applies to
the same words (`speaker_framing_ask.requirement_asks_vertical_framing`), which a
requirement recorded before v101 can still match ("make it vertical", "9:16", "no
black bars"). A requirement that says neither, as prod stored M1's r2, asks nothing.

The newest live requirement that asks either wins (a later correction replaces an
earlier ask). The result preselects the confirm screen's shape
(`app.services.render_shape.apply_creator_ask`), so the app sends it back with the
approval; tells the phone montage worker to frame sideways clips around their faces
(`app.pipeline.phone_montage_framing`); and answers the ask from the render's
receipt (`judge_landscape_fit`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from app.kria.reply_language import say
from app.kria.speaker_framing_ask import requirement_asks_vertical_framing

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement, CreativeBrief

LandscapeFit = Literal["fill", "fit"]

LANDSCAPE_FIT_FACT = "landscape_fit"
_FITS: frozenset[str] = frozenset({"fill", "fit"})


def requirement_landscape_fit(req: BriefRequirement) -> LandscapeFit | None:
    """The bars/crop a live requirement asks for: its recorded ``landscape_fit`` fact,
    else "fill" for a KRI-547 vertical-framing ask, else None."""
    if not req.live:
        return None
    recorded = req.facts.get(LANDSCAPE_FIT_FACT)
    if recorded in _FITS:
        return recorded  # type: ignore[return-value]
    if requirement_asks_vertical_framing(req):
        return "fill"
    return None


@dataclass(frozen=True)
class LandscapeFitAsk:
    """What the creator asked for and which requirements said it.

    ``fit`` is the newest ask; ``requirement_ids`` are the live requirements that
    ask for that same thing (an earlier, opposite ask is replaced, not cited)."""

    fit: LandscapeFit
    requirement_ids: tuple[str, ...]


def landscape_fit_ask(requirements: Iterable[BriefRequirement]) -> LandscapeFitAsk | None:
    asks = [(req.id, fit) for req in requirements if (fit := requirement_landscape_fit(req))]
    if not asks:
        return None
    newest = asks[-1][1]
    return LandscapeFitAsk(newest, tuple(rid for rid, fit in asks if fit == newest))


def brief_landscape_fit_ask(brief: CreativeBrief | None) -> LandscapeFitAsk | None:
    """`landscape_fit_ask` over a brief's live requirements (None without one)."""
    return landscape_fit_ask(brief.live()) if brief is not None else None


# ── the receipt ────────────────────────────────────────────────────────────────
# A montage's bars/crop ask is answered from the phone render's framing receipt
# (`phone_montage_framing.MontageFraming.receipt`, ``variant["landscape_framing"]``).


@dataclass(frozen=True)
class FitVerdict:
    status: Literal["met", "partial"]
    reason: str
    checked: bool


def _clips(n: int) -> str:
    return say(
        en=f"{n} sideways clip" if n == 1 else f"{n} sideways clips",
        tr=f"{n} yatay klip",
    )


def _planned(fit: LandscapeFit) -> FitVerdict:
    if fit == "fill":
        reason = say(
            en=(
                "I'll crop the sideways clips to fill the vertical frame, keeping faces in it, "
                "and check it on the finished video"
            ),
            tr=(
                "Yatay klipleri dikey ekranı dolduracak şekilde kırpacağım, yüzleri kadrajda "
                "tutacağım ve bitmiş videoda kontrol edeceğim"
            ),
        )
    else:
        reason = say(
            en=(
                "I'll keep the sideways clips whole with black bars and check it on the "
                "finished video"
            ),
            tr=(
                "Yatay klipleri siyah bantlarla bütün bırakacağım ve bitmiş videoda kontrol "
                "edeceğim"
            ),
        )
    return FitVerdict("partial", reason, checked=False)


def judge_landscape_fit(
    fit: LandscapeFit,
    *,
    receipt: Mapping[str, Any] | None,
    rendered: bool,
    variant_fit: str | None = None,
) -> FitVerdict:
    """What the creator's bars/crop ask got. ``receipt`` is the finished montage's
    framing receipt (None on a draft, or on a render that did not record one);
    ``variant_fit`` is the bars/crop the variant renders now (an editor Save can
    change it after the receipt was written)."""
    if receipt is None:
        if not rendered:
            return _planned(fit)
        return FitVerdict(
            "partial",
            say(
                en=(
                    "This video didn't record how it framed the sideways clips, so I can't check it"
                ),
                tr=(
                    "Bu video yatay klipleri nasıl kadrajladığını kaydetmedi, o yüzden kontrol "
                    "edemiyorum"
                ),
            ),
            checked=False,
        )
    rows = [row for row in receipt.get("clips") or [] if isinstance(row, Mapping)]
    if receipt.get("output_orientation") == "landscape":
        return FitVerdict(
            "partial",
            say(
                en=(
                    "You chose a landscape video, so the clips fill a wide frame, not a "
                    "vertical one"
                ),
                tr="Yatay video seçtin, o yüzden klipler dikey değil geniş bir kadrajı dolduruyor",
            ),
            checked=True,
        )
    rendered_fit = variant_fit if variant_fit in ("fit", "fill") else receipt.get("landscape_fit")
    changed = rendered_fit != receipt.get("landscape_fit")
    n = len(rows)
    if n == 0:
        return FitVerdict(
            "met",
            say(
                en="All your clips are already vertical, so they fill the frame",
                tr="Kliplerinin hepsi zaten dikey, o yüzden ekranı dolduruyorlar",
            ),
            checked=True,
        )
    if rendered_fit == "fit":
        if fit == "fit":
            return FitVerdict(
                "met",
                say(
                    en=f"Kept black bars on the {_clips(n)}, so nothing is cropped",
                    tr=f"{_clips(n)} siyah bantlarla bütün kaldı, hiçbir şey kırpılmadı",
                ),
                checked=True,
            )
        return FitVerdict(
            "partial",
            say(
                en=f"You chose black bars, so the {_clips(n)} keep them",
                tr=f"Siyah bantları seçtin, o yüzden {_clips(n)} bantlı kaldı",
            ),
            checked=True,
        )
    if fit == "fit":
        return FitVerdict(
            "partial",
            say(
                en=f"You chose crop, so the {_clips(n)} are cropped to fill the frame",
                tr=f"Kırpmayı seçtin, o yüzden {_clips(n)} ekranı dolduracak şekilde kırpıldı",
            ),
            checked=True,
        )
    if changed:
        # Switched to crop in the editor after a bars render: the engine's centre crop.
        return FitVerdict(
            "met",
            say(
                en=f"Cropped the {_clips(n)} to fill the vertical frame",
                tr=f"{_clips(n)} dikey ekranı dolduracak şekilde kırpıldı",
            ),
            checked=True,
        )
    barred = sum(1 for row in rows if row.get("mode") == "letterbox")
    faced = sum(1 for row in rows if row.get("mode") == "face_fill")
    cropped = n - barred
    if barred:
        return FitVerdict(
            "partial",
            say(
                en=(
                    f"Cropped {cropped} of {_clips(n)} to vertical; kept black bars on {barred} "
                    "because the face moves too far for one vertical crop to keep it"
                ),
                tr=(
                    f"{_clips(n)} içinden {cropped} tanesi dikey kırpıldı; {barred} tanesinde yüz "
                    "tek bir dikey kadraja sığmayacak kadar hareket ettiği için siyah bant kaldı"
                ),
            ),
            checked=True,
        )
    follows = (
        say(
            en=f"; the crop follows the main face on {faced}",
            tr=f"; {faced} tanesinde kadraj ana yüzü takip ediyor",
        )
        if faced
        else ""
    )
    return FitVerdict(
        "met",
        say(
            en=f"Cropped the {_clips(n)} to fill the vertical frame{follows}",
            tr=f"{_clips(n)} dikey ekranı dolduracak şekilde kırpıldı{follows}",
        ),
        checked=True,
    )


__all__ = [
    "LANDSCAPE_FIT_FACT",
    "FitVerdict",
    "LandscapeFit",
    "LandscapeFitAsk",
    "brief_landscape_fit_ask",
    "judge_landscape_fit",
    "landscape_fit_ask",
    "requirement_landscape_fit",
]
