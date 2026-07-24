/**
 * Tests for _editor/editor-capabilities.ts — the pure server-capability →
 * tooltip copy mapping (extracted from EditorShell so it is unit-testable
 * without mounting the full shell), plus ToolRail-level render pins for the
 * plan-010 OV-1 contract: on a subtitled variant with sfx/overlays live,
 * Text/Styles stay disabled with the honest Captions-tab tooltip while
 * Sounds/Overlays are enabled. Disabled rail buttons use the
 * focusable-disabled pattern (aria-disabled + aria-describedby reason, no-op
 * click) so the reason is reachable by keyboard/SR/touch.
 */

import "@testing-library/jest-dom";
import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  CAPTIONS_TAB_REASON,
  TEXT_ELEMENTS_LOCKED_FALLBACK,
  computeToolDisabledReasons,
  editorReasonCopy,
  textElementsLockedCopy,
} from "@/app/plan/items/[id]/_editor/editor-capabilities";
import ToolRail from "@/app/plan/items/[id]/_editor/ToolRail";
import type { EditorCapabilities } from "@/lib/plan-api";

/** Subtitled variant after the plan-010 gate lift: effects live, text gated. */
const SUBTITLED_EFFECTS_LIVE: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: true,
  overlays: true,
  // The live server sends the human sentence (CAPTION_TAB_COPY byte-stable
  // contract), NOT the "caption_archetype" code — keep this fixture realistic.
  reason: CAPTIONS_TAB_REASON,
};

/**
 * A lyrics-synced song variant: per-element text is genuinely locked
 * (beat-synced to vocal onsets, lyric_injector.py), but sfx/overlays are
 * already additive-safe, and a whole-style-set swap (dispatch_change_style)
 * is also safe — it re-renders from the track, only visual style changes.
 * The real prod shape observed for job 2a00c97d-...: {sfx: true,
 * overlays: true, suggestions: false, reason: "lyrics_sync"}.
 */
const LYRICS_SYNC_EFFECTS_LIVE: EditorCapabilities = {
  text_elements: false,
  timeline: false,
  split_clips: false,
  mix: false,
  sfx: true,
  overlays: true,
  reason: "lyrics_sync",
};

describe("editorReasonCopy", () => {
  it("maps sound_effects_disabled to human copy", () => {
    expect(editorReasonCopy("sound_effects_disabled")).toBe(
      "sound effects are turned off right now",
    );
  });

  it("maps media_overlays_disabled to human copy", () => {
    expect(editorReasonCopy("media_overlays_disabled")).toBe(
      "overlays are turned off right now",
    );
  });

  it("maps no_video to human copy", () => {
    expect(editorReasonCopy("no_video")).toBe("waiting for this edit to finish rendering");
  });

  it("passes unknown reason codes through raw", () => {
    expect(editorReasonCopy("some_future_reason")).toBe("some_future_reason");
  });

  it("pins the caption-edit sentence byte-for-byte (server contract — CAPTION_TAB_COPY in generative_jobs.py sends this literal)", () => {
    expect(CAPTIONS_TAB_REASON).toBe("Captions can be selected and edited in this editor");
  });

  it("keeps the existing mappings and the empty-reason fallback", () => {
    expect(editorReasonCopy("caption_archetype")).toBe(CAPTIONS_TAB_REASON);
    expect(editorReasonCopy("locked_to_voiceover")).toBe("locked to your voiceover");
    expect(editorReasonCopy(null)).toBe("This version can't be edited.");
    expect(editorReasonCopy(undefined)).toBe("This version can't be edited.");
  });
});

