"""Report every way a fresh account sees less than the founder's account.

Reads the effective process settings only (run it inside a prod machine to
observe prod).  Exit 1 when any parity gap exists, so it can gate onboarding a
new tester.  Prints no secret values; account ids are the only data shown.
"""

from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

FOUNDER_IOS_USER_ID = "25a596e6-0ada-4572-b669-f4f9d5c5aced"

# Allowlists that are not product cohorts: external audits and internal capture.
EXEMPT_ALLOWLISTS = frozenset(
    {
        "tiktok_publishing_beta_user_ids",  # TikTok's own content-posting audit
        "creator_comparison_capture_user_ids",
        "request_following_user_ids",
        "creator_edit_learning_user_ids",
    }
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--founder", default=FOUNDER_IOS_USER_ID, help="founder account id")
    parser.add_argument("--user", help="also compare this account (e.g. the new tester)")
    parser.add_argument(
        "--expect-preset",
        default="cigdem/v2",
        help="caption preset the founder's account is pinned to (id/version)",
    )
    return parser


def _allowlist_gaps(settings: Any, model_fields: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    for name in sorted(model_fields):
        if not name.endswith("_user_ids") or name in EXEMPT_ALLOWLISTS:
            continue
        value = [str(item) for item in getattr(settings, name)]
        if value and value != ["*"]:
            gaps.append({"setting": name, "members": value})
    return gaps


def _rollout_gaps(settings: Any, model_fields: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"setting": name, "percent": getattr(settings, name)}
        for name in sorted(model_fields)
        if name.endswith("_rollout_percent") and getattr(settings, name) < 100
    ]


def _predicate_gaps(settings: Any, founder: str, others: list[str]) -> list[dict[str, Any]]:
    gaps = []
    for name in sorted(dir(type(settings))):
        if not name.endswith("_for") or not callable(getattr(settings, name)):
            continue
        method = getattr(settings, name)
        expected = method(founder)
        for other in others:
            actual = method(other)
            if actual != expected:
                gaps.append(
                    {"predicate": name, "founder": expected, "other": actual, "user": other}
                )
    return gaps


def _caption_gap(settings: Any, expected: str) -> dict[str, str] | None:
    fleet = (
        f"{settings.smart_captions_default_preset_id}"
        f"/{settings.smart_captions_default_preset_version}"
    )
    return None if fleet == expected else {"fleet_default": fleet, "founder_pinned": expected}


def _dark_flags(settings: Any, model_fields: dict[str, Any]) -> list[str]:
    return sorted(
        name
        for name, field in model_fields.items()
        if field.annotation is bool and getattr(settings, name) is False
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    from app.config import Settings, settings  # noqa: PLC0415

    model_fields = Settings.model_fields
    others = [str(uuid.uuid4())] + ([args.user] if args.user else [])
    report = {
        "allowlists_excluding_fresh_accounts": _allowlist_gaps(settings, model_fields),
        "rollout_percent_below_100": _rollout_gaps(settings, model_fields),
        "predicates_differing_for_founder": _predicate_gaps(settings, args.founder, others),
        "caption_preset": _caption_gap(settings, args.expect_preset),
        "flags_off_for_everyone": _dark_flags(settings, model_fields),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    gap_keys = (
        "allowlists_excluding_fresh_accounts",
        "rollout_percent_below_100",
        "predicates_differing_for_founder",
        "caption_preset",
    )
    return 1 if any(report[key] for key in gap_keys) else 0


if __name__ == "__main__":
    raise SystemExit(main())
