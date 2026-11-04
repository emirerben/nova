"""Generation-pinned phone styling adapter, shared by all initial writers."""

from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

import structlog

from app.schemas.phone_text_style import (
    CAPTION_PREFERRED_FONT,
    CURATED_PHONE_FONTS,
    DEFAULT_SHADOW_ENABLED,
    DEFAULT_STROKE_WIDTH,
    PHONE_TEXT_STYLE_VERSION,
    TextStyleRule,
    effective_text_rule,
    preferences_from_direction,
)

log = structlog.get_logger()

PHONE_TEXT_STYLE_FIELD = "phone_text_style"
PHONE_TEXT_STYLE_CLIENT_PROTOCOL = 10
_POLICY: ContextVar[dict | None] = ContextVar("phone_text_style", default=None)
# WCAG AA for body text, the bar a caption over a speaker background must reach (Green 5).
READABLE_CONTRAST = 4.5
BACKGROUND_OUTLINE_PX = 4


def require_phone_text_style_client(policy, *, require_header=False):
    from app.services.client_protocol import current_client_protocol, in_http_request

    if not isinstance(policy, dict) or policy.get("version") != PHONE_TEXT_STYLE_VERSION:
        return
    protocol = current_client_protocol()
    if (require_header or in_http_request() and protocol is not None) and (
        protocol or 0
    ) < PHONE_TEXT_STYLE_CLIENT_PROTOCOL:
        from fastapi import HTTPException

        raise HTTPException(
            426,
            detail={
                "code": "client_update_required",
                "feature": "text_preferences",
                "minimum_client_protocol": PHONE_TEXT_STYLE_CLIENT_PROTOCOL,
            },
        )


def requested_style(strategy: dict, request: str) -> dict:
    """Read only unambiguous current-video asks, never treating them as memory."""
    all_text = {}
    for source, dest in (
        ("font_family", "font_family"),
        ("text_color", "color"),
        ("stroke_width", "stroke_width"),
        ("shadow_enabled", "shadow_enabled"),
    ):
        if strategy.get(source) is not None:
            all_text[dest] = strategy[source]
    from app.schemas.phone_text_style import explicit_text_rule

    resolved = explicit_text_rule(request)
    if resolved is not None and resolved[0] != "all_text":
        # Strategy's legacy knobs have no role field. A scoped wording match
        # supplies that missing scope, rather than leaking the ask to every lane.
        for property_name in resolved[1]:
            all_text.pop(property_name, None)
    result = {"all_text": TextStyleRule.model_validate(all_text).values()} if all_text else {}
    if resolved is not None:
        role, values = resolved
        result[role] = {**result.get(role, {}), **values}
    if strategy.get("label_position"):
        result["label"] = {"position": strategy["label_position"]}
    return result


def approval_text_policy(strategy: dict) -> dict | None:
    """The policy `stamp_phone_text_style` gives a job approved now, for a pre-approval plan.

    KRI-1024: the worker plans under it, so a plan run without it takes a typography
    branch the worker never takes. The creator's saved direction is left out: it only
    narrows the fonts the role-scoped pass picks after the plan.
    """
    from app.config import settings

    if not settings.phone_text_styling_enabled:
        return None
    return {
        "version": PHONE_TEXT_STYLE_VERSION,
        "preferences": {"all_text": {}},
        "requested": requested_style(strategy or {}, ""),
    }


def stamp_phone_text_style(job) -> None:
    from app.config import settings
    from app.services.creator_direction_snapshot import typed_overrides_from_container

    current = dict(job.assembly_plan or {})
    if not settings.phone_text_styling_enabled or PHONE_TEXT_STYLE_FIELD in current:
        return
    # A legacy saved generation never upgrades on retry.
    if current.get("variants") or current.get("_device_render_v1"):
        return
    typed = typed_overrides_from_container(current)
    from app.services.creator_direction_snapshot import snapshot_from_container

    direction = snapshot_from_container(current) or {}
    # Inferred TikTok/persona styles are not confirmed personal rules.
    if direction.get("compatibility_expires_at") is not None:
        typed = direction.get("typed_overrides_without_compatibility") or {}
    preferences = preferences_from_direction(typed)
    global_rule = preferences.setdefault("all_text", {})
    for source, dest in (
        ("font_family", "font_family"),
        ("shadow_enabled", "shadow_enabled"),
        ("stroke_width", "stroke_width"),
        ("text_color", "color"),
    ):
        if source in typed:
            global_rule.setdefault(dest, typed[source])
    if global_rule.get("automatic_font") is True:
        global_rule.pop("font_family", None)
    candidates = job.all_candidates or {}
    stamped = {
        "version": PHONE_TEXT_STYLE_VERSION,
        "preferences": preferences,
        "requested": requested_style(
            candidates.get("creator_strategy") or {}, str(candidates.get("creator_request") or "")
        ),
    }
    # KRI-1177: the look the creator typed onto the brief rides beside the parsed `requested`.
    ask = _brief_caption_ask(current)
    if ask is not None:
        stamped["caption_ask"] = ask.as_dict()
    current[PHONE_TEXT_STYLE_FIELD] = stamped
    job.assembly_plan = current