describe("textElementsLockedCopy", () => {
  it("uses the mapped human copy for known reason codes", () => {
    expect(textElementsLockedCopy(SUBTITLED_EFFECTS_LIVE)).toBe(CAPTIONS_TAB_REASON);
  });

  it("falls back to the text-specific line when reason is null — never the whole-shell copy", () => {
    const copy = textElementsLockedCopy({ ...SUBTITLED_EFFECTS_LIVE, reason: undefined });
    expect(copy).toBe(TEXT_ELEMENTS_LOCKED_FALLBACK);
    expect(copy).not.toBe("This version can't be edited.");
  });

  it("falls back to the text-specific line for unmapped reason codes", () => {
    expect(
      textElementsLockedCopy({ ...SUBTITLED_EFFECTS_LIVE, reason: "some_future_reason" }),
    ).toBe(TEXT_ELEMENTS_LOCKED_FALLBACK);
  });

  it("keeps a server-authored human sentence verbatim (the live caption reason is the sentence, not a code)", () => {
    expect(
      textElementsLockedCopy({ ...SUBTITLED_EFFECTS_LIVE, reason: "caption_archetype" }),
    ).toBe(CAPTIONS_TAB_REASON);
    expect(
      textElementsLockedCopy({
        ...SUBTITLED_EFFECTS_LIVE,
        reason: "Some future server-authored sentence",
      }),
    ).toBe("Some future server-authored sentence");
  });
});

describe("computeToolDisabledReasons", () => {
  it("disables ONLY text/styles with the Captions-tab copy when text_elements is false but the shell is editable (OV-1)", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: SUBTITLED_EFFECTS_LIVE,
        readOnly: false,
        readOnlyReason: CAPTIONS_TAB_REASON,
      }),
    ).toEqual({
      text: CAPTIONS_TAB_REASON,
      styles: CAPTIONS_TAB_REASON,
    });
  });

  it("uses the text-specific fallback (not the whole-shell copy) when text_elements is false with no reason", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: { ...SUBTITLED_EFFECTS_LIVE, reason: undefined },
        readOnly: false,
        readOnlyReason: "This version can't be edited.",
      }),
    ).toEqual({
      text: TEXT_ELEMENTS_LOCKED_FALLBACK,
      styles: TEXT_ELEMENTS_LOCKED_FALLBACK,
    });
  });

  it("uses the read-only reason for text/styles when the whole shell is read-only", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: { ...SUBTITLED_EFFECTS_LIVE, sfx: false, overlays: false },
        readOnly: true,
        readOnlyReason: CAPTIONS_TAB_REASON,
      }),
    ).toMatchObject({ text: CAPTIONS_TAB_REASON, styles: CAPTIONS_TAB_REASON });
  });

  it("surfaces the kill-switch reasons as human copy on sounds/overlays", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: {
          ...SUBTITLED_EFFECTS_LIVE,
          sfx: false,
          overlays: false,
          sfx_reason: "sound_effects_disabled",
          overlays_reason: "media_overlays_disabled",
        },
        readOnly: false,
        readOnlyReason: CAPTIONS_TAB_REASON,
      }),
    ).toMatchObject({
      sounds: "sound effects are turned off right now",
      overlays: "overlays are turned off right now",
    });
  });

  it("falls back to lowercase fragment copy when sfx/overlays are false with no server reason", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: {
          ...SUBTITLED_EFFECTS_LIVE,
          text_elements: true,
          sfx: false,
          overlays: false,
          sfx_reason: null,
          overlays_reason: null,
        },
        readOnly: false,
        readOnlyReason: CAPTIONS_TAB_REASON,
      }),
    ).toEqual({
      sounds: "sound effects aren't available for this edit",
      overlays: "media overlays aren't available for this edit",
    });
  });

  it("keeps Styles enabled (but Text locked) for a lyrics variant when isLyrics is true", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: LYRICS_SYNC_EFFECTS_LIVE,
        readOnly: false,
        readOnlyReason: "lyrics are synced to the song",
        isLyrics: true,
      }),
    ).toEqual({
      text: "lyrics are synced to the song",
      // styles intentionally absent — not in the disabled map.
    });
  });

  it("disables Styles too when the same lyrics capability shape is passed WITHOUT isLyrics (regression guard — the flag, not the capability shape, drives the exception)", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: LYRICS_SYNC_EFFECTS_LIVE,
        readOnly: false,
        readOnlyReason: "lyrics are synced to the song",
      }),
    ).toEqual({
      text: "lyrics are synced to the song",
      styles: "lyrics are synced to the song",
    });
  });

  it("does not let isLyrics leak into non-lyrics text_elements-false cases (captions stay fully text-locked)", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: SUBTITLED_EFFECTS_LIVE,
        readOnly: false,
        readOnlyReason: CAPTIONS_TAB_REASON,
        isLyrics: false,
      }),
    ).toEqual({
      text: CAPTIONS_TAB_REASON,
      styles: CAPTIONS_TAB_REASON,
    });
  });

  it("disables nothing on a fully-capable variant", () => {
    expect(
      computeToolDisabledReasons({
        capabilities: {
          text_elements: true,
          timeline: true,
          split_clips: true,
          mix: true,
          sfx: true,
          overlays: true,
        },
        readOnly: false,
        readOnlyReason: "This version can't be edited.",
      }),
    ).toEqual({});
  });
});

