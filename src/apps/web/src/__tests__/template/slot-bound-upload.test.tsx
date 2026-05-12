/**
 * SlotBoundUpload helper-text and error-wording tests.
 *
 * Covers the user-facing messaging that tells someone uploading clips to a
 * mixed-media template which slot wants which media type. Regression target:
 * the original wording "Pick a video (MP4, MOV)" did not name the slot or
 * point users at the other slot, so users picking a photo for slot 1 of
 * "Impressing Myself" thought the template was broken.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { SlotSummary, TemplateListItem } from "@/lib/api";
import SlotBoundUpload, {
  slotHelperText,
  mismatchError,
} from "@/app/template/[id]/SlotBoundUpload";

// SlotBoundUpload pulls in `@/lib/api` only for createTemplateJob /
// uploadTemplatePhoto / getBatchPresignedUrls etc. Mock them so the
// component is testable without real fetches.
jest.mock("@/lib/api", () => ({
  __esModule: true,
  uploadTemplatePhoto: jest.fn(),
  getBatchPresignedUrls: jest.fn(),
  uploadFileToGcs: jest.fn(),
  createTemplateJob: jest.fn(),
  normaliseMimeType: (m: string) => m,
}));

function mkSlot(position: number, media_type: SlotSummary["media_type"]): SlotSummary {
  return { position, target_duration_s: 3.5, media_type };
}

function mkTemplate(slots: SlotSummary[]): TemplateListItem {
  return {
    id: "936e9558-248f-49be-b857-2b9a193522c6",
    name: "Impressing Myself",
    gcs_path: "x",
    analysis_status: "ready",
    slot_count: slots.length,
    total_duration_s: slots.reduce((a, b) => a + b.target_duration_s, 0),
    copy_tone: "casual",
    thumbnail_url: null,
    required_clips_min: slots.length,
    required_clips_max: slots.length,
    slots,
    required_inputs: [],
  };
}

describe("slotHelperText", () => {
  it("names the other slot when the template is mixed-media", () => {
    const slots = [mkSlot(1, "video"), mkSlot(2, "photo")];
    expect(slotHelperText(slots[0], slots)).toBe(
      "Moving clip — mp4 or mov (the photo goes in slot 2).",
    );
    expect(slotHelperText(slots[1], slots)).toBe(
      "Still image — jpg, png, webp, or heic (the video goes in slot 1).",
    );
  });

  it("omits the opposite-slot hint when every slot is the same type", () => {
    const slots = [mkSlot(1, "video"), mkSlot(2, "video"), mkSlot(3, "video")];
    expect(slotHelperText(slots[1], slots)).toBe("Moving clip — mp4 or mov.");
  });
});

describe("mismatchError", () => {
  it("names the slot and points to the other slot when mixed-media", () => {
    const slots = [mkSlot(1, "video"), mkSlot(2, "photo")];
    expect(mismatchError(slots[0], slots)).toBe(
      "Slot 1 needs a video (mp4/mov). Photos go in slot 2.",
    );
    expect(mismatchError(slots[1], slots)).toBe(
      "Slot 2 needs a photo (jpg/png/webp/heic). Videos go in slot 1.",
    );
  });

  it("only names the slot when there is no opposite slot", () => {
    const slots = [mkSlot(1, "video"), mkSlot(2, "video")];
    expect(mismatchError(slots[0], slots)).toBe("Slot 1 needs a video (mp4/mov).");
  });
});

describe("<SlotBoundUpload /> rendering", () => {
  it("shows per-slot helper text for the Impressing Myself shape", () => {
    const template = mkTemplate([mkSlot(1, "video"), mkSlot(2, "photo")]);
    render(<SlotBoundUpload template={template} inputs={{}} onJobCreated={() => {}} />);

    expect(
      screen.getByText("Moving clip — mp4 or mov (the photo goes in slot 2)."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Still image — jpg, png, webp, or heic (the video goes in slot 1)."),
    ).toBeInTheDocument();
  });

  it("renders the new mismatch error when a wrong-type file is picked", () => {
    const template = mkTemplate([mkSlot(1, "video"), mkSlot(2, "photo")]);
    const { container } = render(
      <SlotBoundUpload template={template} inputs={{}} onJobCreated={() => {}} />,
    );

    // Simulate picking a PNG on slot 1 (video slot). The component renders
    // both file inputs as hidden — find them and fire the change event with
    // a fake File object.
    const inputs = container.querySelectorAll('input[type="file"]');
    expect(inputs).toHaveLength(2);

    const photo = new File(["fake"], "photo.png", { type: "image/png" });
    Object.defineProperty(inputs[0], "files", { value: [photo] });
    fireEvent.change(inputs[0]);

    expect(
      screen.getByText("Slot 1 needs a video (mp4/mov). Photos go in slot 2."),
    ).toBeInTheDocument();
  });
});