def _brief_caption_ask(container: dict):
    """The caption look the job's pinned brief asks for (``creator_brief_binding``), or None.

    An unreadable binding is "no ask": the first draft then renders exactly as before."""
    raw_binding = container.get("creator_brief_binding")
    if not raw_binding:
        return None
    from app.kria.brief_binding import BriefBinding
    from app.kria.caption_look_ask import brief_caption_look

    try:
        return brief_caption_look(BriefBinding.model_validate(raw_binding).resolve())
    except Exception as exc:  # noqa: BLE001 - an unreadable binding is "no ask"
        log.warning("phone_text_style.caption_ask_binding_unreadable", error=str(exc)[:200])
        return None


def style_rows(
    rows: list[dict],
    policy: dict | None,
    *,
    canvas=None,
    selected_fonts=None,
    excluded_boxes=None,
    background_frames=None,
) -> list[dict]:
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.phone_text_style import resolve_phone_text_style

    if not policy:
        return copy.deepcopy(rows)
    if policy.get("version") != PHONE_TEXT_STYLE_VERSION:
        raise ValueError("This text styling version is not supported")
    return resolve_phone_text_style(
        rows,
        preferences=policy.get("preferences"),
        requested=policy.get("requested"),
        selected_fonts=selected_fonts,
        canvas=canvas or PORTRAIT,
        excluded_boxes=excluded_boxes,
        background_frames=background_frames,
    )


def select_fonts(rows: list[dict], policy: dict, *, mood: str = "", job_id: str = "") -> dict:
    from app.agents._model_client import default_client
    from app.agents._runtime import RunContext
    from app.agents.phone_typography import PhoneTypographyAgent, PhoneTypographyInput
    from app.pipeline.phone_text_style import _covers, coverage_text, text_role
    from app.services.pipeline_trace import record_pipeline_event

    roles = {}
    preferences, requested = policy.get("preferences") or {}, policy.get("requested") or {}
    for role in sorted({text_role(row) for row in rows}):
        rule = effective_text_rule(preferences, requested, role)
        if rule.get("font_family"):
            continue
        texts = [
            coverage_text(row)
            for row in rows
            if text_role(row) == role
            and "font_family"
            not in ((row.get("source_params") or {}).get("creator_style_fields") or [])
        ]
        eligible = [
            font
            for font in dict.fromkeys([*rule.get("favorite_fonts", []), *CURATED_PHONE_FONTS])
            if font not in rule.get("excluded_fonts", [])
            and all(_covers(font, text) for text in texts)
        ]
        favorites = [font for font in rule.get("favorite_fonts", []) if font in eligible]
        roles[role] = {"eligible_fonts": favorites or eligible, "wording": " ".join(texts)[:1200]}
    if not roles:
        return {}
    try:
        result = PhoneTypographyAgent(default_client()).run(
            PhoneTypographyInput(roles=roles, mood=mood[:300]), ctx=RunContext(job_id=job_id)
        )
        return {
            role: font
            for role, font in result.fonts.items()
            if role in roles and font in roles[role]["eligible_fonts"]
        }
    except Exception:  # noqa: BLE001 - bounded best-effort aesthetic selection
        record_pipeline_event(
            "text",
            "font_selection_fallback",
            {"roles": list(roles), "version": PHONE_TEXT_STYLE_VERSION},
        )
        return {}


@contextmanager
def phone_text_policy_scope(policy: dict | None):
    from app.pipeline.phone_text_style import phone_text_measurement_scope

    token = _POLICY.set(copy.deepcopy(policy))
    try:
        with phone_text_measurement_scope():
            yield
    finally:
        _POLICY.reset(token)


def current_phone_text_policy() -> dict | None:
    return copy.deepcopy(_POLICY.get())


def phone_text_generation(function):
    """Scope immutable direction and selection caches to one initial phone writer."""

    @wraps(function)
    def scoped(job_id, snapshot, *args, **kwargs):
        with phone_text_policy_scope(snapshot.get(PHONE_TEXT_STYLE_FIELD)):
            policy = _POLICY.get()
            if policy is not None:
                from app.services.phone_sources import PHONE_SOURCES_FIELD

                policy["_source_bindings"] = snapshot.get(PHONE_SOURCES_FIELD) or []
            return function(job_id, snapshot, *args, **kwargs)

    return scoped


