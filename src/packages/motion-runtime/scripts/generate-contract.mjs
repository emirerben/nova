import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const catalogPath = resolve(root, "creator-blocks.catalog.json");
const schemaPath = resolve(root, "motion-scene.schema.json");
const aiPath = resolve(root, "creator-blocks.ai.json");
const limitsPath = resolve(root, "motion-limits.json");
const checkOnly = process.argv.includes("--check");
const catalog = JSON.parse(readFileSync(catalogPath, "utf8"));
const limits = JSON.parse(readFileSync(limitsPath, "utf8"));

function fail(message) {
  throw new Error(`Creator Block catalog: ${message}`);
}

function assertCatalog() {
  if (catalog.catalog_version !== 2 || catalog.schema_version !== 3) {
    fail("catalog_version=2 and schema_version=3 are required");
  }
  if (!Array.isArray(catalog.control_definitions) || !Array.isArray(catalog.presets)) {
    fail("control_definitions and presets must be arrays");
  }
  const controlKeys = new Set();
  for (const control of catalog.control_definitions) {
    if (controlKeys.has(control.key)) fail(`duplicate control ${control.key}`);
    controlKeys.add(control.key);
    if (!["number", "enum", "boolean"].includes(control.type)) {
      fail(`unsupported control type ${control.type}`);
    }
    if (!["instance", "motion"].includes(control.storage)) {
      fail(`unsupported storage for ${control.key}`);
    }
  }
  const presetIds = new Set();
  for (const entry of catalog.presets) {
    if (presetIds.has(entry.preset_id)) fail(`duplicate current preset ${entry.preset_id}`);
    presetIds.add(entry.preset_id);
    if (entry.preset_version !== 2) fail(`${entry.preset_id} must expose preset v2`);
    if (!Number.isInteger(entry.default_duration_frames) || entry.default_duration_frames <= 0) {
      fail(`${entry.preset_id} has invalid default duration`);
    }
    if (
      !Number.isInteger(entry.base_choreography_frames) ||
      !Number.isInteger(entry.fixed_exit_frames) ||
      !Number.isInteger(entry.motion_defaults?.hold_frames)
    ) {
      fail(`${entry.preset_id} timing fields must be integer frames`);
    }
    const calculatedDefault = Math.round(
      entry.base_choreography_frames / entry.motion_defaults.speed,
    ) + entry.motion_defaults.hold_frames + entry.fixed_exit_frames;
    if (calculatedDefault !== entry.default_duration_frames) {
      fail(`${entry.preset_id} timing phases total ${calculatedDefault}, expected ${entry.default_duration_frames}`);
    }
    if (!Number.isInteger(entry.complexity_weight) || entry.complexity_weight < 1 || entry.complexity_weight > 4) {
      fail(`${entry.preset_id} complexity_weight must be 1-4`);
    }
    if (
      !/^#[0-9A-Fa-f]{6}$/.test(entry.palette_defaults?.primary ?? "") ||
      !/^#[0-9A-Fa-f]{6}$/.test(entry.palette_defaults?.accent ?? "")
    ) {
      fail(`${entry.preset_id} palette defaults must be #RRGGBB colors`);
    }
    for (const key of entry.supported_controls) {
      if (!controlKeys.has(key)) fail(`${entry.preset_id} references unknown control ${key}`);
    }
    const parameterKeys = new Set();
    for (const parameter of entry.parameters) {
      if (parameterKeys.has(parameter.key)) fail(`${entry.preset_id} duplicates ${parameter.key}`);
      parameterKeys.add(parameter.key);
      if (!["string", "string_list", "asset_list", "number", "enum", "boolean"].includes(parameter.type)) {
        fail(`${entry.preset_id}.${parameter.key} has unsupported type ${parameter.type}`);
      }
      if (parameter.required && !(parameter.key in entry.defaults)) {
        fail(`${entry.preset_id}.${parameter.key} is required but has no default`);
      }
    }
    for (const key of Object.keys(entry.defaults)) {
      if (!parameterKeys.has(key)) fail(`${entry.preset_id} has unknown default ${key}`);
    }
    const speed = effectiveControl(entry, "speed");
    const hold = effectiveControl(entry, "hold_frames");
    const slowDuration = Math.round(entry.base_choreography_frames / speed.minimum) +
      hold.maximum + entry.fixed_exit_frames;
    if (slowDuration > 240) {
      fail(`${entry.preset_id} minimum speed produces ${slowDuration} frames (>240)`);
    }
  }
  if (catalog.presets.length !== 9 || !presetIds.has("evolving_type")) {
    fail("exactly nine current presets including evolving_type are required");
  }
}

