/**
 * SlidePostPanel — the editor for a mixed-media slide post (plans/024).
 *
 * Covers: reorder (buttons), remove, cover selection, caption commit-on-blur,
 * profile switch, add-from-pool, compose, export gating on validation
 * errors, and that `onRefetch` fires after every mutation (the panel never
 * holds its own copy of server truth).
 */

import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { PlanItem, PlanItemVariant, PoolAsset } from "@/lib/plan-api";

jest.mock("@/lib/plan-api", () => ({
  __esModule: true,
  putSlidePostDraft: jest.fn(),
  composeSlidePost: jest.fn(),
  listPoolAssets: jest.fn(),
  getSlidePostBundleUrl: jest.fn(),
}));

import {
  composeSlidePost,
  getSlidePostBundleUrl,
  listPoolAssets,
  putSlidePostDraft,
} from "@/lib/plan-api";
import SlidePostPanel from "@/app/plan/items/[id]/components/SlidePostPanel";

const mockPutDraft = putSlidePostDraft as jest.Mock;
const mockCompose = composeSlidePost as jest.Mock;
const mockListPoolAssets = listPoolAssets as jest.Mock;
const mockGetBundleUrl = getSlidePostBundleUrl as jest.Mock;

function makeItem(overrides: Partial<PlanItem> = {}): PlanItem {
  return {
    id: "item-1",
    day_index: null,
    theme: "Trip recap",
    idea: "Trip recap",
    position: 0,
    filming_suggestion: null,
    rationale: null,
    filming_guide: [],
    clip_gcs_paths: [],
    status: "idea",
    current_job_id: null,
    user_edited: false,
    edit_format: "slides",
    landscape_fit: "fit",
    slide_post: {
      schema_version: 1,
      version: 1,
      platform_profile: "tiktok_photo",
      slides: [
        { id: "s0", asset_id: "a0", kind: "image" },
        { id: "s1", asset_id: "a1", kind: "image" },
      ],
      cover_index: 0,
      caption: "hello",
      rendered_version: 1,
      user_edited: false,
    },
    ...overrides,
  } as PlanItem;
}

beforeEach(() => {
  jest.clearAllMocks();
  mockListPoolAssets.mockResolvedValue({ assets: [], max_assets: 100 });
  mockPutDraft.mockResolvedValue({});
  mockCompose.mockResolvedValue({});
});