def generation_style_rows(
    rows: list[dict], *, canvas=None, job_id="", mood="", excluded_boxes=None
) -> list[dict]:
    policy = _POLICY.get()
    if not policy or not rows:
        return copy.deepcopy(rows)
    from app.pipeline.canvas import PORTRAIT

    _ensure_generation_environment(canvas or PORTRAIT)
    from app.pipeline.phone_text_style import text_role

    resolved_rows = policy.setdefault("_resolved_rows", {})
    inputs = policy.setdefault("_row_inputs", {})
    if all(
        row["id"] in resolved_rows
        and (row == inputs.get(row["id"]) or row == resolved_rows[row["id"]])
        for row in rows
    ):
        return [copy.deepcopy(resolved_rows[row["id"]]) for row in rows]

    selected = policy.setdefault("selected_fonts", {})
    attempted = set(policy.get("selection_roles") or selected)
    missing = [row for row in rows if text_role(row) not in attempted]
    if missing and int(policy.get("selection_calls", 0)) < 2:
        selected.update(select_fonts(missing, policy, mood=mood, job_id=job_id))
        policy["selection_calls"] = int(policy.get("selection_calls", 0)) + 1
        policy["selection_roles"] = sorted(attempted | {text_role(row) for row in missing})
    from app.pipeline.phone_text_style import animation_box, measure_box

    incoming_ids = {row["id"] for row in rows}
    previous_boxes = [
        {
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "box": animation_box(row, measure_box(row, canvas or PORTRAIT), canvas or PORTRAIT),
            "kind": "text",
        }
        for key, row in resolved_rows.items()
        if key not in incoming_ids
    ]
    resolved = style_rows(
        rows,
        policy,
        canvas=canvas,
        selected_fonts=policy["selected_fonts"],
        excluded_boxes=[
            *(excluded_boxes or []),
            *policy.get("_face_boxes", []),
            *policy.get("_caption_boxes", []),
            *previous_boxes,
        ],
        background_frames=policy.get("_background_frames", []),
    )
    for original, row in zip(rows, resolved, strict=True):
        inputs.setdefault(row["id"], copy.deepcopy(original))
        resolved_rows[row["id"]] = copy.deepcopy(row)
    return resolved


def generation_resolved_rows(role: str) -> list[dict]:
    from app.pipeline.phone_text_style import text_role

    policy = _POLICY.get() or {}
    return [
        copy.deepcopy(row)
        for row in policy.get("_resolved_rows", {}).values()
        if text_role(row) == role
    ]