describe("ToolRail with the lyrics-sync effects-live disable map", () => {
  it("marks Text focusable-disabled while Styles/Sounds/Overlays stay enabled", () => {
    render(
      <ToolRail
        activeTool={null}
        disabledTools={computeToolDisabledReasons({
          capabilities: LYRICS_SYNC_EFFECTS_LIVE,
          readOnly: false,
          readOnlyReason: "lyrics are synced to the song",
          isLyrics: true,
        })}
        onToggleTool={() => {}}
      />,
    );

    const text = screen.getByRole("button", { name: "Text tool" });
    expect(text).not.toBeDisabled();
    expect(text).toHaveAttribute("aria-disabled", "true");
    expect(text).toHaveAttribute("title", "Text — lyrics are synced to the song");

    for (const name of ["Styles tool", "Sounds tool", "Overlays tool"]) {
      const tool = screen.getByRole("button", { name });
      expect(tool).toBeEnabled();
      expect(tool).not.toHaveAttribute("aria-disabled");
    }
  });
});

describe("ToolRail with the subtitled effects-live disable map", () => {
  function renderRail(onToggleTool: (tool: string) => void = () => {}) {
    return render(
      <ToolRail
        activeTool={null}
        disabledTools={computeToolDisabledReasons({
          capabilities: SUBTITLED_EFFECTS_LIVE,
          readOnly: false,
          readOnlyReason: CAPTIONS_TAB_REASON,
        })}
        onToggleTool={onToggleTool}
      />,
    );
  }

  it("marks Text/Styles focusable-disabled (aria-disabled, not disabled) while Sounds/Overlays stay enabled", () => {
    renderRail();

    const text = screen.getByRole("button", { name: "Text tool" });
    const styles = screen.getByRole("button", { name: "Styles tool" });
    // Focusable-disabled: reachable by keyboard/SR — the native attribute is off.
    expect(text).not.toBeDisabled();
    expect(styles).not.toBeDisabled();
    expect(text).toHaveAttribute("aria-disabled", "true");
    expect(styles).toHaveAttribute("aria-disabled", "true");
    // Title kept as a pointer-hover bonus.
    expect(text).toHaveAttribute("title", `Text — ${CAPTIONS_TAB_REASON}`);
    expect(styles).toHaveAttribute("title", `Styles — ${CAPTIONS_TAB_REASON}`);

    const sounds = screen.getByRole("button", { name: "Sounds tool" });
    const overlays = screen.getByRole("button", { name: "Overlays tool" });
    expect(sounds).toBeEnabled();
    expect(sounds).not.toHaveAttribute("aria-disabled");
    expect(overlays).toBeEnabled();
    expect(overlays).not.toHaveAttribute("aria-disabled");
  });

  it("exposes the disable reason via aria-describedby → a visually-hidden element", () => {
    renderRail();

    const text = screen.getByRole("button", { name: "Text tool" });
    const describedBy = text.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    const reason = document.getElementById(describedBy!);
    expect(reason).toHaveTextContent(CAPTIONS_TAB_REASON);
    expect(reason).toHaveClass("sr-only");
    expect(text).toHaveAccessibleDescription(CAPTIONS_TAB_REASON);

    // Enabled tools carry no stale description hook.
    expect(
      screen.getByRole("button", { name: "Sounds tool" }),
    ).not.toHaveAttribute("aria-describedby");
  });

  it("no-ops clicks on aria-disabled tools but still fires enabled ones", () => {
    const onToggleTool = jest.fn();
    renderRail(onToggleTool);

    fireEvent.click(screen.getByRole("button", { name: "Text tool" }));
    fireEvent.click(screen.getByRole("button", { name: "Styles tool" }));
    expect(onToggleTool).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Sounds tool" }));
    expect(onToggleTool).toHaveBeenCalledWith("sounds");
  });
});
