"""Generate or verify the checked Kria runtime-v2 tool contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.kria.api_schemas import (
    ApprovalDecisionBody,
    ApprovalDecisionOut,
    ApprovalSnapshotOut,
    DraftSnapshotOut,
    DraftUndoBody,
    DraftWriteBody,
    KriaProblemOut,
    SubmitTurnBody,
    ThreadDeltaOut,
    TurnAccepted,
    TurnCancelBody,
    TurnCancelled,
)
from app.kria.contracts import KRIA_SCHEMA_VERSION
from app.kria.recipes import EditRecipeV1
from app.kria.registry import KRIA_TOOLS
from app.routes.auth import (
    MobileExchangeRequest,
    MobileLinkResponse,
    MobileRefreshRequest,
    MobileRevokeResponse,
    MobileSessionOut,
    MobileUserOut,
)
from app.routes.creation_threads import (
    ActionBody,
    AttachBody,
    CreateBody,
    CreationCapabilitiesOut,
    CreationThreadOut,
    UploadBody,
    UploadTarget,
)
from app.routes.generative_jobs import (
    EditorCommitRequest,
    EditorCommitResponse,
    GenerativeJobStatusResponse,
    GenerativeUploadUrlRequest,
    GenerativeUploadUrlResponse,
    TemporaryUploadCancellationResponse,
)
from app.routes.me import (
    LibraryPlaybackResponse,
    LibraryResponse,
    OpenInEditorBody,
    OpenInEditorResponse,
)
from app.routes.personas import PersonaResponse, QuestionnaireBody

DEFAULT_SNAPSHOT = Path(__file__).parents[2] / "tests" / "fixtures" / "kria_turns" / "tools.json"
DEFAULT_TYPES = Path(__file__).parents[3] / "web" / "src" / "lib" / "kria-runtime-v2.generated.ts"
DEFAULT_MOBILE_SNAPSHOT = Path(__file__).parents[2] / "tests" / "fixtures" / "kria_mobile.json"
DEFAULT_MOBILE_OPENAPI = Path(__file__).parents[3] / "ios" / "Kria" / "Generated" / "openapi.yaml"

API_MODELS = (
    SubmitTurnBody,
    TurnAccepted,
    TurnCancelBody,
    TurnCancelled,
    ApprovalDecisionBody,
    ApprovalDecisionOut,
    ApprovalSnapshotOut,
    DraftSnapshotOut,
    DraftWriteBody,
    DraftUndoBody,
    ThreadDeltaOut,
    KriaProblemOut,
)

MOBILE_API_MODELS = (
    MobileExchangeRequest,
    MobileRefreshRequest,
    MobileSessionOut,
    MobileUserOut,
    EditRecipeV1,
    MobileRevokeResponse,
    MobileLinkResponse,
    CreateBody,
    CreationCapabilitiesOut,
    CreationThreadOut,
    UploadBody,
    UploadTarget,
    AttachBody,
    ActionBody,
    PersonaResponse,
    QuestionnaireBody,
    LibraryResponse,
    LibraryPlaybackResponse,
    GenerativeUploadUrlRequest,
    GenerativeUploadUrlResponse,
    TemporaryUploadCancellationResponse,
    OpenInEditorBody,
    OpenInEditorResponse,
    GenerativeJobStatusResponse,
    EditorCommitRequest,
    EditorCommitResponse,
    *API_MODELS,
)


def contract_json() -> str:
    payload = {
        "schema_version": KRIA_SCHEMA_VERSION,
        "tools": KRIA_TOOLS.manifest(),
        "api": {model.__name__: model.model_json_schema() for model in API_MODELS},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def mobile_contract_json() -> str:
    """Generate the checked JSON contract consumed by the native client build."""
    return (
        json.dumps(
            {
                "schema_version": 1,
                "endpoints": {
                    "/auth/mobile/exchange": {
                        "method": "POST",
                        "request": "MobileExchangeRequest",
                        "response": "MobileSessionOut",
                    },
                    "/auth/mobile/refresh": {
                        "method": "POST",
                        "request": "MobileRefreshRequest",
                        "response": "MobileSessionOut",
                    },
                    "/auth/mobile/revoke": {
                        "method": "POST",
                        "request": "MobileRefreshRequest",
                        "response": "MobileRevokeResponse",
                    },
                    "/auth/mobile/me": {"method": "GET", "response": "MobileUserOut"},
                    "/auth/mobile/link": {
                        "method": "POST",
                        "request": "MobileExchangeRequest",
                        "response": "MobileLinkResponse",
                    },
                },
                "models": {
                    "MobileExchangeRequest": {
                        "provider": ["google", "apple"],
                        "id_token": "string",
                        "nonce": "string",
                    },
                    "MobileRefreshRequest": {"refresh_token": "string"},
                    "MobileRevokeResponse": {"revoked": "boolean"},
                    "MobileLinkResponse": {
                        "linked": "boolean",
                        "linked_providers": "Array<Provider>",
                    },
                    "MobileSessionOut": {
                        "access_token": "string",
                        "refresh_token": "string",
                        "token_type": "Bearer",
                        "expires_in": "integer",
                        "user": "MobileUserOut",
                    },
                    "MobileUserOut": {
                        "id": "string",
                        "email": "string",
                        "name": ["string", "null"],
                        "onboarding_status": "string",
                        "linked_providers": "Array<Provider>",
                    },
                    "EditRecipeV1": {
                        "schema_version": 1,
                        "renderer_version": "string",
                        "canvas": "Canvas",
                        "frame_rate": "number",
                        "assets": "Array<MediaAsset>",
                        "tracks": "Array<TimelineTrack>",
                        "audio": "AudioMixRecipe",
                        "required_capabilities": "Set<MediaCapability>",
                    },
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _schema_ref(model: type) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{model.__name__}"}


def _openapi30_schema(value: Any) -> Any:
    """Translate Pydantic's JSON Schema null unions to OpenAPI 3.0 nullable.

    Swift OpenAPI Generator intentionally warns and drops JSON Schema's
    standalone ``{"type": "null"}`` branches. The mobile document uses the
    OpenAPI 3.0 spelling so generated Swift optionals stay faithful to the
    server model.
    """
    if isinstance(value, list):
        return [_openapi30_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    any_of = value.get("anyOf")
    if isinstance(any_of, list):
        concrete = [item for item in any_of if item != {"type": "null"}]
        if len(concrete) == 1 and len(concrete) != len(any_of):
            translated = _openapi30_schema(concrete[0])
            if isinstance(translated, dict):
                siblings = {
                    key: _openapi30_schema(item) for key, item in value.items() if key != "anyOf"
                }
                return {**translated, **siblings, "nullable": True}
    translated = {key: _openapi30_schema(item) for key, item in value.items()}
    exclusive_minimum = translated.get("exclusiveMinimum")
    if isinstance(exclusive_minimum, (int, float)) and not isinstance(exclusive_minimum, bool):
        translated["minimum"] = exclusive_minimum
        translated["exclusiveMinimum"] = True
    exclusive_maximum = translated.get("exclusiveMaximum")
    if isinstance(exclusive_maximum, (int, float)) and not isinstance(exclusive_maximum, bool):
        translated["maximum"] = exclusive_maximum
        translated["exclusiveMaximum"] = True
    if "const" in translated:
        translated["enum"] = [translated.pop("const")]
    return translated


def _json_request(model: type, *, required: bool = True) -> dict[str, Any]:
    return {
        "required": required,
        "content": {"application/json": {"schema": _schema_ref(model)}},
    }


def _json_responses(model: type, *, status_code: str = "200") -> dict[str, Any]:
    return {
        status_code: {
            "description": "Success",
            "content": {"application/json": {"schema": _schema_ref(model)}},
        },
        "401": {"description": "Authentication required"},
        "409": {"description": "Revision or identity conflict"},
        "422": {"description": "Invalid request"},
    }


def mobile_openapi_json() -> str:
    """Render the native, server-owned OpenAPI 3.0 subset.

    The Swift build plugin consumes this checked file directly. Renderer
    internals and unrelated admin/browser routes are deliberately absent.
    """

    schemas: dict[str, Any] = {}
    for model in MOBILE_API_MODELS:
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(_openapi30_schema(schema.pop("$defs", {})))
        schemas[model.__name__] = _openapi30_schema(schema)

    bearer = [{"mobileBearer": []}]
    thread_id = {
        "name": "thread_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    approval_id = {
        "name": "approval_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    job_id = {
        "name": "job_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    item_id = {
        "name": "item_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }
    variant_id = {
        "name": "variant_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "maxLength": 160},
    }
    document: dict[str, Any] = {
        "openapi": "3.0.3",
        "info": {
            "title": "Kria Mobile API",
            "version": "1.0.0",
            "description": (
                "Checked native subset; server drafts and render jobs remain authoritative."
            ),
        },
        "servers": [{"url": "https://nova-video.fly.dev"}],
        "paths": {
            "/auth/mobile/exchange": {
                "post": {
                    "operationId": "exchangeMobileIdentity",
                    "requestBody": _json_request(MobileExchangeRequest),
                    "responses": _json_responses(MobileSessionOut),
                }
            },
            "/auth/mobile/refresh": {
                "post": {
                    "operationId": "refreshMobileSession",
                    "requestBody": _json_request(MobileRefreshRequest),
                    "responses": _json_responses(MobileSessionOut),
                }
            },
            "/auth/mobile/revoke": {
                "post": {
                    "operationId": "revokeMobileSession",
                    "requestBody": _json_request(MobileRefreshRequest),
                    "responses": _json_responses(MobileRevokeResponse),
                }
            },
            "/auth/mobile/me": {
                "get": {
                    "operationId": "getMobileAccount",
                    "security": bearer,
                    "responses": _json_responses(MobileUserOut),
                }
            },
            "/auth/mobile/link": {
                "post": {
                    "operationId": "linkMobileIdentity",
                    "security": bearer,
                    "requestBody": _json_request(MobileExchangeRequest),
                    "responses": _json_responses(MobileLinkResponse),
                }
            },
            "/creation-threads": {
                "get": {
                    "operationId": "listCreationThreads",
                    "security": bearer,
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": _schema_ref(CreationThreadOut),
                                    }
                                }
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "createCreationThread",
                    "security": bearer,
                    "requestBody": _json_request(CreateBody),
                    "responses": _json_responses(CreationThreadOut, status_code="201"),
                },
            },
            "/creation-threads/capabilities": {
                "get": {
                    "operationId": "getCreationCapabilities",
                    "security": bearer,
                    "responses": _json_responses(CreationCapabilitiesOut),
                }
            },
            "/creation-threads/{thread_id}/turns": {
                "parameters": [thread_id],
                "post": {
                    "operationId": "submitCreationTurn",
                    "security": bearer,
                    "requestBody": _json_request(SubmitTurnBody),
                    "responses": _json_responses(TurnAccepted, status_code="202"),
                },
            },
            "/creation-threads/{thread_id}/actions": {
                "parameters": [thread_id],
                "post": {
                    "operationId": "applyCreationAction",
                    "security": bearer,
                    "requestBody": _json_request(ActionBody),
                    "responses": _json_responses(CreationThreadOut),
                },
            },
            "/creation-threads/{thread_id}": {
                "parameters": [thread_id],
                "get": {
                    "operationId": "getCreationThread",
                    "security": bearer,
                    "parameters": [
                        {
                            "name": "projection",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string", "enum": ["full"]},
                        }
                    ],
                    "responses": _json_responses(CreationThreadOut),
                },
            },
            "/creation-threads/{thread_id}/upload-urls": {
                "parameters": [thread_id],
                "post": {
                    "operationId": "reserveCreationThreadUploads",
                    "security": bearer,
                    "requestBody": _json_request(UploadBody),
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": _schema_ref(UploadTarget),
                                    }
                                }
                            },
                        },
                        "401": {"description": "Authentication required"},
                        "409": {"description": "Revision or identity conflict"},
                        "422": {"description": "Invalid request"},
                    },
                },
            },
            "/creation-threads/{thread_id}/media": {
                "parameters": [thread_id],
                "post": {
                    "operationId": "attachCreationThreadMedia",
                    "security": bearer,
                    "requestBody": _json_request(AttachBody),
                    "responses": _json_responses(CreationThreadOut),
                },
            },
            "/creation-threads/{thread_id}/delta": {
                "parameters": [thread_id],
                "get": {
                    "operationId": "getCreationThreadDelta",
                    "security": bearer,
                    "parameters": [
                        {
                            "name": "after_sequence",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "default": -1},
                        }
                    ],
                    "responses": _json_responses(ThreadDeltaOut),
                },
            },
            "/creation-threads/{thread_id}/draft": {
                "parameters": [thread_id],
                "get": {
                    "operationId": "getCreationDraft",
                    "security": bearer,
                    "responses": _json_responses(DraftSnapshotOut),
                },
                "put": {
                    "operationId": "updateCreationDraft",
                    "security": bearer,
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": _json_request(DraftWriteBody),
                    "responses": _json_responses(DraftSnapshotOut),
                },
            },
            "/creation-threads/{thread_id}/draft/undo": {
                "parameters": [thread_id],
                "post": {
                    "operationId": "undoCreationDraft",
                    "security": bearer,
                    "requestBody": _json_request(DraftUndoBody),
                    "responses": _json_responses(DraftSnapshotOut),
                },
            },
            "/creation-threads/{thread_id}/approvals/{approval_id}/{decision}": {
                "parameters": [
                    thread_id,
                    approval_id,
                    {
                        "name": "decision",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "enum": ["approve", "deny"]},
                    },
                ],
                "post": {
                    "operationId": "decideCreationApproval",
                    "security": bearer,
                    "requestBody": _json_request(ApprovalDecisionBody),
                    "responses": _json_responses(ApprovalDecisionOut),
                },
            },
            "/creation-threads/{thread_id}/approvals/{approval_id}": {
                "parameters": [thread_id, approval_id],
                "get": {
                    "operationId": "getCreationApproval",
                    "security": bearer,
                    "responses": _json_responses(ApprovalSnapshotOut),
                },
            },
            "/personas": {
                "get": {
                    "operationId": "getPersona",
                    "security": bearer,
                    "responses": _json_responses(PersonaResponse),
                },
                "post": {
                    "operationId": "createPersona",
                    "security": bearer,
                    "requestBody": _json_request(QuestionnaireBody),
                    "responses": _json_responses(PersonaResponse, status_code="201"),
                },
            },
            "/me/jobs": {
                "get": {
                    "operationId": "listLibraryJobs",
                    "security": bearer,
                    "responses": _json_responses(LibraryResponse),
                }
            },
            "/me/jobs/{job_id}/playback-url": {
                "parameters": [job_id],
                "get": {
                    "operationId": "refreshPlaybackURL",
                    "security": bearer,
                    "responses": _json_responses(LibraryPlaybackResponse),
                },
            },
            "/me/jobs/{job_id}/edit-recipe": {
                "parameters": [job_id],
                "get": {
                    "operationId": "getEditRecipe",
                    "security": bearer,
                    "parameters": [
                        {
                            "name": "variant_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "maxLength": 160},
                        }
                    ],
                    "responses": _json_responses(EditRecipeV1),
                },
            },
            "/me/jobs/{job_id}/open-in-editor": {
                "parameters": [job_id],
                "post": {
                    "operationId": "openLibraryJobInEditor",
                    "security": bearer,
                    "requestBody": _json_request(OpenInEditorBody, required=False),
                    "responses": _json_responses(OpenInEditorResponse),
                },
            },
            "/generative-jobs/upload-url": {
                "post": {
                    "operationId": "reserveTemporaryUpload",
                    "security": bearer,
                    "requestBody": _json_request(GenerativeUploadUrlRequest),
                    "responses": _json_responses(GenerativeUploadUrlResponse),
                }
            },
            "/generative-jobs/uploads/{reservation_id}": {
                "parameters": [
                    {
                        "name": "reservation_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    }
                ],
                "delete": {
                    "operationId": "cancelTemporaryUpload",
                    "security": bearer,
                    "responses": _json_responses(TemporaryUploadCancellationResponse),
                },
            },
            "/generative-jobs/{job_id}/status": {
                "parameters": [job_id],
                "get": {
                    "operationId": "getGenerativeJobStatus",
                    "security": bearer,
                    "responses": _json_responses(GenerativeJobStatusResponse),
                },
            },
            "/plan-items/{item_id}/variants/{variant_id}/editor-commit": {
                "parameters": [item_id, variant_id],
                "post": {
                    "operationId": "commitPlanItemEditor",
                    "security": bearer,
                    "requestBody": _json_request(EditorCommitRequest),
                    "responses": _json_responses(EditorCommitResponse),
                },
            },
        },
        "components": {
            "securitySchemes": {
                "mobileBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
            },
            "schemas": schemas,
        },
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def _ts_type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).rsplit("/", 1)[-1]
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(value) for value in schema["enum"])
    if "anyOf" in schema:
        return " | ".join(_ts_type(value) for value in schema["anyOf"])
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(_ts_type({"type": value}) for value in kind)
    if kind == "array":
        item = _ts_type(schema.get("items") or {})
        return f"Array<{item}>"
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        if properties:
            required = set(schema.get("required") or [])
            rows = [
                f"{json.dumps(name)}{'' if name in required else '?'}: {_ts_type(value)};"
                for name, value in properties.items()
            ]
            return "{ " + " ".join(rows) + " }"
        additional = schema.get("additionalProperties", True)
        value_type = _ts_type(additional) if isinstance(additional, dict) else "unknown"
        return f"Record<string, {value_type}>"
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(str(kind), "unknown")


def typescript_contract() -> str:
    """Generate the runtime-v2 browser types from the checked server models."""

    definitions: dict[str, dict[str, Any]] = {}
    roots: list[tuple[str, dict[str, Any]]] = []
    for model in API_MODELS:
        schema = model.model_json_schema()
        definitions.update(schema.pop("$defs", {}))
        roots.append((model.__name__, schema))
    rows = [
        "// Generated by `python -m app.cli.kria_contracts --write`.",
        "// Do not edit by hand; `make verify-kria` checks this file.",
        "",
    ]
    root_names = {name for name, _schema in roots}
    for name in sorted(definitions):
        if name not in root_names:
            rows.append(f"export type {name} = {_ts_type(definitions[name])};")
    for name, schema in roots:
        rows.append(f"export type {name} = {_ts_type(schema)};")
    rows.extend(
        [
            "",
            "export type KriaApprovalSnapshot = ApprovalSnapshotOut;",
            "export type KriaApprovalDecision = ApprovalDecisionOut;",
            "export type KriaTurnAccepted = TurnAccepted;",
            "export type KriaDelta = ThreadDeltaOut;",
            "export type KriaDraftSnapshot = DraftSnapshotOut;",
            "",
        ]
    )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli.kria_contracts")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--types", type=Path, default=DEFAULT_TYPES)
    parser.add_argument("--mobile-snapshot", type=Path, default=DEFAULT_MOBILE_SNAPSHOT)
    parser.add_argument("--mobile-openapi", type=Path, default=DEFAULT_MOBILE_OPENAPI)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    rendered = contract_json()
    rendered_types = typescript_contract()
    rendered_mobile_openapi = mobile_openapi_json()
    if args.write:
        args.mobile_openapi.parent.mkdir(parents=True, exist_ok=True)
        args.snapshot.write_text(rendered, encoding="utf-8")
        args.types.write_text(rendered_types, encoding="utf-8")
        args.mobile_snapshot.write_text(mobile_contract_json(), encoding="utf-8")
        args.mobile_openapi.write_text(rendered_mobile_openapi, encoding="utf-8")
        print(f"Wrote Kria contract snapshot to {args.snapshot}")
        print(f"Wrote Kria TypeScript contract to {args.types}")
        print(f"Wrote mobile contract snapshot to {args.mobile_snapshot}")
        print(f"Wrote mobile OpenAPI subset to {args.mobile_openapi}")
        return 0
    if not args.check:
        print(rendered, end="")
        return 0
    if not args.snapshot.is_file():
        parser.error(f"contract snapshot not found: {args.snapshot}")
    if args.snapshot.read_text(encoding="utf-8") != rendered:
        parser.error(
            "Kria tool contract drifted; review the change and update "
            "tests/fixtures/kria_turns/tools.json"
        )
    if not args.types.is_file():
        parser.error(f"TypeScript contract not found: {args.types}")
    if args.types.read_text(encoding="utf-8") != rendered_types:
        parser.error(
            "Kria TypeScript contract drifted; run python -m app.cli.kria_contracts --write"
        )
    print(f"Kria contract matches {args.snapshot}")
    if not args.mobile_snapshot.is_file():
        parser.error(f"mobile contract snapshot not found: {args.mobile_snapshot}")
    if args.mobile_snapshot.read_text(encoding="utf-8") != mobile_contract_json():
        parser.error("mobile contract drifted; run python -m app.cli.kria_contracts --write")
    print(f"Mobile contract matches {args.mobile_snapshot}")
    if not args.mobile_openapi.is_file():
        parser.error(f"mobile OpenAPI subset not found: {args.mobile_openapi}")
    if args.mobile_openapi.read_text(encoding="utf-8") != rendered_mobile_openapi:
        parser.error("mobile OpenAPI drifted; run python -m app.cli.kria_contracts --write")
    print(f"Mobile OpenAPI matches {args.mobile_openapi}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