def generation_caption_look(cues: list[dict], look=None, *, canvas=None):
    from dataclasses import replace

    from app.pipeline.phone_captions import PhoneCaptionLook
    from app.pipeline.phone_text_style import drawable_family

    policy = _POLICY.get()
    if not policy:
        return look
    if look is not None:
        return look  # an editor-authored appearance is already resolved
    if policy.get("caption_look") is not None:
        return policy["caption_look"]
    from app.pipeline.canvas import PORTRAIT

    _ensure_generation_environment(canvas or PORTRAIT)
    rule = effective_text_rule(
        policy.get("preferences") or {}, policy.get("requested") or {}, "caption"
    )
    # KRI-1177: the look the creator typed onto the brief overrides the default look on the first
    # draft -- colour, spot and size are theirs, even over a face or the corner speaker.
    from app.kria.caption_look_ask import CAPTION_ASK_SIZE_PX, CaptionLookAsk

    ask = CaptionLookAsk.from_dict(policy.get("caption_ask"))
    if ask is not None:
        if ask.color is not None:
            rule["color"] = ask.color
        if ask.position is not None:
            rule["position"] = ask.position
    if (
        float(rule.get("stroke_width", 0)) > 12
        or not float(rule.get("stroke_width", 0)).is_integer()
    ):
        from app.pipeline.phone_text_style import _style_decline

        raise _style_decline(
            "Caption outlines use whole pixel widths from 0 to 12. Choose a supported width."
        )
    texts = [str(cue.get("text") or "") for cue in cues]
    selected = policy.get("selected_fonts") or {}
    if (
        "caption" not in selected
        and not rule.get("font_family")
        and int(policy.get("selection_calls", 0)) < 2
    ):
        selected.update(
            select_fonts(
                [
                    {"id": f"caption-{index}", "text": cue.get("text", "")}
                    for index, cue in enumerate(cues)
                ],
                policy,
            )
        )
        policy["selected_fonts"] = selected
        policy["selection_calls"] = int(policy.get("selection_calls", 0)) + 1
    candidates = (
        [rule["font_family"]]
        if rule.get("font_family")
        else list(
            dict.fromkeys(
                [
                    selected.get("caption"),
                    *rule.get("favorite_fonts", []),
                    CAPTION_PREFERRED_FONT,
                    *CURATED_PHONE_FONTS,
                ]
            )
        )
    )
    excluded = rule.get("excluded_fonts", [])
    font = next(
        (
            d
            for f in candidates
            if f and f not in excluded and (d := drawable_family(f, texts, excluded)) is not None
        ),
        None,
    )
    if font is None:
        from app.pipeline.phone_text_style import _style_decline

        raise _style_decline(
            "The eligible fonts cannot draw all caption text. Choose another font."
        )
    resolved = replace(
        PhoneCaptionLook(),
        font_family=font,
        outline_px=rule.get("stroke_width", DEFAULT_STROKE_WIDTH),
        shadow_enabled=rule.get("shadow_enabled", DEFAULT_SHADOW_ENABLED),
        text_color=rule.get("color", "#FFFFFF"),
        max_width_frac=0.68,
        shape_text=True,
    )
    # The asked size is drawn as given: the rows below mark it creator-authored, so the fit
    # cascade can neither shrink nor re-fit it (a centred line used to fall to 30 px).
    asked_px = CAPTION_ASK_SIZE_PX[ask.size] if ask is not None and ask.size is not None else None
    spot_asked = ask is not None and ask.position is not None
    authored = [*(["max_width_frac"] if spot_asked else []), *(["size_px"] if asked_px else [])]
    if asked_px is not None:
        resolved = replace(resolved, text_size_px=asked_px)
    over_face = False
    if rule.get("position"):
        from app.schemas.text_style_intent import LABEL_ANCHORS

        x, y, alignment = LABEL_ANCHORS[rule["position"]]
        resolved = replace(
            resolved,
            position_x_frac=0.06 if alignment == "left" else 0.82 if alignment == "right" else x,
            position_y_frac=min(y, 0.70),
            text_anchor=alignment,
        )
    if cues:
        from app.agents._schemas.text_element import TextElement
        from app.pipeline.canvas import PORTRAIT
        from app.pipeline.phone_text_style import (
            Obstacle,
            _place,
            measure_box,
            position_requested,
        )

        canvas = canvas or PORTRAIT
        rows = [
            TextElement(
                id=f"caption-{index}",
                text=str(cue["text"]),
                start_s=float(cue.get("start_s", 0)),
                end_s=float(cue.get("end_s", 0)),
                font_family=font,
                size_px=resolved.text_size_px,
                color=resolved.text_color,
                stroke_width=resolved.outline_px,
                shadow_enabled=resolved.shadow_enabled,
                max_width_frac=0.68,
                effect="pop-in",
                source_params={
                    "phone_text_style_version": 1,
                    **({"creator_style_fields": authored} if authored else {}),
                },
            ).model_dump(mode="json", exclude_none=True)
            for index, cue in enumerate(cues)
            if str(cue.get("text") or "").strip()
            and float(cue.get("end_s", 0)) > float(cue.get("start_s", 0))
        ]
        if rows:
            row = max(rows, key=lambda r: measure_box(r, canvas)[3] - measure_box(r, canvas)[1])
            row["start_s"], row["end_s"] = (
                min(r["start_s"] for r in rows),
                max(r["end_s"] for r in rows),
            )
            from app.pipeline.phone_captions import watermark_keepout_rect

            mark = watermark_keepout_rect(canvas.width, canvas.height)
            obstacles = [
                *(
                    Obstacle(face["box"] if isinstance(face, dict) else face, "face")
                    for face in policy.get("_face_boxes") or []
                ),
                Obstacle(
                    (
                        mark[0] / canvas.width,
                        mark[1] / canvas.height,
                        mark[2] / canvas.width,
                        mark[3] / canvas.height,
                    ),
                    "watermark",
                ),
            ]
            _place(
                row,
                role="caption",
                wanted=rule,
                canvas=canvas,
                block_rows=rows,
                # An asked spot has nothing to dodge: with obstacles the fit would still lift it
                # up its corner (0.06-0.18) off a face or the watermark; the compiler clears the
                # watermark itself and `over_face` below says what the spot covers.
                obstacles=[] if spot_asked else obstacles,
                position_fixed=spot_asked or position_requested(policy.get("requested"), "caption"),
            )
            resolved = replace(
                resolved,
                text_size_px=asked_px or row["size_px"],
                position_x_frac=row["x_frac"],
                position_y_frac=row["y_frac"],
                text_anchor=row["alignment"],
                max_width_frac=row["max_width_frac"],
            )
            if "color" not in rule:
                # Green 5: kept so a background that arrives later re-picks the colour.
                policy["_caption_auto_fill"] = {
                    "box": list(measure_box(row, canvas)),
                    "start_s": row["start_s"],
                    "end_s": row["end_s"],
                    "outline_asked": "stroke_width" in rule,
                }
                if policy.get("_background_frames"):
                    resolved = _auto_caption_colour(resolved, policy)
            if ask is not None:
                over_face = _caption_over_face(row, rows, policy.get("_face_boxes") or [], canvas)
    if ask is not None:
        policy["_caption_look_receipt"] = {
            **ask.as_dict(),
            "size_px": resolved.text_size_px,
            # Where the block was drawn, so a later move (a KRI-827 mark, an editor Save) is seen.
            "position_x_frac": round(float(resolved.position_x_frac), 5),
            "position_y_frac": round(float(resolved.position_y_frac), 5),
            "over_face": over_face,
        }
    policy["caption_look"] = resolved
    return resolved


def _caption_over_face(row: dict, rows: list[dict], faces: list, canvas) -> bool:
    """True when the caption block ``row`` draws overlaps a sampled face while a cue is on."""
    if not faces:
        return False
    from app.pipeline.phone_text_style import _overlap, _same_window, animation_box, measure_box

    block = animation_box(row, measure_box(dict(row), canvas), canvas)
    return any(
        _overlap(block, face["box"] if isinstance(face, dict) else face)
        and (not isinstance(face, dict) or any(_same_window(face, r) for r in rows))
        for face in faces
    )