function effectiveControl(entry, key) {
  const definition = catalog.control_definitions.find((candidate) => candidate.key === key);
  if (!definition || !entry.supported_controls.includes(key)) {
    fail(`${entry.preset_id} does not support ${key}`);
  }
  return { ...definition, ...(entry.control_overrides?.[key] ?? {}) };
}

function definitionSchema(definition) {
  if (definition.type === "string") {
    return {
      type: "string",
      ...(definition.min_length === undefined ? {} : { minLength: definition.min_length }),
      ...(definition.max_length === undefined ? {} : { maxLength: definition.max_length }),
    };
  }
  if (definition.type === "string_list") {
    return {
      type: "array",
      ...(definition.min_items === undefined ? {} : { minItems: definition.min_items }),
      ...(definition.max_items === undefined ? {} : { maxItems: definition.max_items }),
      items: {
        type: "string",
        minLength: 1,
        ...(definition.max_length === undefined ? {} : { maxLength: definition.max_length }),
      },
    };
  }
  if (definition.type === "asset_list") {
    return {
      type: "array",
      ...(definition.min_items === undefined ? {} : { minItems: definition.min_items }),
      ...(definition.max_items === undefined ? {} : { maxItems: definition.max_items }),
      items: { $ref: "#/$defs/asset" },
    };
  }
  if (definition.type === "number") {
    return {
      type: definition.integer ? "integer" : "number",
      ...(definition.minimum === undefined ? {} : { minimum: definition.minimum }),
      ...(definition.maximum === undefined ? {} : { maximum: definition.maximum }),
      // Decimal UI steps such as 0.05 are metadata, not JSON-Schema multiples:
      // binary floats like 0.72 otherwise fail a mathematically-valid contract.
      ...(!definition.integer || definition.step === undefined
        ? {}
        : { multipleOf: definition.step }),
    };
  }
  if (definition.type === "enum") return { type: "string", enum: definition.values };
  if (definition.type === "boolean") return { type: "boolean" };
  fail(`cannot generate schema for ${definition.type}`);
}

function paramsSchema(entry, version) {
  const parameters = entry.parameters.filter((parameter) => (parameter.since_version ?? 1) <= version);
  return {
    type: "object",
    additionalProperties: false,
    required: parameters.filter((parameter) => parameter.required).map((parameter) => parameter.key),
    properties: Object.fromEntries(
      parameters.map((parameter) => [parameter.key, definitionSchema(parameter)]),
    ),
  };
}

const baseProperties = {
  id: { $ref: "#/$defs/id" },
  preset_id: { type: "string" },
  preset_version: { type: "integer" },
  start_frame: { $ref: "#/$defs/frame_start" },
  end_frame_exclusive: { $ref: "#/$defs/frame_end" },
  palette: { $ref: "#/$defs/palette" },
  intensity: { $ref: "#/$defs/intensity" },
};
const commonRequired = [
  "id", "preset_id", "preset_version", "start_frame", "end_frame_exclusive", "palette", "intensity",
];

function motionSchema(entry) {
  const controls = entry.supported_controls
    .map((key) => effectiveControl(entry, key))
    .filter((control) => control.storage === "motion");
  return {
    type: "object",
    additionalProperties: false,
    required: ["version", ...controls.filter((control) => control.required).map((control) => control.key)],
    properties: {
      version: { const: 2 },
      ...Object.fromEntries(controls.map((control) => [control.key, definitionSchema(control)])),
    },
  };
}

