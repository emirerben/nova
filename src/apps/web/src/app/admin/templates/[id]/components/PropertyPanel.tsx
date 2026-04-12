import type { Dispatch } from "react";
import type {
  EditorAction,
  EditorSelection,
  Recipe,
  RecipeInterstitial,
  RecipeSlot,
  RecipeTextOverlay,
} from "./recipe-types";
import {
  COLOR_HINT_OPTIONS,
  INTERSTITIAL_TYPE_OPTIONS,
  OVERLAY_EFFECT_OPTIONS,
  OVERLAY_POSITION_OPTIONS,
  OVERLAY_ROLE_OPTIONS,
  SLOT_TYPE_OPTIONS,
  SYNC_STYLE_OPTIONS,
  TEXT_SIZE_OPTIONS,
  TRANSITION_IN_OPTIONS,
} from "./recipe-types";
import {
  FONT_NAMES,
  FONT_REGISTRY,
  MAX_OVERLAY_TEXT_LEN,
  getInferredFontName,
  resolveOverlayPreview,
} from "./overlay-constants";

// ── Shared field components ─────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs text-zinc-500 mb-1">{label}</label>
      {children}
    </div>
  );
}

const inputClass =
  "w-full bg-zinc-900 border border-zinc-700 rounded px-2.5 py-1.5 text-sm text-white focus:outline-none focus:border-zinc-500";

const selectClass =
  "w-full bg-zinc-900 border border-zinc-700 rounded px-2.5 py-1.5 text-sm text-white focus:outline-none focus:border-zinc-500";

function NumberInput({
  label,
  value,
  onChange,
  step = 0.1,
  min,
  max,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  step?: number;
  min?: number;
  max?: number;
}) {
  return (
    <Field label={label}>
      <input
        type="number"
        value={value}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (!isNaN(n)) onChange(n);
        }}
        step={step}
        min={min}
        max={max}
        className={inputClass}
      />
    </Field>
  );
}

function TextInput({
  label,
  value,
  onChange,
  maxLength,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  maxLength?: number;
}) {
  return (
    <Field label={label}>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        maxLength={maxLength}
        className={inputClass}
      />
    </Field>
  );
}

function SelectInput<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: readonly T[];
  onChange: (v: T) => void;
}) {
  return (
    <Field label={label}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as T)}
        className={selectClass}
      >
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {opt}
          </option>
        ))}
      </select>
    </Field>
  );
}

function CheckboxInput({
  label,
  checked,
  onChange,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label className="flex items-center gap-2 text-sm text-zinc-300 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="rounded border-zinc-600 bg-zinc-900 text-blue-500 focus:ring-0"
      />
      {label}
    </label>
  );
}

// ── Font Picker ─────────────────────────────────────────────────────────────

function FontPicker({
  overlay,
  onChange,
}: {
  overlay: RecipeTextOverlay;
  onChange: (fontName: string | null) => void;
}) {
  const inferred = getInferredFontName(overlay);
  const current = overlay.font_family ?? "";

  return (
    <Field label="Font">
      <select
        value={current}
        onChange={(e) => onChange(e.target.value || null)}
        className={selectClass}
      >
        {/* Show inferred font as placeholder when font_family is not set */}
        <option value="">
          {inferred} (from style)
        </option>
        {FONT_NAMES.map((name) => {
          const entry = FONT_REGISTRY[name];
          return (
            <option
              key={name}
              value={name}
              style={{ fontFamily: entry.css_family, fontWeight: entry.weight }}
            >
              {name}
            </option>
          );
        })}
      </select>
    </Field>
  );
}

// ── Slot Properties ─────────────────────────────────────────────────────────