def generation_caption_cues(cues: list[dict], *, language: str | None = None) -> list[dict]:
    """KRI-1177: ``cues`` in the case the creator asked for, in the cue text itself (the editor
    shows it and a Save keeps it); ``cues`` unchanged when no case was asked."""
    from app.kria.caption_look_ask import CaptionLookAsk, apply_caption_case

    ask = CaptionLookAsk.from_dict((_POLICY.get() or {}).get("caption_ask"))
    if ask is None or ask.case is None:
        return cues
    return apply_caption_case(cues, ask.case, language)


def remember_caption_bounds(layers, *, canvas):
    policy = _POLICY.get()
    if not policy:
        return
    # The actual compiler has already rewrapped around the watermark. Reserve
    # those final ink bounds, not the original cue's nominal anchor.
    from app.pipeline.phone_captions import caption_ink_box
    from app.pipeline.phone_text_style import animation_box
    from app.pipeline.text_overlay_skia import _resolve_typeface_for_overlay

    look = policy.get("caption_look")
    if look is None:
        return
    # KRI-1192: measure in the face that DRAWS the caption text (Hangul/Arabic runs carry the
    # script face's glyphs), which the glyph-safe resolution picks from the text itself.
    # Latin text resolves to `look.font_family` exactly as before.
    text = " ".join(run.text for layer in layers for run in layer.runs if run.text)
    typeface = _resolve_typeface_for_overlay(
        {"font_family": look.font_family, "text": text}
    ).typeface
    boxes = []
    for layer in layers:
        ink = [caption_ink_box(run, typeface) for run in layer.runs]
        ink = [box for box in ink if box is not None]
        if ink:
            boxes.append(
                {
                    "start_s": layer.start,
                    "end_s": layer.end,
                    "box": animation_box(
                        {
                            "effect": layer.effect,
                            "x_frac": layer.anchor_x / canvas.width,
                            "y_frac": layer.anchor_y / canvas.height,
                            "size_px": look.text_size_px,
                            "font_family": look.font_family,
                            "text": " ".join(run.text for run in layer.runs),
                            "stroke_width": look.outline_px,
                            "shadow_enabled": look.shadow_enabled,
                        },
                        (
                            min(b[0] for b in ink) / canvas.width,
                            min(b[1] for b in ink) / canvas.height,
                            max(b[2] for b in ink) / canvas.width,
                            max(b[3] for b in ink) / canvas.height,
                        ),
                        canvas,
                    ),
                    "kind": "caption",
                }
            )
    policy["_caption_boxes"] = boxes


def register_generation_timeline(clips):
    policy = _POLICY.get()
    if not policy or not policy.get("_source_bindings") or policy.get("_environment_sampled"):
        return
    policy["_sample_windows"] = [
        {
            "media_id": clip.source_asset_id,
            "source_start": clip.source_start,
            "source_duration": clip.source_duration,
            "start_s": clip.timeline_start,
            "end_s": clip.timeline_start + clip.source_duration / clip.rate,
            "scale": clip.transform.scale,
            "x": clip.transform.position_x,
            "y": clip.transform.position_y,
            "rotation": clip.transform.rotation_degrees,
        }
        for clip in clips
    ]


def _auto_caption_colour(look, policy: dict):
    """``look`` with the automatic caption colour for the frames behind the caption block
    (`automatic_fill`). Green 5: over a speaker background no plain colour may be readable
    everywhere (bright terraces and a dark shirt behind one caption): then white with a black
    outline, unless the creator chose the outline themselves."""
    from dataclasses import replace

    from app.pipeline.phone_text_background import automatic_fill

    auto = policy["_caption_auto_fill"]
    fill, score = automatic_fill(
        tuple(auto["box"]),
        [
            frame
            for frame in policy.get("_background_frames") or []
            if frame["start_s"] < auto["end_s"] and auto["start_s"] < frame["end_s"]
        ],
    )
    look = replace(look, text_color=fill)
    outline_needed = score < READABLE_CONTRAST and not auto["outline_asked"]
    if policy.get("_speaker_background") and outline_needed:
        look = replace(
            look,
            text_color="#FFFFFF",
            outline_px=max(look.outline_px, BACKGROUND_OUTLINE_PX),
            stroke_color="#000000",
        )
    return look


