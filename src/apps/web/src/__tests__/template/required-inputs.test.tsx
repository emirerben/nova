/**
 * Conditional rendering of `required_inputs` on /template/[id].
 *
 * The page should render input fields only for templates that declare
 * required_inputs, and skip the section entirely otherwise.
 */

import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import type { TemplateListItem } from "@/lib/api";

// Mock Next.js navigation hooks used by the page.
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
  useParams: () => ({ id: "tpl-123" }),
}));

// Mock the API client + side effects so the page renders synchronously.
const listTemplatesMock = jest.fn<Promise<TemplateListItem[]>, []>();
jest.mock("@/lib/api", () => ({
  listTemplates: () => listTemplatesMock(),
  createTemplateJob: jest.fn(),
  getDriveImportBatchStatus: jest.fn(),
  getBatchPresignedUrls: jest.fn(),
  importBatchFromDrive: jest.fn(),
  normaliseMimeType: (m: string) => m,
  uploadFileToGcs: jest.fn(),
}));

jest.mock("@/hooks/useArchitectureData", () => ({
  trackRecentJob: jest.fn(),
}));

jest.mock("@/lib/batch-storage", () => ({
  saveBatchToStorage: jest.fn(),
  readBatchFromStorage: () => null,
  clearBatchStorage: jest.fn(),
}));

jest.mock("@/lib/google-drive-picker", () => ({
  preloadDriveScripts: jest.fn(() => Promise.resolve()),
  requestDriveAccessToken: jest.fn(),
  openDrivePicker: jest.fn(),
}));

// SlotBoundUpload imports api.ts; mock it to avoid touching real fetch.
jest.mock("@/app/template/[id]/SlotBoundUpload", () => ({
  __esModule: true,
  default: () => null,
}));

import TemplateDetailPage from "@/app/template/[id]/page";

function makeTemplate(overrides: Partial<TemplateListItem> = {}): TemplateListItem {
  return {
    id: "tpl-123",
    name: "Test Template",
    gcs_path: "x",
    analysis_status: "ready",
    slot_count: 5,
    total_duration_s: 24,
    copy_tone: "casual",
    thumbnail_url: null,
    required_clips_min: 5,
    required_clips_max: 10,
    slots: [],
    required_inputs: [],
    ...overrides,
  };
}

describe("/template/[id] required_inputs conditional rendering", () => {
  beforeEach(() => {
    listTemplatesMock.mockReset();
  });

  it("does not render an inputs section when required_inputs is empty", async () => {
    listTemplatesMock.mockResolvedValueOnce([makeTemplate()]);
    render(<TemplateDetailPage />);

    // Wait for the template to load and the body to render.
    await screen.findByText("Test Template");

    // No location prompt because required_inputs is empty.
    expect(screen.queryByText(/Where was this filmed/i)).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText(/Tokyo/i)).not.toBeInTheDocument();
  });

  it("renders one labeled input per required_input entry", async () => {
    listTemplatesMock.mockResolvedValueOnce([
      makeTemplate({
        required_inputs: [
          {
            key: "location",
            label: "Where was this filmed?",
            placeholder: "Tokyo, Peru, your hometown…",
            max_length: 50,
            required: false,
          },
        ],
      }),
    ]);
    render(<TemplateDetailPage />);

    await screen.findByText("Test Template");
    expect(screen.getByText(/Where was this filmed/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText(/Tokyo/i)).toBeInTheDocument();
  });
});