function SlotProperties({
  slot,
  slotIndex,
  dispatch,
  previewSubject,
}: {
  slot: RecipeSlot;
  slotIndex: number;
  dispatch: Dispatch<EditorAction>;
  previewSubject?: string;
}) {
  const set = (field: keyof RecipeSlot, value: unknown) =>
    dispatch({ type: "UPDATE_SLOT_FIELD", slotIndex, field, value });

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-medium text-white">
        Slot {slot.position} Properties
      </h3>

      <div className="grid grid-cols-3 gap-3">
        <NumberInput
          label="Duration (s)"
          value={slot.target_duration_s}
          onChange={(v) => set("target_duration_s", v)}
          step={0.1}
          min={0.1}
        />
        <NumberInput
          label="Speed"
          value={slot.speed_factor}
          onChange={(v) => set("speed_factor", v)}
          step={0.05}
          min={0.25}
          max={4}
        />
        <SelectInput
          label="Type"
          value={slot.slot_type}
          options={SLOT_TYPE_OPTIONS}
          onChange={(v) => set("slot_type", v)}
        />
      </div>

      <div className="grid grid-cols-3 gap-3">
        <SelectInput
          label="Transition In"
          value={slot.transition_in}
          options={TRANSITION_IN_OPTIONS}
          onChange={(v) => set("transition_in", v)}
        />
        <SelectInput
          label="Color Hint"
          value={slot.color_hint}
          options={COLOR_HINT_OPTIONS}
          onChange={(v) => set("color_hint", v)}
        />
        <NumberInput
          label="Energy"
          value={slot.energy}
          onChange={(v) => set("energy", v)}
          step={0.5}
          min={0}
          max={10}
        />
      </div>

      <NumberInput
        label="Priority"
        value={slot.priority}
        onChange={(v) => set("priority", v)}
        step={1}
        min={1}
        max={10}
      />

      {/* Text Overlays */}
      <div className="border-t border-zinc-800 pt-3 mt-3">
        <div className="flex items-center justify-between mb-2">
          <h4 className="text-xs font-medium text-zinc-400 uppercase tracking-wide">
            Text Overlays ({slot.text_overlays.length})
          </h4>
          <button
            onClick={() => dispatch({ type: "ADD_OVERLAY", slotIndex })}
            className="text-xs text-blue-400 hover:text-blue-300"
          >
            + Add
          </button>
        </div>

        {slot.text_overlays.length === 0 && (
          <p className="text-xs text-zinc-600">No text overlays</p>
        )}

        <div className="space-y-2">
          {slot.text_overlays.map((overlay, oi) => (
            <OverlayListItem
              key={oi}
              overlay={overlay}
              slotIndex={slotIndex}
              overlayIndex={oi}
              dispatch={dispatch}
              previewSubject={previewSubject}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Overlay list item (inline in slot panel) ────────────────────────────────

function OverlayListItem({
  overlay,
  slotIndex,
  overlayIndex,
  dispatch,
  previewSubject,
}: {
  overlay: RecipeTextOverlay;
  slotIndex: number;
  overlayIndex: number;
  dispatch: Dispatch<EditorAction>;
  previewSubject?: string;
}) {
  const set = (field: keyof RecipeTextOverlay, value: unknown) =>
    dispatch({
      type: "UPDATE_OVERLAY_FIELD",
      slotIndex,
      overlayIndex,
      field,
      value,
    });

  return (
    <div className="bg-zinc-900/50 border border-zinc-800 rounded p-3 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs text-zinc-400">
          {overlay.sample_text ? `"${overlay.sample_text.slice(0, 20)}"` : "(empty)"}{" "}
          <span className="text-zinc-600">| {overlay.effect} | {overlay.position}</span>
        </span>
        <button
          onClick={() =>
            dispatch({ type: "REMOVE_OVERLAY", slotIndex, overlayIndex })
          }
          className="text-xs text-zinc-600 hover:text-red-400"
        >
          Remove
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SelectInput
          label="Role"
          value={overlay.role}
          options={OVERLAY_ROLE_OPTIONS}
          onChange={(v) => set("role", v)}
        />
      </div>

      <div className="grid grid-cols-3 gap-2">
        <SelectInput
          label="Position"
          value={overlay.position}
          options={OVERLAY_POSITION_OPTIONS}
          onChange={(v) => set("position", v)}
        />
        <SelectInput
          label="Effect"
          value={overlay.effect}
          options={OVERLAY_EFFECT_OPTIONS}
          onChange={(v) => set("effect", v)}
        />
        <FontPicker
          overlay={overlay}
          onChange={(fontName) => set("font_family", fontName || undefined)}
        />
      </div>

      <div className="grid grid-cols-3 gap-2">
        <SelectInput
          label="Text Size"
          value={overlay.text_size}
          options={TEXT_SIZE_OPTIONS}
          onChange={(v) => set("text_size", v)}
        />
        <TextInput
          label="Text Color"
          value={overlay.text_color}
          onChange={(v) => set("text_color", v)}
        />
        <div>
          <TextInput
            label="Text"
            value={overlay.sample_text}
            onChange={(v) => set("sample_text", v)}
            maxLength={MAX_OVERLAY_TEXT_LEN}
          />
          {(() => {
            const resolved = resolveOverlayPreview(overlay, previewSubject || "");
            return resolved !== overlay.sample_text && resolved ? (
              <span className="text-xs text-zinc-500">&rarr; {resolved}</span>
            ) : null;
          })()}
        </div>
      </div>

      <div className="grid grid-cols-4 gap-2">
        <NumberInput
          label="Start (s)"
          value={overlay.start_s}
          onChange={(v) => set("start_s", v)}
          step={0.1}
          min={0}
        />
        <NumberInput
          label="End (s)"
          value={overlay.end_s}
          onChange={(v) => set("end_s", v)}
          step={0.1}
          min={0}
        />
        <NumberInput
          label="Start Override"
          value={overlay.start_s_override ?? 0}
          onChange={(v) => set("start_s_override", v === 0 ? null : v)}
          step={0.1}
        />
        <NumberInput
          label="End Override"
          value={overlay.end_s_override ?? 0}
          onChange={(v) => set("end_s_override", v === 0 ? null : v)}
          step={0.1}
        />
      </div>

      <div className="flex items-center gap-4">
        <CheckboxInput
          label="Darkening"
          checked={overlay.has_darkening}
          onChange={(v) => set("has_darkening", v)}
        />
        <CheckboxInput
          label="Narrowing"
          checked={overlay.has_narrowing}
          onChange={(v) => set("has_narrowing", v)}
        />
        <NumberInput
          label="Font Cycle Accel @s"
          value={overlay.font_cycle_accel_at_s ?? 0}
          onChange={(v) => set("font_cycle_accel_at_s", v === 0 ? null : v)}
          step={0.5}
        />
      </div>
    </div>
  );
}

// ── Interstitial Properties ─────────────────────────────────────────────────

function InterstitialProperties({
  interstitial,
  interstitialIndex,
  dispatch,
}: {
  interstitial: RecipeInterstitial;
  interstitialIndex: number;
  dispatch: Dispatch<EditorAction>;
}) {
  const set = (field: keyof RecipeInterstitial, value: unknown) =>
    dispatch({
      type: "UPDATE_INTERSTITIAL_FIELD",
      interstitialIndex,
      field,
      value,
    });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-white">
          Interstitial (after slot {interstitial.after_slot})
        </h3>
        <button
          onClick={() =>
            dispatch({ type: "REMOVE_INTERSTITIAL", interstitialIndex })
          }
          className="text-xs text-zinc-600 hover:text-red-400"
        >
          Remove
        </button>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <SelectInput
          label="Type"
          value={interstitial.type}
          options={INTERSTITIAL_TYPE_OPTIONS}
          onChange={(v) => set("type", v)}
        />
        <NumberInput
          label="After Slot"
          value={interstitial.after_slot}
          onChange={(v) => set("after_slot", v)}
          step={1}
          min={1}
        />
        <NumberInput
          label="Hold (s)"
          value={interstitial.hold_s}
          onChange={(v) => set("hold_s", v)}
          step={0.1}
          min={0.1}
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <TextInput
          label="Hold Color"
          value={interstitial.hold_color}
          onChange={(v) => set("hold_color", v)}
        />
        <NumberInput
          label="Animate (s)"
          value={interstitial.animate_s}
          onChange={(v) => set("animate_s", v)}
          step={0.1}
          min={0}
        />
      </div>
    </div>
  );
}

// ── Global Properties ───────────────────────────────────────────────────────

function GlobalProperties({
  recipe,
  dispatch,
}: {
  recipe: Recipe;
  dispatch: Dispatch<EditorAction>;
}) {
  const set = (field: keyof Recipe, value: unknown) =>
    dispatch({ type: "UPDATE_GLOBAL_FIELD", field, value });

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-medium text-white">Global Settings</h3>

      <div className="grid grid-cols-2 gap-3">
        <SelectInput
          label="Color Grade"
          value={recipe.color_grade}
          options={COLOR_HINT_OPTIONS}
          onChange={(v) => set("color_grade", v)}
        />
        <SelectInput
          label="Sync Style"
          value={recipe.sync_style}
          options={SYNC_STYLE_OPTIONS}
          onChange={(v) => set("sync_style", v)}
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <TextInput
          label="Copy Tone"
          value={recipe.copy_tone}
          onChange={(v) => set("copy_tone", v)}
        />
        <TextInput
          label="Pacing Style"
          value={recipe.pacing_style}
          onChange={(v) => set("pacing_style", v)}
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <TextInput
          label="Caption Style"
          value={recipe.caption_style}
          onChange={(v) => set("caption_style", v)}
        />
        <TextInput
          label="Transition Style"
          value={recipe.transition_style}
          onChange={(v) => set("transition_style", v)}
        />
      </div>

      <TextInput
        label="Creative Direction"
        value={recipe.creative_direction}
        onChange={(v) => set("creative_direction", v)}
      />

      {/* Interstitials */}
      <div className="border-t border-zinc-800 pt-3 mt-3">
        <div className="flex items-center justify-between mb-3">
          <h4 className="text-xs font-medium text-zinc-400 uppercase tracking-wide">
            Interstitials ({recipe.interstitials.length})
          </h4>
          <button
            onClick={() => dispatch({ type: "ADD_INTERSTITIAL" })}
            className="text-xs text-blue-400 hover:text-blue-300"
          >
            + Add
          </button>
        </div>

        {recipe.interstitials.map((inter, ii) => (
          <div key={ii} className="mb-3">
            <InterstitialProperties
              interstitial={inter}
              interstitialIndex={ii}
              dispatch={dispatch}
            />
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main PropertyPanel ──────────────────────────────────────────────────────

export function PropertyPanel({
  recipe,
  selection,
  dispatch,
  previewSubject,
}: {
  recipe: Recipe;
  selection: EditorSelection | null;
  dispatch: Dispatch<EditorAction>;
  previewSubject?: string;
}) {
  if (!selection) {
    return (
      <div className="text-sm text-zinc-500 py-8 text-center">
        Select a slot or click &ldquo;Global&rdquo; to edit properties
      </div>
    );
  }

  if (selection.type === "global") {
    return <GlobalProperties recipe={recipe} dispatch={dispatch} />;
  }

  if (selection.type === "slot") {
    const slot = recipe.slots[selection.slotIndex];
    if (!slot) return null;
    return (
      <SlotProperties
        slot={slot}
        slotIndex={selection.slotIndex}
        dispatch={dispatch}
        previewSubject={previewSubject}
      />
    );
  }

  if (selection.type === "overlay" && selection.overlayIndex != null) {
    const slot = recipe.slots[selection.slotIndex];
    const overlay = slot?.text_overlays[selection.overlayIndex];
    if (!slot || !overlay) return null;
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-white">
            Overlay: {overlay.sample_text ? `"${overlay.sample_text.slice(0, 25)}"` : "(empty)"}
          </h3>
          <button
            onClick={() =>
              dispatch({
                type: "REMOVE_OVERLAY",
                slotIndex: selection.slotIndex,
                overlayIndex: selection.overlayIndex!,
              })
            }
            className="text-xs text-zinc-600 hover:text-red-400"
          >
            Remove
          </button>
        </div>
        <OverlayListItem
          overlay={overlay}
          slotIndex={selection.slotIndex}
          overlayIndex={selection.overlayIndex}
          dispatch={dispatch}
          previewSubject={previewSubject}
        />
      </div>
    );
  }

  if (selection.type === "interstitial" && selection.interstitialIndex != null) {
    const inter = recipe.interstitials[selection.interstitialIndex];
    if (!inter) return null;
    return (
      <InterstitialProperties
        interstitial={inter}
        interstitialIndex={selection.interstitialIndex}
        dispatch={dispatch}
      />
    );
  }

  return null;
}