def register_generation_background(background, canvas=None) -> None:
    """Green 5: on a Talk-to-camera edit with a speaker background, the pixels behind a caption
    are the background (the fill, or a timed window's photo or video) with the speaker take drawn
    in its frame, not the take filling the canvas. Recorded on the policy so the environment
    sampler judges text colour against that (`_background_frames`). A colour fill, no background,
    no policy or the switch off records nothing (byte-identical)."""
    from app.config import settings

    policy = _POLICY.get()
    if (
        not policy
        or background is None
        or getattr(background, "kind", None) == "color"
        or not settings.caption_background_contrast_enabled
    ):
        return
    frame = getattr(background, "speaker_frame", None)
    policy["_speaker_background"] = {
        "fill": {
            "path": background.gcs_path,
            "kind": background.kind,
            "source_start": float(background.source_start_s or 0.0),
        },
        "windows": [
            {
                "path": window.gcs_path,
                "kind": window.kind,
                "source_start": float(window.source_start_s or 0.0),
                "start_s": float(window.start_s),
                "end_s": float(window.end_s),
            }
            for window in background.windows
        ],
        "frame": None
        if frame is None
        else {"width": frame.width_fraction, "x": frame.x_fraction, "y": frame.y_fraction},
    }
    if policy.get("_environment_sampled"):
        # Text was already styled (an earlier compile pass, before the background was known):
        # the frames change for what comes next, and an automatic caption colour is chosen
        # again over them; its font, size and spot stay.
        from app.pipeline.canvas import PORTRAIT

        policy["_background_frames"] = _background_frames(
            policy["_speaker_background"],
            policy.get("_background_frames") or [],
            canvas or PORTRAIT,
        )
        if policy.get("caption_look") is not None and policy.get("_caption_auto_fill"):
            policy["caption_look"] = _auto_caption_colour(policy["caption_look"], policy)


def _segments(spec: dict, until: float) -> list[tuple[float, float, dict]]:
    """``(start, end, media)``: every timed window, and the fill wherever no window covers."""
    windows = sorted(spec["windows"], key=lambda w: w["start_s"])
    out: list[tuple[float, float, dict]] = []
    cursor = 0.0
    for window in windows:
        if window["start_s"] > cursor + 1e-6:
            out.append((cursor, window["start_s"], spec["fill"]))
        out.append((window["start_s"], window["end_s"], window))
        cursor = max(cursor, window["end_s"])
    if cursor < until - 1e-6:
        out.append((cursor, until, spec["fill"]))
    return out


def _framed(background: bytes, take: bytes | None, frame: dict | None) -> bytes:
    """``background`` with ``take`` drawn small in the speaker ``frame`` (canvas fractions, the
    centre at x/y, uniform scale by width). Unmatted: the speaker's whole frame counts, the
    conservative case for contrast."""
    from app.pipeline.phone_text_background import HEIGHT, WIDTH

    if take is None or frame is None:
        return background
    pixels = bytearray(background)
    scale = frame["width"]
    left, top = frame["x"] - scale / 2, frame["y"] - scale / 2
    for y in range(HEIGHT):
        fy = (y / HEIGHT - top) / scale
        if not 0 <= fy < 1:
            continue
        for x in range(WIDTH):
            fx = (x / WIDTH - left) / scale
            if 0 <= fx < 1:
                pixels[y * WIDTH + x] = take[int(fy * HEIGHT) * WIDTH + int(fx * WIDTH)]
    return bytes(pixels)


def _background_frames(spec: dict, take_frames: list[dict], canvas) -> list[dict]:
    """The frames behind text on a speaker-background edit, one per background segment: its photo
    or video sampled at its start, the take sampled at that time drawn in the speaker frame. With
    no frame (a full-size matted speaker) both the background and the take count. Any media that
    cannot be read keeps ``take_frames`` (fail-open: the colour is chosen as before)."""
    import tempfile
    from pathlib import Path

    from app.pipeline.phone_text_background import sample_background
    from app.services.pipeline_trace import record_pipeline_event
    from app.storage import download_to_file

    until = max([float(f["end_s"]) for f in take_frames] + [w["end_s"] for w in spec["windows"]])
    until = min(until, 1800.0) if until > 0 else 1800.0
    frames: list[dict] = []
    try:
        with tempfile.TemporaryDirectory(prefix="phone-text-background-") as directory:
            local: dict[str, str] = {}
            for index, (start, end, media) in enumerate(_segments(spec, until)):
                path = local.get(media["path"])
                if path is None:
                    # The media's own extension, so the decoder is chosen by it (a photo's jpg).
                    suffix = Path(str(media["path"])).suffix[:8]
                    path = str(Path(directory) / f"background-{len(local)}{suffix}")
                    download_to_file(media["path"], path, timeout_s=10)
                    local[media["path"]] = path
                at = media["source_start"] if media["kind"] == "video" else None
                pixels = sample_background(path, at, canvas=canvas)
                if pixels is None:
                    raise ValueError(f"segment {index} unreadable")
                take = next(
                    (f["pixels"] for f in take_frames if f["start_s"] < end and start < f["end_s"]),
                    take_frames[0]["pixels"] if take_frames else None,
                )
                frames.append(
                    {"start_s": start, "end_s": end, "pixels": _framed(pixels, take, spec["frame"])}
                )
                if spec["frame"] is None and take is not None:
                    frames.append({"start_s": start, "end_s": end, "pixels": take})
    except Exception as exc:  # noqa: BLE001 - never fail styling; no paths in the trace
        record_pipeline_event(
            "text", "styling_background_sample_failed", {"reason": type(exc).__name__}
        )
        return take_frames
    record_pipeline_event(
        "text",
        "styling_background_sample",
        {"segments": len(frames), "speaker_frame": spec["frame"] is not None},
    )
    return frames


