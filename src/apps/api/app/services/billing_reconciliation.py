"""Reconcile delayed Cloud Billing export with Nova's settled AI ledger.

The Cloud Billing side is read-only and uses the BigQuery REST API so the
worker does not need the large ``google-cloud-bigquery`` dependency.  Project
IDs are explicitly mapped to Nova environments; unrelated costs in the same
billing-account export are ignored instead of being guessed into a bucket.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import google.auth
import structlog
from google.auth.transport.requests import AuthorizedSession
from sqlalchemy import text

from app import storage
from app.config import settings
from app.database import sync_session

log = structlog.get_logger()

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TABLE_RE = re.compile(r"^gcp_billing_export_v1_[A-Fa-f0-9_-]+$")
_TOPIC_RE = re.compile(r"^projects/[A-Za-z0-9_-]+/topics/[A-Za-z0-9_-]+$")
_ENVIRONMENTS = frozenset({"production", "development", "lab"})
_EXCLUDED_PROJECT = "excluded"
_PROJECT_MAP_VALUES = _ENVIRONMENTS | {_EXCLUDED_PROJECT}
_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass(frozen=True, slots=True)
class BillingExportTarget:
    billing_account_id: str
    project_id: str
    dataset_id: str
    table_id: str
    location: str = "EU"

    @property
    def qualified_table(self) -> str:
        return f"{self.project_id}.{self.dataset_id}.{self.table_id}"


@dataclass(frozen=True, slots=True)
class BillingExportResult:
    project_costs_usd: dict[str, float]
    export_watermark: datetime | None


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    usage_date: date
    status: str
    cloud_costs_usd: dict[str, float]
    ledger_costs_usd: dict[str, float]
    differences: dict[str, dict[str, float | bool]]
    export_watermark: datetime | None
    error_detail: str | None = None


def _load_json(raw: str, *, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc


def parse_export_targets(raw: str) -> list[BillingExportTarget]:
    payload = _load_json(raw, label="BILLING_EXPORT_TARGETS_JSON")
    if not isinstance(payload, list) or not payload:
        raise ValueError("BILLING_EXPORT_TARGETS_JSON must be a non-empty JSON array")
    targets: list[BillingExportTarget] = []
    for value in payload:
        if not isinstance(value, dict):
            raise ValueError("each billing export target must be an object")
        target = BillingExportTarget(
            billing_account_id=str(value.get("billing_account_id") or "").strip(),
            project_id=str(value.get("project_id") or "").strip(),
            dataset_id=str(value.get("dataset_id") or "").strip(),
            table_id=str(value.get("table_id") or "").strip(),
            location=str(value.get("location") or "EU").strip().upper(),
        )
        if not target.billing_account_id:
            raise ValueError("billing_account_id is required for every export target")
        if not _IDENTIFIER_RE.fullmatch(target.project_id):
            raise ValueError("invalid billing export project_id")
        if not _IDENTIFIER_RE.fullmatch(target.dataset_id):
            raise ValueError("invalid billing export dataset_id")
        if not _TABLE_RE.fullmatch(target.table_id):
            raise ValueError("table_id must be a Standard Usage Cost export table")
        expected_table = "gcp_billing_export_v1_" + target.billing_account_id.replace("-", "_")
        if target.table_id.lower() != expected_table.lower():
            raise ValueError("table_id does not match its declared billing_account_id")
        if not _IDENTIFIER_RE.fullmatch(target.location):
            raise ValueError("invalid billing export location")
        targets.append(target)
    locations = {target.location for target in targets}
    if len(locations) != 1:
        raise ValueError("billing export targets must share one BigQuery location")
    if len({target.qualified_table for target in targets}) != len(targets):
        raise ValueError("billing export targets must be unique")
    return targets


def parse_project_environment_map(raw: str) -> dict[str, str]:
    payload = _load_json(raw, label="BILLING_PROJECT_ENVIRONMENT_MAP_JSON")
    if not isinstance(payload, dict) or not payload:
        raise ValueError("BILLING_PROJECT_ENVIRONMENT_MAP_JSON must be a non-empty JSON object")
    result: dict[str, str] = {}
    for project_id, environment in payload.items():
        project = str(project_id).strip()
        env = str(environment).strip().lower()
        if not _IDENTIFIER_RE.fullmatch(project):
            raise ValueError("invalid project ID in billing environment map")
        if env not in _PROJECT_MAP_VALUES:
            raise ValueError(f"invalid billing environment {env!r}")
        result[project] = env
    return result


def build_standard_export_query(targets: list[BillingExportTarget]) -> str:
    """Build a validated Standard Usage query normalized to USD.

    ``currency_conversion_rate`` converts USD to the billing-account currency,
    so dividing both cost and credits by it makes exports from GBP and USD
    accounts comparable with Nova's USD ledger.
    """

    selects = []
    for target in targets:
        selects.append(
            f"""
            SELECT
              project.id AS project_id,
              SAFE_DIVIDE(
                CAST(cost AS NUMERIC) + IFNULL((
                  SELECT SUM(CAST(credit.amount AS NUMERIC))
                  FROM UNNEST(credits) AS credit
                ), 0),
                NULLIF(CAST(currency_conversion_rate AS NUMERIC), 0)
              ) AS cost_usd,
              export_time
            FROM `{target.qualified_table}`
            WHERE DATE(usage_start_time) = @usage_date
            """.strip()
        )
    union = "\nUNION ALL\n".join(selects)
    return f"""
        WITH exported AS (
          {union}
        )
        SELECT
          COALESCE(project_id, '__unattributed__') AS project_id,
          CAST(ROUND(SUM(cost_usd), 6) AS STRING) AS cost_usd,
          CAST(MAX(export_time) AS STRING) AS export_watermark
        FROM exported
        GROUP BY project_id
        ORDER BY project_id
    """.strip()


def _credentials() -> Any:
    credentials = storage.get_gcp_credentials(scopes=[_CLOUD_SCOPE])
    if credentials is not None:
        return credentials
    credentials, _ = google.auth.default(scopes=[_CLOUD_SCOPE])
    return credentials


def _query_export(
    targets: list[BillingExportTarget],
    usage_date: date,
    *,
    session: AuthorizedSession | None = None,
) -> BillingExportResult:
    own_session = session is None
    session = session or AuthorizedSession(_credentials())
    project_id = targets[0].project_id
    endpoint = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries"
    response = session.post(
        endpoint,
        json={
            "query": build_standard_export_query(targets),
            "useLegacySql": False,
            "location": targets[0].location,
            "timeoutMs": 25_000,
            "parameterMode": "NAMED",
            "queryParameters": [
                {
                    "name": "usage_date",
                    "parameterType": {"type": "DATE"},
                    "parameterValue": {"value": usage_date.isoformat()},
                }
            ],
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("jobComplete"):
        job_reference = payload.get("jobReference") or {}
        job_id = str(job_reference.get("jobId") or "")
        if not job_id:
            raise RuntimeError("BigQuery response did not include a job ID")
        result_endpoint = (
            f"https://bigquery.googleapis.com/bigquery/v2/projects/{project_id}/queries/{job_id}"
        )
        response = session.get(
            result_endpoint,
            params={"location": targets[0].location, "timeoutMs": 25_000},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("jobComplete"):
            raise TimeoutError("BigQuery billing reconciliation did not finish")
    if payload.get("errors"):
        raise RuntimeError("BigQuery billing reconciliation query failed")

    project_costs: dict[str, float] = {}
    watermark: datetime | None = None
    for row in payload.get("rows") or []:
        fields = row.get("f") or []
        if len(fields) < 3:
            continue
        project = str(fields[0].get("v") or "")
        project_costs[project] = project_costs.get(project, 0.0) + float(fields[1].get("v") or 0)
        value = fields[2].get("v")
        if value:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            watermark = max(watermark, parsed) if watermark else parsed
    if own_session:
        session.close()
    return BillingExportResult(project_costs_usd=project_costs, export_watermark=watermark)


def _ledger_costs(usage_date: date) -> dict[str, float]:
    start = datetime.combine(usage_date, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    with sync_session() as db:
        rows = db.execute(
            text(
                """
                SELECT
                    r.environment,
                    r.usage_purpose,
                    r.principal_type AS principal,
                    COALESCE(SUM(r.settled_cost_usd), 0) AS cost_usd
                FROM ai_cost_reservations r
                WHERE r.status = 'settled'
                  -- Attribute the ledger charge to the provider-start day,
                  -- not the later response/settlement day. A request that
                  -- crosses midnight belongs beside Cloud Billing's
                  -- usage_start_time date.
                  AND r.provider_started_at >= :start_at
                  AND r.provider_started_at < :end_at
                GROUP BY r.environment, r.usage_purpose, principal
                """
            ),
            {"start_at": start, "end_at": end},
        )
        costs: dict[str, float] = {}
        for row in rows:
            amount = float(row.cost_usd or 0)
            environment = str(row.environment)
            purpose = str(row.usage_purpose or "unclassified")
            principal = str(row.principal)
            for key in (
                environment,
                f"{environment}:purpose:{purpose}",
                f"{environment}:principal:{principal}",
            ):
                costs[key] = costs.get(key, 0.0) + amount
        return {key: round(value, 6) for key, value in costs.items()}


def compare_costs(
    *,
    usage_date: date,
    project_costs_usd: dict[str, float],
    project_environment_map: dict[str, str],
    ledger_costs_usd: dict[str, float],
    export_watermark: datetime | None,
    threshold_pct: float,
) -> ReconciliationResult:
    cloud: dict[str, float] = {environment: 0.0 for environment in _ENVIRONMENTS}
    for project_id, environment in project_environment_map.items():
        if environment != _EXCLUDED_PROJECT:
            cloud[environment] += float(project_costs_usd.get(project_id, 0.0))
    cloud = {key: round(value, 6) for key, value in cloud.items()}
    unmapped = round(
        sum(
            float(cost)
            for project_id, cost in project_costs_usd.items()
            if project_id not in project_environment_map and float(cost) > 0
        ),
        6,
    )
    if unmapped:
        cloud["unmapped"] = unmapped
    # Preserve attribution dimensions alongside each environment total. Cloud
    # Billing can reconcile only the project/environment aggregate; the ledger
    # dimensions make customer, internal, live-eval, canary, and Omni traffic
    # independently auditable within that matched total.
    ledger = {key: round(float(value), 6) for key, value in ledger_costs_usd.items()}
    for environment in _ENVIRONMENTS:
        ledger.setdefault(environment, 0.0)
    differences: dict[str, dict[str, float | bool]] = {}
    mismatch = False
    for environment in sorted(_ENVIRONMENTS):
        cloud_value = cloud[environment]
        ledger_value = ledger[environment]
        absolute = round(cloud_value - ledger_value, 6)
        denominator = max(abs(cloud_value), abs(ledger_value), 0.01)
        relative = round(abs(absolute) / denominator, 6)
        exceeded = relative > threshold_pct and abs(absolute) >= 0.01
        mismatch = mismatch or exceeded
        differences[environment] = {
            "cloud_cost_usd": cloud_value,
            "ledger_cost_usd": ledger_value,
            "absolute_usd": absolute,
            "relative": relative,
            "threshold_exceeded": exceeded,
        }
    if unmapped:
        differences["unmapped"] = {
            "cloud_cost_usd": unmapped,
            "ledger_cost_usd": 0.0,
            "absolute_usd": unmapped,
            "relative": 1.0,
            "threshold_exceeded": True,
        }
    status = (
        "incomplete"
        if export_watermark is None or unmapped
        else "mismatch"
        if mismatch
        else "matched"
    )
    return ReconciliationResult(
        usage_date=usage_date,
        status=status,
        cloud_costs_usd=cloud,
        ledger_costs_usd=ledger,
        differences=differences,
        export_watermark=export_watermark,
    )


def reconcile_usage_date(usage_date: date) -> ReconciliationResult:
    targets = parse_export_targets(settings.billing_export_targets_json)
    project_map = parse_project_environment_map(settings.billing_project_environment_map_json)
    exported = _query_export(targets, usage_date)
    return compare_costs(
        usage_date=usage_date,
        project_costs_usd=exported.project_costs_usd,
        project_environment_map=project_map,
        ledger_costs_usd=_ledger_costs(usage_date),
        export_watermark=exported.export_watermark,
        threshold_pct=settings.billing_reconciliation_threshold_pct,
    )


def publish_reconciliation_alert(result: ReconciliationResult) -> None:
    topic = settings.billing_reconciliation_pubsub_topic.strip()
    if not _TOPIC_RE.fullmatch(topic):
        raise ValueError("BILLING_RECONCILIATION_PUBSUB_TOPIC is not configured")
    payload = json.dumps(
        {
            "schema_version": "1.0",
            "kind": "nova_ai_billing_reconciliation",
            # Pub/Sub is at-least-once. Consumers use this stable key to make
            # notification delivery idempotent across an abandoned claim.
            "event_id": f"nova-ai-billing-reconciliation:{result.usage_date.isoformat()}",
            "usage_date": result.usage_date.isoformat(),
            "status": result.status,
            "threshold_pct": settings.billing_reconciliation_threshold_pct,
            "cloud_costs_usd": result.cloud_costs_usd,
            "ledger_costs_usd": result.ledger_costs_usd,
            "differences": result.differences,
            "export_watermark": (
                result.export_watermark.isoformat() if result.export_watermark else None
            ),
            "error_detail": result.error_detail,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    session = AuthorizedSession(_credentials())
    try:
        response = session.post(
            f"https://pubsub.googleapis.com/v1/{topic}:publish",
            json={
                "messages": [
                    {
                        "data": base64.b64encode(payload).decode(),
                        "attributes": {
                            "event_id": (
                                f"nova-ai-billing-reconciliation:{result.usage_date.isoformat()}"
                            )
                        },
                    }
                ]
            },
            timeout=15,
        )
        response.raise_for_status()
    finally:
        session.close()
