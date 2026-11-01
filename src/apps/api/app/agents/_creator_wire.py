"""Versioned typed wire transport; product parsers remain the semantic authority.

Optional/nullable values use 0-or-1 arrays, so absence needs no nullable unions.
Maps use typed key/value entries, never opaque JSON strings. Objects are closed;
Copilot operation names/fields are taken from the authoritative parser registry.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from functools import cached_property

from jsonschema import Draft202012Validator

VERSION = "typed-presence-v2"
ANTHROPIC_JSON_DEPTH = 8
ANTHROPIC_PROJECTION_VERSION = "claude-acyclic-json-8-v1"
INSTRUCTION = """\n\nOUTPUT TRANSPORT typed-presence-v2:
Return the typed JSON object in the supplied schema.
This changes serialization only; all creative and safety instructions above still apply.
Every property is required on the wire. A field described as presence contains [] when
omitted, or [value] when supplied; nested values follow the same rule. A dictionary
is a list of {key,value} entries; keys must be unique. A union is an object with
a schema-selected branch tag and its typed value. Do not put JSON inside strings.
For explicit null, put the property name in its object's _nulls array and use [].
Null and omission are different; never list a supplied value in _nulls.
The server decodes this transport before applying its existing validators. Never fill an
absent field with invented facts. Do not include server-owned fields or execution results.
"""
SERVER_FIELDS = {
    "resolved_clip_intents",
    "resolved_song_takes",
    "ordering_choice",
    "choice_answers",
    "omitted_copy_targets",
    "song_captions",
    "creator_duration_s",
    "target_duration_requested",
    "pacing_requested",
    "selected_music_section",
}
MODEL_HIDDEN = {
    "clip_intents",
    "reaction_beats",
    "closing_media",
    "ending_clip",
    "background_media",
    "timed_cards",
    "song_sync",
    "voice_mode",
    "pinned_texts",
}


def obj(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def arr(item, **bounds):
    return {"type": "array", "items": item, **bounds}


def _json_defs():
    result = {
        "JSONValue": {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
                arr({"$ref": "#/$defs/JSONValue"}),
                {"type": "object", "additionalProperties": {"$ref": "#/$defs/JSONValue"}},
            ]
        }
    }
    result["NonNullJSONValue"] = {
        "anyOf": [b for b in result["JSONValue"]["anyOf"] if b.get("type") != "null"]
    }
    return result


def main_creator_schema():
    from pydantic import TypeAdapter

    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.agents.main_creator import MainCreatorOutput

    schema = MainCreatorOutput.model_json_schema()
    strategy = schema["$defs"]["CreativeStrategy"]
    for name in sorted(MODEL_HIDDEN):
        field = CreativeStrategy.model_fields[name]
        fragment = TypeAdapter(field.annotation).json_schema()
        schema["$defs"].update(fragment.pop("$defs", {}))
        fragment["description"] = field.description or name
        strategy["properties"][name] = fragment
    # Main Creator already requires these in its authoritative parse method.
    strategy["required"] = sorted(
        set(strategy.get("required", [])) | {"target_duration_s", "rationale"}
    )
    schema["$defs"].update(_json_defs())
    schema["$defs"]["BriefUpdate"]["properties"]["facts"] = {
        "type": "object",
        "additionalProperties": {"$ref": "#/$defs/JSONValue"},
    }
    return schema


def copilot_schema():
    from app.agents.edit_copilot import _OP_FIELDS, _OP_REQUIRED, _STYLE_PATCH_FIELDS
    from app.services.kria_editor_ops_text import EXTRA_PATCH_FIELDS

    string = {"type": "string"}
    number = {"type": "number"}
    boolean = {"type": "boolean"}
    integer = {"type": "integer", "minimum": 0}

    def enum(*values):
        return {"type": "string", "enum": list(values)}

    selector = obj(
        {
            **{
                k: string
                for k in (
                    "scope",
                    "media_kind",
                    "quantifier",
                    "kind",
                    "role",
                    "text",
                    "id",
                    "clip_id",
                    "source",
                    "group",
                    "contains",
                )
            },
            **{k: integer for k in ("bar_index", "slot_index", "index")},
            "ids": arr(string),
            "target_ids": arr(string),
            "clip_ids": arr(string),
            "clip_indexes": arr(integer),
            "slot_indexes": arr(integer),
            "expected_count": integer,
            "indexes": arr(integer),
            "all": boolean,
        }
    )
    bools = {
        "all",
        "reset",
        "hard_cuts",
        "minimal_text",
        "emphasis",
        "shadow_enabled",
        "behind_subject",
        "is_highlight",
        "enabled",
        "fit_to_video",
        "preserve_text",
    }
    numbers = {
        "at_s",
        "start_s",
        "end_s",
        "duration_s",
        "source_start_s",
        "in_s",
        "target_s",
        "shift_s",
        "gain",
        "music_level",
        "original_level",
        "music_gain_db",
        "intensity",
        "speed",
        "scale",
        "x_frac",
        "y_frac",
        "zoom",
        "size_px",
        "size_scale",
        "rotation_deg",
        "rotation_delta_deg",
        "stroke_width",
        "letter_spacing",
        "line_spacing",
        "playback_rate",
        "transition_duration_s",
        "max_width_frac",
        "opacity",
        "background_opacity",
        "duration",
        "delay",
        "amount",
        "cadence_s",
    }
    list_strings = {"target_ids", "segments", "slide_ids", "asset_ids"}
    map_fields = {
        "effect",
        "config",
        "params",
        "palette",
        "animation_phases",
        "target_identities",
        "focus",
    }

    def field(name):
        if name == "gap_policy":
            return enum("scale", "close", "fixed")
        if name == "backdrop":
            return enum("blank", "color", "blur")
        if name == "preset":
            # KRI-1162: the speaker's corner, a name and never numbers (S37).
            return enum(
                "small_bottom_right", "small_bottom_left", "small_top_right", "small_top_left"
            )
        if name in bools:
            return boolean
        if name == "base_generation":
            return {"anyOf": [string, integer]}
        if name.endswith("_index") or name in {
            "revision_number",
            "hold_frames",
            "text_appearance_version",
        }:
            return integer
        if name in numbers:
            return number
        if name in list_strings:
            return arr(string)
        if name in {"slide_indexes", "order"}:
            return arr(integer)
        if name == "selector":
            return selector
        if name == "replace":
            return obj({"find": string, "with": string}, ("find", "with"))
        if name == "patch":
            return patch
        if name == "tilt":
            return obj(
                {
                    "rotation_deg": number,
                    "rotation_delta_deg": number,
                    "backdrop": obj({"kind": enum("blank", "color", "blur"), "color": string}),
                }
            )
        if name in map_fields:
            # Dynamic preset/animation metadata remains locally validated; it is
            # structurally typed JSON, not an untyped substitute for an op.
            return {"$ref": "#/$defs/JSONValue"}
        strings = {
            "after_animation_of",
            "aspect",
            "asset_id",
            "background",
            "below",
            "candidate_id",
            "caption",
            "clip_id",
            "criterion",
            "direction",
            "display_mode",
            "easing",
            "effect_bundle_id",
            "effect_id",
            "find",
            "slide_id",
            "fit",
            "kind",
            "label_from",
            "layout",
            "look_preset",
            "media_kind",
            "mode",
            "motion_id",
            "orientation",
            "position",
            "preset_id",
            "quantifier",
            "scope",
            "source",
            "strategy",
            "style_from",
            "suggestion_id",
            "text",
            "time_format",
            "timezone",
            "title",
            "tool",
            "track_id",
            "transition",
            "transition_in",
            "transition_out",
            "font_family",
            "color",
            "highlight_color",
            "alignment",
            "text_case",
            "effect",
            "background_color",
            "style",
            "transition_after",
            "weight",
        }
        if name not in strings:
            raise ValueError("unregistered wire field type: " + name)
        return string

    patch_names = (
        set(_STYLE_PATCH_FIELDS)
        | set(EXTRA_PATCH_FIELDS)
        | {
            "in_s",
            "start_s",
            "end_s",
            "duration_s",
            "size_scale",
            "background_color",
            "background_opacity",
            "behind_subject",
            "gain",
            "display_mode",
            "scale",
            "intensity",
            "easing",
            "at_s",
            "music_level",
            "look_preset",
            "transition_in",
            "transition_out",
            "text",
            "animation_phases",
            "position",
            "style",
            "speed",
            "params",
            "enabled",
            "transition_after",
            "transition_duration_s",
            "playback_rate",
            "tilt",
            "rotation_delta_deg",
        }
    )
    patch = obj(
        {
            k: (
                {"anyOf": [field(k), {"type": "null"}]}
                if k in {"background_color", "tilt"}
                else field(k)
            )
            for k in sorted(patch_names)
        }
    )
    ops = []
    for name, fields in sorted(_OP_FIELDS.items()):
        ops.append(
            obj(
                {
                    "op": {"type": "string", "const": name},
                    **{
                        k: (
                            {"type": "string"}
                            if name == "replace_caption_text" and k == "replace"
                            else field(k)
                        )
                        for k in sorted(set(fields) - {"integrity"})
                    },
                },
                ["op", *sorted(_OP_REQUIRED.get(name, ()))],
            )
        )
    pending = copy.deepcopy(ops)
    for operation in pending:
        operation["required"] = ["op"]
    result = obj(
        {
            "intent": enum("edit", "clarify", "describe", "reject", "unknown"),
            "ops": arr({"anyOf": ops}),
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reply": string,
            "suggestions": arr(string),
            "needs_clarification": boolean,
            "unmet_requests": arr(
                obj({"request": string, "reason": string}, ("request", "reason"))
            ),
            "claimed_ops": arr(string),
            "length_options": arr(obj({"label": string, "seconds": number}, ("label", "seconds"))),
            "clarification_context": obj(
                {**selector["properties"], "selector": copy.deepcopy(selector), "referent": string}
            ),
            "pending_actions": arr({"anyOf": pending}),
        },
        ("intent", "reply"),
    )
    # ResearchRequest is a typed product contract, independent of output-only fields.
    from app.services.researched_text import ResearchRequest

    research = ResearchRequest.model_json_schema()
    result["$defs"] = {**_json_defs(), **research.pop("$defs", {})}
    result["properties"]["research_request"] = research
    for name in ("research_request", "claimed_ops", "clarification_context"):
        result["properties"][name] = {"anyOf": [result["properties"][name], {"type": "null"}]}
    return result


class WireContract:
    def __init__(self, source):
        self.source = copy.deepcopy(source)
        self._defs = self.source.get("$defs", {})

    def _resolve(self, node):
        if "$ref" in node:
            return self._defs[node["$ref"].split("/")[-1]]
        return node

    def _variants(self, node):
        return node.get("anyOf", node.get("oneOf"))

    def _nullable(self, node):
        if node.get("$ref") == "#/$defs/JSONValue":
            return True, {"$ref": "#/$defs/NonNullJSONValue"}
        node = self._resolve(node)
        branches = self._variants(node)
        if branches and any(b.get("type") == "null" for b in branches):
            rest = [b for b in branches if b.get("type") != "null"]
            return True, rest[0] if len(rest) == 1 else {"anyOf": rest}
        return False, node

    def _branch(self, node, value):
        if isinstance(value, dict):
            for branch in self._variants(node) or []:
                properties = self._resolve(branch).get("properties", {})
                for tag in ("kind", "op"):
                    choice = properties.get(tag, {})
                    choices = choice.get("enum", [choice.get("const")])
                    if tag in value and value[tag] in choices:
                        return branch
        for branch in self._variants(node) or []:
            validator = Draft202012Validator({**branch, "$defs": self._defs})
            if validator.is_valid(value):
                return branch
        raise ValueError("value does not match a typed branch")

    def project(self, value):
        def visit(node, v):
            node = self._resolve(node)
            if self._variants(node):
                return visit(self._branch(node, v), v)
            if isinstance(v, dict):
                if "properties" in node:
                    unknown = (
                        set(v)
                        - set(node["properties"])
                        - SERVER_FIELDS
                        - {"outcome", "rejection_reasons", "reply_notes"}
                    )
                    if unknown:
                        raise ValueError("unregistered wire fields: " + ",".join(sorted(unknown)))
                    if "target_duration_s" in node["properties"] and "target_duration_s" not in v:
                        v = {
                            **v,
                            "target_duration_s": node["properties"]["target_duration_s"].get(
                                "default", 24
                            ),
                        }
                    return {
                        k: visit(node["properties"][k], x)
                        for k, x in v.items()
                        if k in node["properties"]
                    }
                additional = node.get("additionalProperties")
                return {
                    k: visit(
                        additional
                        if isinstance(additional, dict)
                        else {"$ref": "#/$defs/JSONValue"},
                        x,
                    )
                    for k, x in v.items()
                }
            if isinstance(v, list):
                return [visit(node.get("items", {}), x) for x in v]
            return v

        return visit(self.source, value)

    def _transform(self, node):
        if "$ref" in node:
            return {"$ref": node["$ref"]}
        branches = self._variants(node)
        if branches:
            return {
                "anyOf": [
                    obj(
                        {
                            "branch": {"type": "string", "enum": [str(i)]},
                            "value": self._transform(b),
                        },
                        ("branch", "value"),
                    )
                    for i, b in enumerate(branches)
                ]
            }
        if node.get("type") == "object" or "properties" in node:
            if "properties" not in node:
                item = node.get("additionalProperties", {"$ref": "#/$defs/JSONValue"})
                if not isinstance(item, dict):
                    item = {"$ref": "#/$defs/JSONValue"}
                return arr(
                    obj(
                        {"key": {"type": "string"}, "value": self._transform(item)},
                        ("key", "value"),
                    )
                )
            props = {}
            required = set(node.get("required", []))
            for key, child in node["properties"].items():
                nullable, plain = self._nullable(child)
                result = self._transform(plain)
                if key not in required or nullable:
                    result = arr(
                        result,
                        maxItems=1,
                        description="presence: [] omit; [value] supply; _nulls clear",
                    )
                props[key] = result
            nullable_names = [
                key for key, child in node["properties"].items() if self._nullable(child)[0]
            ]
            if nullable_names:
                props["_nulls"] = arr({"type": "string", "enum": nullable_names})
            return obj(props, props)
        result = {
            k: copy.deepcopy(v)
            for k, v in node.items()
            if k not in ("title", "default", "discriminator", "$defs")
        }
        if "items" in result:
            result["items"] = self._transform(node["items"])
        if "const" in result:
            result["enum"] = [result.pop("const")]
        return result

    @cached_property
    def local_schema(self):
        schema = self._transform(self.source)
        schema["$defs"] = {k: self._transform(v) for k, v in self._defs.items()}
        # Hoist repeated selector/patch shapes. This changes no accepted value;
        # it keeps provider grammar complexity bounded instead of copying a wide
        # patch object into every operation and pending-action alternative.
        counts = Counter()

        def inventory(n):
            if isinstance(n, dict):
                if n.get("type") == "object" and "properties" in n:
                    counts[json.dumps(n, sort_keys=True)] += 1
                for child in n.values():
                    inventory(child)
            elif isinstance(n, list):
                for child in n:
                    inventory(child)

        inventory(schema)
        shared = {}
        bodies = {}

        def compact(n, root=False):
            if isinstance(n, list):
                return [compact(x) for x in n]
            if not isinstance(n, dict):
                return n
            key = json.dumps(n, sort_keys=True)
            if not root and counts[key] > 1 and len(key) > 120:
                name = "WireShared_" + hashlib.sha256(key.encode()).hexdigest()[:12]
                if name not in shared:
                    shared[name] = True
                    bodies[name] = compact(n, root=True)
                return {"$ref": "#/$defs/" + name}
            return {k: compact(v) for k, v in n.items()}

        result = compact(schema, root=True)
        result["$defs"].update(bodies)
        return result

    def provider_schema(self, model):
        schema = copy.deepcopy(self.local_schema)
        if model.startswith("claude") and "JSONValue" in self._defs:
            # Anthropic rejects recursive grammars. Preserve typed branch tags
            # and map entries, but explicitly bound only dynamic JSON containers.
            # Local/Sol/Gemini contracts retain their recursive value support.
            definitions = schema["$defs"]

            def rebind(node, target):
                if isinstance(node, dict):
                    return {
                        k: target if k == "$ref" and v == "#/$defs/JSONValue" else rebind(v, target)
                        for k, v in node.items()
                    }
                if isinstance(node, list):
                    return [rebind(v, target) for v in node]
                return node

            for depth in range(ANTHROPIC_JSON_DEPTH):
                source = copy.deepcopy(self._defs["JSONValue"])
                if depth == 0:
                    source["anyOf"] = source["anyOf"][:4]  # original scalar branch indexes
                else:
                    source = rebind(source, f"#/$defs/JSONValueLevel{depth - 1}")
                definitions[f"JSONValueLevel{depth}"] = self._transform(source)
            definitions["JSONValue"] = {"$ref": f"#/$defs/JSONValueLevel{ANTHROPIC_JSON_DEPTH - 1}"}
            definitions["NonNullJSONValue"] = self._transform(self._defs["NonNullJSONValue"])

        # Keep bounds enforced locally. Anthropic does not support all numeric /
        # string limits. Every projection is explicit and versioned, never hidden.
        def walk(node):
            if isinstance(node, dict):
                if model.startswith("claude"):
                    for key in (
                        "minimum",
                        "maximum",
                        "exclusiveMinimum",
                        "exclusiveMaximum",
                        "minLength",
                        "maxLength",
                        "minItems",
                        "maxItems",
                        "pattern",
                        "format",
                    ):
                        node.pop(key, None)
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        walk(schema)
        return json.loads(json.dumps(schema, sort_keys=True))

    def encode(self, value):
        value = self.project(value)
        if not Draft202012Validator(self.source).is_valid(value):
            raise ValueError("invalid model-owned source value")

        def visit(node, v):
            node = self._resolve(node)
            if self._variants(node):
                branch = self._branch(node, v)
                return {
                    "branch": str(self._variants(node).index(branch)),
                    "value": visit(branch, v),
                }
            if "properties" in node:
                required = set(node.get("required", []))
                out = {}
                for key, child in node["properties"].items():
                    nullable, plain = self._nullable(child)
                    if key not in required or nullable:
                        out[key] = [] if key not in v or v[key] is None else [visit(plain, v[key])]
                    else:
                        if key not in v:
                            raise ValueError("missing required wire field: " + key)
                        out[key] = visit(child, v[key])
                if any(self._nullable(child)[0] for child in node["properties"].values()):
                    out["_nulls"] = [
                        key
                        for key, child in node["properties"].items()
                        if self._nullable(child)[0] and key in v and v[key] is None
                    ]
                return out
            if node.get("type") == "object":
                item = node.get("additionalProperties")
                item = item if isinstance(item, dict) else {"$ref": "#/$defs/JSONValue"}
                return [{"key": k, "value": visit(item, x)} for k, x in sorted(v.items())]
            if node.get("type") == "array":
                return [visit(node["items"], x) for x in v]
            return v

        result = visit(self.source, value)
        if not Draft202012Validator(self.local_schema).is_valid(result):
            raise ValueError("invalid typed wire value")
        return result

    def decode(self, value):
        if not Draft202012Validator(self.local_schema).is_valid(value):
            raise ValueError("invalid typed wire response")

        def visit(node, v):
            node = self._resolve(node)
            if self._variants(node):
                return visit(self._variants(node)[int(v["branch"])], v["value"])
            if "properties" in node:
                required = set(node.get("required", []))
                out = {}
                nulls = v.get("_nulls", [])
                if len(nulls) != len(set(nulls)):
                    raise ValueError("duplicate explicit null")
                for key, child in node["properties"].items():
                    nullable, plain = self._nullable(child)
                    if key not in required or nullable:
                        if key in nulls:
                            if not nullable or v[key]:
                                raise ValueError("conflicting explicit null")
                            out[key] = None
                            continue
                        if not v[key]:
                            if key in required:
                                raise ValueError("missing required presence")
                            continue
                        out[key] = visit(plain, v[key][0])
                    else:
                        out[key] = visit(child, v[key])
                return out
            if node.get("type") == "object":
                item = node.get("additionalProperties")
                item = item if isinstance(item, dict) else {"$ref": "#/$defs/JSONValue"}
                if len({p["key"] for p in v}) != len(v):
                    raise ValueError("duplicate dictionary key")
                return {p["key"]: visit(item, p["value"]) for p in v}
            if node.get("type") == "array":
                return [visit(node["items"], x) for x in v]
            return v

        return visit(self.source, value)