def _ensure_generation_environment(canvas):
    """Reuse each sampled proxy for faces and contrast; bound each source budget."""
    import tempfile
    import time
    from pathlib import Path

    from app.pipeline.phone_guided_text_placement import face_box_to_canvas
    from app.pipeline.phone_text_background import sample_background
    from app.pipeline.render_geometry import sample_face_regions
    from app.services.phone_sources import PhoneSourceBinding
    from app.services.pipeline_trace import record_pipeline_event
    from app.storage import download_to_file

    policy = _POLICY.get()
    if not policy or policy.get("_environment_sampled"):
        return
    policy["_environment_sampled"] = True
    sources = policy.get("_source_bindings") or []
    boxes, frames = [], []
    face_errors = face_timeouts = decoded = source_errors = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="phone-text-environment-") as directory:
        for index, raw in enumerate(sources[:4]):
            if time.monotonic() - started > 18:
                break
            try:
                binding = PhoneSourceBinding.model_validate(raw)
                path = str(Path(directory) / f"proxy-{index}.mp4")
                download_to_file(
                    binding.proxy_path, path, timeout_s=max(0.1, 18 - (time.monotonic() - started))
                )
                windows = [
                    w
                    for w in policy.get("_sample_windows", [])
                    if w["media_id"] == binding.media_id
                ]
                if not windows:
                    windows = [
                        {
                            "source_start": 0,
                            "source_duration": float(binding.original.duration_s),
                            "start_s": 0,
                            "end_s": 1800,
                            "scale": 1,
                            "x": 0,
                            "y": 0,
                            "rotation": 0,
                        }
                    ]
                # Spread the bounded five samples over the source's actual
                # appearances instead of spending every sample on its first use.
                appearances = [windows[round(i * (len(windows) - 1) / 4)] for i in range(5)]
                observations = [
                    (w, w["source_start"] + w["source_duration"] * fraction)
                    for w, fraction in zip(appearances, (0.02, 0.25, 0.5, 0.75, 0.98), strict=True)
                ]
                points = [at for _, at in observations]
                regions, receipt = sample_face_regions(
                    path, points, max_samples=5, timeout_s=3, count_decoded=True
                )
                face_errors += int(bool(receipt.get("worker_error")))
                face_timeouts += int(bool(receipt.get("timed_out")))
                decoded += int(receipt.get("decoded") or 0)
                width, height = binding.original.width, binding.original.height
                if binding.original.orientation_degrees in (90, 270):
                    width, height = height, width
                for region in regions:
                    mapped = face_box_to_canvas(
                        region.box,
                        source_width=width,
                        source_height=height,
                        canvas_width=canvas.width,
                        canvas_height=canvas.height,
                        crop=None,
                    )
                    if mapped is not None:
                        for window in windows:
                            if (
                                region.start_s > window["source_start"] + window["source_duration"]
                                or region.end_s < window["source_start"]
                            ):
                                continue
                            scale, x, y = (
                                window["scale"],
                                window["x"] / canvas.width,
                                window["y"] / canvas.height,
                            )
                            if window["rotation"]:
                                box = (0.0, 0.0, 1.0, 1.0)  # conservative rotated-frame protection
                            else:
                                box = (
                                    (mapped.left - 0.5) * scale + 0.5 + x,
                                    (mapped.top - 0.5) * scale + 0.5 + y,
                                    (mapped.right - 0.5) * scale + 0.5 + x,
                                    (mapped.bottom - 0.5) * scale + 0.5 + y,
                                )
                            if box[2] > 0 and box[0] < 1 and box[3] > 0 and box[1] < 1:
                                boxes.append(
                                    {
                                        "start_s": window["start_s"],
                                        "end_s": window["end_s"],
                                        "box": box,
                                        "kind": "face",
                                    }
                                )
                for window, at in observations:
                    if time.monotonic() - started > 18:
                        break
                    pixels = (
                        sample_background(path, at, canvas=canvas)
                        if window["scale"] == 1
                        and not window["rotation"]
                        and not window["x"]
                        and not window["y"]
                        else None
                    )
                    if pixels:
                        frames.append(
                            {
                                "start_s": window["start_s"],
                                "end_s": window["end_s"],
                                "pixels": pixels,
                            }
                        )
            except Exception:  # noqa: BLE001 - no source paths/private text in receipts
                source_errors += 1
                continue
    if policy.get("_speaker_background"):
        frames = _background_frames(policy["_speaker_background"], frames, canvas)
    policy["_face_boxes"], policy["_background_frames"] = boxes, frames
    record_pipeline_event(
        "text",
        "styling_environment_sample",
        {
            "sources": min(len(sources), 4),
            "frames": len(frames),
            "face_regions": len(boxes),
            "coverage": "conservative_source_samples" if frames else "unavailable",
            "faces_decoded": decoded,
            "face_sampler_errors": face_errors,
            "face_sampler_timeouts": face_timeouts,
            "source_sampling_errors": source_errors,
        },
    )


