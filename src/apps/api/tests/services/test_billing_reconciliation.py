from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from app.services.billing_reconciliation import (
    BillingExportTarget,
    _query_export,
    build_standard_export_query,
    compare_costs,
    parse_export_targets,
    parse_project_environment_map,
)


def _target(account: str, project: str) -> dict[str, str]:
    return {
        "billing_account_id": account,
        "project_id": project,
        "dataset_id": "all_billing_data",
        "table_id": f"gcp_billing_export_v1_{account.replace('-', '_')}",
        "location": "EU",
    }


def test_two_standard_export_targets_are_validated() -> None:
    targets = parse_export_targets(
        json.dumps(
            [
                _target("AAAAAA-BBBBBB-CCCCCC", "nova-finops-a"),
                _target("111111-222222-333333", "nova-finops-b"),
            ]
        )
    )

    assert [target.project_id for target in targets] == [
        "nova-finops-a",
        "nova-finops-b",
    ]


@pytest.mark.parametrize(
    "mutation,error",
    [
        ({"table_id": "detailed_export"}, "Standard Usage Cost"),
        ({"dataset_id": "billing`; DROP TABLE x; --"}, "dataset_id"),
        ({"table_id": "gcp_billing_export_v1_FFFFFF_EEEEEE_DDDDDD"}, "billing_account_id"),
        ({"location": "US"}, "share one BigQuery location"),
    ],
)
def test_export_target_rejects_unsafe_or_mixed_configuration(
    mutation: dict[str, str], error: str
) -> None:
    first = _target("AAAAAA-BBBBBB-CCCCCC", "nova-finops-a")
    second = _target("111111-222222-333333", "nova-finops-b")
    second.update(mutation)
    with pytest.raises(ValueError, match=error):
        parse_export_targets(json.dumps([first, second]))


def test_project_environment_map_is_explicit() -> None:
    assert parse_project_environment_map(
        '{"nova-prod":"production","nova-dev":"development","nova-omni":"lab",'
        '"nova-storage":"excluded"}'
    ) == {
        "nova-prod": "production",
        "nova-dev": "development",
        "nova-omni": "lab",
        "nova-storage": "excluded",
    }
    with pytest.raises(ValueError, match="invalid billing environment"):
        parse_project_environment_map('{"nova-prod":"customer"}')


def test_query_uses_both_tables_parameterized_date_and_currency_normalization() -> None:
    targets = [
        BillingExportTarget(
            billing_account_id="a",
            project_id="finops-a",
            dataset_id="billing",
            table_id="gcp_billing_export_v1_A",
        ),
        BillingExportTarget(
            billing_account_id="b",
            project_id="finops-b",
            dataset_id="billing",
            table_id="gcp_billing_export_v1_B",
        ),
    ]

    query = build_standard_export_query(targets)

    assert "`finops-a.billing.gcp_billing_export_v1_A`" in query
    assert "`finops-b.billing.gcp_billing_export_v1_B`" in query
    assert "DATE(usage_start_time) = @usage_date" in query
    assert "currency_conversion_rate" in query
    assert "UNNEST(credits)" in query


class _Response:
    def __init__(self, body: dict) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


class _Session:
    def __init__(self, body: dict) -> None:
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, **kwargs):  # noqa: ANN003
        self.calls.append((url, kwargs))
        return _Response(self.body)


def test_query_export_parses_project_costs_and_watermark() -> None:
    target = BillingExportTarget(
        billing_account_id="a",
        project_id="finops-a",
        dataset_id="billing",
        table_id="gcp_billing_export_v1_A",
    )
    session = _Session(
        {
            "jobComplete": True,
            "rows": [
                {
                    "f": [
                        {"v": "nova-prod"},
                        {"v": "1.250001"},
                        {"v": "2026-09-08T06:00:00Z"},
                    ]
                },
                {
                    "f": [
                        {"v": "nova-dev"},
                        {"v": "0.2"},
                        {"v": "2026-09-08T06:01:00+00:00"},
                    ]
                },
            ],
        }
    )

    result = _query_export([target], date(2026, 9, 5), session=session)  # type: ignore[arg-type]

    assert result.project_costs_usd == {"nova-prod": 1.250001, "nova-dev": 0.2}
    assert result.export_watermark == datetime(2026, 9, 8, 6, 1, tzinfo=UTC)
    request = session.calls[0][1]["json"]
    assert request["queryParameters"][0]["parameterValue"]["value"] == "2026-09-05"


def test_compare_costs_reconciles_environments_independently() -> None:
    result = compare_costs(
        usage_date=date(2026, 9, 5),
        project_costs_usd={"nova-prod": 10.0, "nova-dev": 1.0, "unmapped": 99.0},
        project_environment_map={
            "nova-prod": "production",
            "nova-dev": "development",
            "nova-omni": "lab",
        },
        ledger_costs_usd={
            "production": 9.5,
            "production:principal:customer": 8.5,
            "production:purpose:release_canary": 1.0,
            "development": 0.7,
            "development:purpose:live_eval": 0.7,
            "lab": 0.0,
            "lab:purpose:omni_lab": 0.0,
        },
        export_watermark=datetime(2026, 9, 8, tzinfo=UTC),
        threshold_pct=0.10,
    )

    assert result.status == "incomplete"
    assert result.differences["production"]["threshold_exceeded"] is False
    assert result.differences["development"]["threshold_exceeded"] is True
    assert result.cloud_costs_usd["lab"] == 0
    assert result.cloud_costs_usd["unmapped"] == 99.0
    assert result.differences["unmapped"]["threshold_exceeded"] is True
    assert result.ledger_costs_usd["production:principal:customer"] == 8.5
    assert result.ledger_costs_usd["production:purpose:release_canary"] == 1.0
    assert result.ledger_costs_usd["development:purpose:live_eval"] == 0.7
    assert result.ledger_costs_usd["lab:purpose:omni_lab"] == 0.0


def test_compare_costs_ignores_only_explicitly_excluded_storage_project() -> None:
    result = compare_costs(
        usage_date=date(2026, 9, 5),
        project_costs_usd={"nova-prod": 1.0, "nova-storage": 0.25},
        project_environment_map={
            "nova-prod": "production",
            "nova-storage": "excluded",
        },
        ledger_costs_usd={"production": 1.0},
        export_watermark=datetime(2026, 9, 8, tzinfo=UTC),
        threshold_pct=0.10,
    )

    assert result.status == "matched"
    assert "unmapped" not in result.cloud_costs_usd
    assert "excluded" not in result.cloud_costs_usd


def test_missing_export_watermark_is_incomplete_not_matched() -> None:
    result = compare_costs(
        usage_date=date(2026, 9, 5),
        project_costs_usd={},
        project_environment_map={"nova-prod": "production"},
        ledger_costs_usd={},
        export_watermark=None,
        threshold_pct=0.10,
    )
    assert result.status == "incomplete"


def test_ledger_reconciliation_uses_exact_provider_start_day() -> None:
    from inspect import getsource

    from app.services.billing_reconciliation import _ledger_costs

    source = getsource(_ledger_costs)
    assert "r.provider_started_at >= :start_at" in source
    assert "r.created_at >= :start_at" not in source
    assert "r.provider_started_at < :end_at" in source
    assert "r.settled_at >= :start_at" not in source
