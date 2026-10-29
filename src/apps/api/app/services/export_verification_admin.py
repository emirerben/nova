"""Admin projection of the stored phone-export verification evidence.

The `_device_render_v1` record is private to the creator surfaces and stripped
from `/admin/jobs/{id}/debug`. This module reads it for operators as an
allowlist: every response field is a number, an enum or id token, a storage
object path, or a timestamp. Keys outside the models below (upload URLs,
decline messages, recipes) never leave the record, and a stored value that
does not fit its field's type is dropped instead of failing the request.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    computed_field,
    field_validator,
)

from app.services.device_render import DEVICE_RENDER_FIELD

# No "/" in a token and no ":" in a path, so neither can carry a URL.
Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:\-]{1,160}$")]
StoragePath = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.\-][A-Za-z0-9_./\-]{0,511}$")]


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @field_validator("*", mode="wrap")
    @classmethod
    def _drop_unfit(
        cls, value: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
    ) -> Any:
        try:
            return handler(value)
        except ValidationError:
            return cls.model_fields[info.field_name].get_default(call_default_factory=True)


class ExportThresholds(BaseModel):
    """Pass rules the verifier code applies; reports do not store them.

    Picture sample passes when mean pixel error <= picture_exact_max_mean_pixel_error,
    or when error <= picture_max_mean_pixel_error and SSIM >= picture_min_local_ssim.
    Audio chunk passes when correlation >= audio_min_correlation and gain is within
    [audio_min_gain, audio_max_gain]; a reference quieter than
    audio_silent_reference_rms instead needs output rms < audio_silent_max_rms.
    Duration passes within max(duration_min_tolerance_s, duration_tolerance_frames
    / frame_rate) of the recipe plus brand tail.
    """

    picture_exact_max_mean_pixel_error: float
    picture_max_mean_pixel_error: float
    picture_min_local_ssim: float
    whole_window_neighbour_shift_rejects: bool
    audio_min_correlation: float
    audio_min_gain: float
    audio_max_gain: float
    audio_silent_reference_rms: float
    audio_silent_max_rms: float
    duration_min_tolerance_s: float
    duration_tolerance_frames: int


_BASE_THRESHOLDS = ExportThresholds(
    picture_exact_max_mean_pixel_error=round(2 / 255, 6),
    picture_max_mean_pixel_error=0.1,
    picture_min_local_ssim=0.95,
    whole_window_neighbour_shift_rejects=False,
    audio_min_correlation=0.97,
    audio_min_gain=0.85,
    audio_max_gain=1.15,
    audio_silent_reference_rms=0.0001,
    audio_silent_max_rms=0.002,
    duration_min_tolerance_s=0.1,
    duration_tolerance_frames=2,
)
# Numeric limits are unchanged since v1; v3 (KRI-810/KRI-831) added displayed-frame
# neighbours and rejects a window where every sample needed one.
THRESHOLDS_BY_VERIFIER: dict[str, ExportThresholds] = {
    "phone-simple-cut-v1": _BASE_THRESHOLDS,
    "phone-simple-cut-v2": _BASE_THRESHOLDS,
    "phone-simple-cut-v3": _BASE_THRESHOLDS.model_copy(
        update={"whole_window_neighbour_shift_rejects": True}
    ),
}


class ExportCheck(_Evidence):
    kind: Token | None = None
    status: Token | None = None
    reason: Token | None = None
    clip_id: Token | None = None
    layer_id: Token | None = None
    chunk: int | None = None
    samples: int | None = None
    picture_clock: Token | None = None
    neighbour_samples: int | None = None
    max_mean_pixel_error: float | None = None
    min_local_ssim: float | None = None
    correlation: float | None = None
    gain: float | None = None
    lag_samples: int | None = None
    rms: float | None = None
    actual_s: float | None = None
    expected_s: float | None = None
    at_s: float | None = None
    expected_visible: bool | None = None


class ExportIdentity(_Evidence):
    job_id: Token | None = None
    variant_id: Token | None = None
    recipe_revision: int | None = None
    recipe_digest: Token | None = None


class ExportReport(_Evidence):
    status: Token | None = None
    verifier_version: Token | None = None
    identity: ExportIdentity | None = None
    unsupported: list[Token] = []
    picture_sampling: Token | None = None
    audio_comparison: Token | None = None
    renderer_version: int | None = None
    recipe_schema_version: int | None = None
    required_capabilities: list[Token] = []
    elapsed_s: float | None = None
    sha256: Token | None = None
    file_size_bytes: int | None = None
    artifact_path: StoragePath | None = None
    storage_generation: Token | None = None
    source_receipts_digest: Token | None = None
    checks: list[ExportCheck] = []

    @computed_field
    @property
    def thresholds(self) -> ExportThresholds | None:
        return THRESHOLDS_BY_VERIFIER.get(self.verifier_version or "")


class VerificationTask(_Evidence):
    state: Token | None = None
    runs: int | None = None
    queued_at: datetime | None = None


class ExportAttempt(_Evidence):
    index: int
    attempt_id: str
    path: StoragePath | None = None
    size: int | None = None
    sha256: Token | None = None
    brand_tail: Token | None = None
    verification_task: VerificationTask | None = None
    actual_export_verification: ExportReport | None = None


class RepairCause(_Evidence):
    code: Token | None = None
    requirement_ids: list[Token] = []
    field_paths: list[Token] = []


class RepairChange(_Evidence):
    recipe_fields_changed: list[Token] = []
    source_assets_normalized: list[Token] = []


class ExportRepair(_Evidence):
    attempt: int | None = None
    action: Token | None = None
    failed_attempt_id: Token | None = None
    failed_identity: ExportIdentity | None = None
    asset_ids: list[Token] = []
    cause: RepairCause | None = None
    change: RepairChange | None = None
    outcome: Token | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_s: float | None = None
    model_calls: int | None = None
    report: ExportReport | None = None
    result: ExportReport | None = None


class ExportPolicy(_Evidence):
    version: int | None = None
    verifier_version: Token | None = None
    source_receipts_digest: Token | None = None


class ContractDecline(_Evidence):
    decline_reason: Token | None = None
    field_path: Token | None = None
    stage: Token | None = None
    recorded_at: datetime | None = None


class VariantExportEvidence(_Evidence):
    variant_id: str
    phase: Token | None = None
    reason_code: Token | None = None
    identity: ExportIdentity | None = None
    base_generation: Token | None = None
    published_attempt: Token | None = None
    verification_attempt: Token | None = None
    actual_export_policy: ExportPolicy | None = None
    contract_decline: ContractDecline | None = None
    attempts: list[ExportAttempt] = []
    repair_history: list[ExportRepair] = []


class JobExportVerification(BaseModel):
    job_id: str
    job_status: str
    variants: list[VariantExportEvidence]


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def project_export_verification(
    job_id: str, job_status: str, assembly_plan: dict | None
) -> JobExportVerification:
    variants = []
    for variant_id, record in _dict(_dict(assembly_plan).get(DEVICE_RENDER_FIELD)).items():
        if not isinstance(record, dict):
            continue
        status = _dict(record.get("status"))
        # JSONB does not keep key order, so order attempts by when verification queued.
        attempts = sorted(
            ((key, _dict(value)) for key, value in _dict(record.get("attempts")).items()),
            key=lambda item: (
                str(_dict(item[1].get("verification_task")).get("queued_at") or "~"),
                item[0],
            ),
        )
        variants.append(
            VariantExportEvidence.model_validate(
                {
                    **record,
                    "variant_id": variant_id,
                    "phase": status.get("phase"),
                    "reason_code": status.get("reason_code"),
                    "identity": _dict(status.get("request")).get("identity"),
                    "attempts": [
                        {**attempt, "index": index, "attempt_id": attempt_id}
                        for index, (attempt_id, attempt) in enumerate(attempts)
                    ],
                }
            )
        )
    return JobExportVerification(job_id=job_id, job_status=job_status, variants=variants)