def generation_style_fields() -> dict:
    policy = _POLICY.get()
    if not policy:
        return {}
    result = {
        PHONE_TEXT_STYLE_FIELD: {
            key: copy.deepcopy(value)
            for key, value in policy.items()
            if key != "caption_look" and not key.startswith("_")
        }
    }
    look = policy.get("caption_look")
    if policy.get("_caption_look_receipt") is not None:
        result["caption_look_receipt"] = copy.deepcopy(policy["_caption_look_receipt"])
    if look is not None:
        result.update(
            voiceover_caption_font=look.font_family,
            caption_stroke_width=look.outline_px,
            caption_shadow_enabled=look.shadow_enabled,
            caption_text_color=look.text_color,
            caption_highlight_color=look.highlight_color,
            caption_size_px=look.text_size_px,
            caption_margin_v=round(1920 * (1 - max(0.30, min(0.90, look.position_y_frac)))),
            caption_editor_style={
                "alignment": look.text_anchor,
                "position_x_frac": look.position_x_frac,
                "position_y_frac": look.position_y_frac,
                "max_width_frac": look.max_width_frac,
                # Only when the look set one (Green 5's outline over a background), so every
                # other variant keeps its exact fields.
                **({"stroke_color": look.stroke_color} if look.stroke_color else {}),
            },
        )
    return result


def style_guided_generation(plan, bindings, *, job_id: str) -> dict:
    """Share the proxy sampler; decide against faces and text in one pass."""
    import tempfile
    import time

    from app.pipeline.canvas import canvas_for_orientation
    from app.pipeline.phone_guided_text_placement import build_phone_face_sampler
    from app.services.pipeline_trace import record_pipeline_event
    from app.storage import download_to_file

    raw = plan.model_dump(mode="json", exclude_none=False)
    lanes = ("text_elements", "context_label_text_elements", "narration_label_text_elements")
    rows = [row for lane in lanes for row in raw.get(lane) or []]
    from app.pipeline.phone_text_style import text_role

    for lane in lanes[1:]:
        for row in raw.get(lane) or []:
            if text_role(row) == "title":
                row["source_params"] = {
                    **(row.get("source_params") or {}),
                    "phone_text_role": "label",
                }
    boxes = []
    frames = []
    policy = _POLICY.get()
    if policy is not None:
        policy["_environment_sampled"] = True
    if bindings and rows:
        all_anchors = sorted(
            {
                round(
                    float(row["start_s"])
                    + (float(row["end_s"]) - float(row["start_s"])) * fraction,
                    3,
                )
                for row in rows
                if text_role(row) in {"title", "label", "pinned", "caption", "lyric"}
                for fraction in (0.02, 0.25, 0.5, 0.75, 0.98)
            }
        )
        anchors = (
            all_anchors
            if len(all_anchors) <= 20
            else [all_anchors[round(i * (len(all_anchors) - 1) / 19)] for i in range(20)]
        )
        try:
            from app.pipeline.phone_text_background import sample_background

            canvas = canvas_for_orientation(plan.output_orientation)
            sample_started = time.monotonic()

            def observe(path, points):
                for moment, at in points:
                    if len(frames) >= 12 or time.monotonic() - sample_started > 18:
                        break
                    pixels = sample_background(path, at, crop=moment.source_crop, canvas=canvas)
                    if pixels:
                        frames.append(
                            {
                                "start_s": moment.output_start_s,
                                "end_s": moment.output_end_s,
                                "pixels": pixels,
                            }
                        )

            with tempfile.TemporaryDirectory(prefix="phone-text-faces-") as directory:

                def bounded_download(source, destination):
                    remaining = 18 - (time.monotonic() - sample_started)
                    if remaining <= 0:
                        raise TimeoutError("Phone text sampling budget exhausted")
                    download_to_file(source, destination, timeout_s=remaining)

                sampler = build_phone_face_sampler(
                    plan,
                    bindings,
                    workdir=directory,
                    download=bounded_download,
                    observe_frames=observe,
                )
                regions, receipt = sampler(anchors, 12.0)
                boxes = [
                    {
                        "start_s": r.start_s,
                        "end_s": r.end_s,
                        "box": (r.box.left, r.box.top, r.box.right, r.box.bottom),
                        "kind": "face",
                    }
                    for r in regions
                ]
                receipt["contrast_frames"] = len(frames)
                record_pipeline_event("text", "styling_face_sample", receipt)
        except Exception:  # noqa: BLE001 - sampling is advisory, never claim clearance
            record_pipeline_event("text", "styling_face_sample", {"decision": "unavailable"})
    if policy is not None:
        policy["_background_frames"] = frames
    styled = generation_style_rows(
        rows,
        canvas=canvas_for_orientation(plan.output_orientation),
        job_id=job_id,
        mood=plan.direction,
        excluded_boxes=boxes,
    )
    by_id = {row["id"]: row for row in styled}
    for lane in lanes:
        raw[lane] = [by_id[row["id"]] for row in raw.get(lane) or []]
    return raw