describe("SlidePostPanel", () => {
  it("renders every slide from the draft", async () => {
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());
    expect(screen.getByRole("list", { name: "Slides" })).toBeInTheDocument();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("moving a slide later saves the reordered list and refetches", async () => {
    const onRefetch = jest.fn();
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={onRefetch} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Move slide 1 later" }));

    await waitFor(() => expect(mockPutDraft).toHaveBeenCalled());
    const [, body] = mockPutDraft.mock.calls[0];
    expect(body.slides.map((s: { id: string }) => s.id)).toEqual(["s1", "s0"]);
    expect(onRefetch).toHaveBeenCalled();
  });

  it("removing a slide drops it from the saved draft", async () => {
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Remove slide 1" }));

    await waitFor(() => expect(mockPutDraft).toHaveBeenCalled());
    const [, body] = mockPutDraft.mock.calls[0];
    expect(body.slides).toHaveLength(1);
    expect(body.slides[0].id).toBe("s1");
  });

  it("setting a new cover updates cover_index in the saved draft", async () => {
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Set slide 2 as cover" }));

    await waitFor(() => expect(mockPutDraft).toHaveBeenCalled());
    const [, body] = mockPutDraft.mock.calls[0];
    expect(body.cover_index).toBe(1);
  });

  it("commits the caption only on blur, not on every keystroke", async () => {
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    const textarea = screen.getByLabelText("Caption");
    fireEvent.change(textarea, { target: { value: "hello world" } });
    expect(mockPutDraft).not.toHaveBeenCalled();

    fireEvent.blur(textarea);
    await waitFor(() => expect(mockPutDraft).toHaveBeenCalled());
    const [, body] = mockPutDraft.mock.calls[0];
    expect(body.caption).toBe("hello world");
  });

  it("switching platform profile saves the new profile", async () => {
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Instagram carousel" }));

    await waitFor(() => expect(mockPutDraft).toHaveBeenCalled());
    const [, body] = mockPutDraft.mock.calls[0];
    expect(body.platform_profile).toBe("instagram_carousel");
  });

  it("adding a ready pool asset not already in the draft appends it", async () => {
    const poolAsset: PoolAsset = {
      id: "a2",
      kind: "image",
      status: "ready",
      media_status: "ready",
      source_filename: "c.jpg",
      duration_s: null,
      aspect: null,
      subject: null,
      user_context: "",
      display_url: "https://example.com/c.jpg",
      deduped: false,
      gcs_path: "users/u/plan/item-1/pool/c.jpg",
    };
    mockListPoolAssets.mockResolvedValue({ assets: [poolAsset], max_assets: 100 });
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={jest.fn()} />);
    await waitFor(() => screen.getByRole("button", { name: "Add to post" }));

    fireEvent.click(screen.getByRole("button", { name: "Add to post" }));

    await waitFor(() => expect(mockPutDraft).toHaveBeenCalled());
    const [, body] = mockPutDraft.mock.calls[0];
    expect(body.slides).toHaveLength(3);
    expect(body.slides[2].asset_id).toBe("a2");
  });

  it("an asset already in the draft is not offered again in Add", async () => {
    const inDraft: PoolAsset = {
      id: "a0",
      kind: "image",
      status: "ready",
      media_status: "ready",
      source_filename: "a.jpg",
      duration_s: null,
      aspect: null,
      subject: null,
      user_context: "",
      display_url: null,
      deduped: false,
      gcs_path: "users/u/plan/item-1/pool/a.jpg",
    };
    mockListPoolAssets.mockResolvedValue({ assets: [inDraft], max_assets: 100 });
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    expect(screen.queryByRole("button", { name: "Add to post" })).not.toBeInTheDocument();
  });

  it("composing calls the compose endpoint and refetches", async () => {
    const onRefetch = jest.fn();
    render(<SlidePostPanel item={makeItem()} variant={null} onRefetch={onRefetch} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "✨ Compose with Kria" }));

    await waitFor(() => expect(mockCompose).toHaveBeenCalledWith("item-1", { platformProfile: "tiktok_photo" }));
    await waitFor(() => expect(onRefetch).toHaveBeenCalled());
  });

  it("export is disabled while the rendered variant has validation errors", async () => {
    const variant = {
      variant_id: "slides",
      slide_post: {
        platform_profile: "tiktok_photo",
        caption: "hello",
        cover_index: 0,
        validation: {
          ok: false,
          errors: [{ code: "unsupported_media_kind", message: "no videos allowed" }],
          warnings: [],
        },
      },
    } as unknown as PlanItemVariant;
    render(<SlidePostPanel item={makeItem()} variant={variant} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    expect(screen.getByText("no videos allowed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export" })).toBeDisabled();
  });

  it("export opens the signed bundle URL when validation passes", async () => {
    const variant = {
      variant_id: "slides",
      slide_post: {
        platform_profile: "tiktok_photo",
        caption: "hello",
        cover_index: 0,
        validation: { ok: true, errors: [], warnings: [] },
      },
    } as unknown as PlanItemVariant;
    mockGetBundleUrl.mockResolvedValue({ url: "https://signed.example/bundle.zip" });
    const openSpy = jest.spyOn(window, "open").mockImplementation(() => null);
    render(<SlidePostPanel item={makeItem()} variant={variant} onRefetch={jest.fn()} />);
    await waitFor(() => expect(mockListPoolAssets).toHaveBeenCalled());

    fireEvent.click(screen.getByRole("button", { name: "Export" }));

    await waitFor(() =>
      expect(openSpy).toHaveBeenCalledWith(
        "https://signed.example/bundle.zip",
        "_blank",
        "noopener,noreferrer",
      ),
    );
    openSpy.mockRestore();
  });
});