function creatorSchema(entry, version) {
  return {
    type: "object",
    additionalProperties: false,
    required: [...commonRequired, "params", ...(version === 2 ? ["motion"] : [])],
    properties: {
      ...baseProperties,
      preset_id: { const: entry.preset_id },
      preset_version: { const: version },
      params: paramsSchema(entry, version),
      ...(version === 2 ? { motion: motionSchema(entry) } : {}),
    },
  };
}

function generateSchema() {
  const maxTimelineFrames = 60 * limits.motion_fps;
  const refs = [{ $ref: "#/$defs/route_trace_v1" }];
  const definitions = {
    id: { type: "string", minLength: 1, maxLength: 80, pattern: "^[A-Za-z0-9_-]+$" },
    frame_start: { type: "integer", minimum: 0, maximum: maxTimelineFrames - 1 },
    frame_end: { type: "integer", minimum: 1, maximum: maxTimelineFrames },
    palette: {
      type: "object",
      additionalProperties: false,
      required: ["primary", "accent"],
      properties: {
        primary: { type: "string", pattern: "^#[0-9A-Fa-f]{6}$" },
        accent: { type: "string", pattern: "^#[0-9A-Fa-f]{6}$" },
      },
    },
    intensity: { type: "number", minimum: 0, maximum: 1 },
    asset: {
      type: "object",
      additionalProperties: false,
      required: ["asset_id", "gcs_path"],
      properties: {
        asset_id: { $ref: "#/$defs/id" },
        gcs_path: {
          type: "string",
          minLength: 1,
          maxLength: 900,
          pattern: "^(users|dev-user|generative-jobs|slot-uploads|music-uploads)/[A-Za-z0-9_./-]+$",
        },
      },
    },
    route_trace_v1: {
      type: "object",
      additionalProperties: false,
      required: commonRequired,
      properties: {
        ...baseProperties,
        preset_id: { const: "route_trace" },
        preset_version: { const: 1 },
      },
    },
  };
  for (const entry of catalog.presets) {
    for (const version of entry.legacy_versions) {
      const key = `${entry.preset_id}_v${version}`;
      definitions[key] = creatorSchema(entry, version);
      refs.push({ $ref: `#/$defs/${key}` });
    }
    definitions[entry.preset_id] = creatorSchema(entry, 2);
    refs.push({ $ref: `#/$defs/${entry.preset_id}` });
  }
  return {
    $schema: "https://json-schema.org/draft/2020-12/schema",
    $id: "https://nova.video/schemas/motion-scene-v3.json",
    title: "Nova motion preset instances v3",
    type: "array",
    maxItems: limits.motion_max_instances,
    items: { oneOf: refs },
    $defs: definitions,
  };
}

function generateAiSnapshot() {
  return {
    catalog_version: catalog.catalog_version,
    presets: catalog.presets
      .filter((entry) => entry.ai_exposed)
      .map((entry) => ({
        preset_id: entry.preset_id,
        preset_version: entry.preset_version,
        label: entry.label,
        kind: entry.kind,
        default_duration_frames: entry.default_duration_frames,
        min_assets: entry.min_assets,
        palette_defaults: entry.palette_defaults,
        parameters: entry.parameters.map((parameter) =>
          parameter.type === "asset_list"
            ? { ...parameter, key: "asset_ids" }
            : parameter,
        ),
        controls: entry.supported_controls.map((key) => effectiveControl(entry, key)),
        defaults: entry.parameters.some((parameter) => parameter.type === "asset_list")
          ? Object.fromEntries(
              Object.entries(entry.defaults).map(([key, value]) => [
                key === "assets" ? "asset_ids" : key,
                value,
              ]),
            )
          : entry.defaults,
        motion_defaults: entry.motion_defaults,
      })),
  };
}

function formatted(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function emit(path, value) {
  const next = formatted(value);
  if (checkOnly) {
    const current = readFileSync(path, "utf8");
    if (current !== next) {
      throw new Error(`${path} drifted; run node scripts/generate-contract.mjs`);
    }
  } else {
    writeFileSync(path, next);
  }
}

assertCatalog();
emit(schemaPath, generateSchema());
emit(aiPath, generateAiSnapshot());
