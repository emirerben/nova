process.env.NEXT_PUBLIC_CREATION_HUB_ENABLED = "true";

import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { WorkspaceHome } from "@/app/plan/_components/workspace/WorkspaceHome";

jest.mock("@/app/plan/_components/workspace/IdeasHome", () => ({
  IdeasHome: () => <section>Existing ideas ledger</section>,
}));

jest.mock("@/app/plan/_components/SeedUploadCard", () => ({
  __esModule: true,
  default: () => <div>Activation</div>,
}));

describe("WorkspaceHome creation hub", () => {
  beforeEach(() => {
    delete process.env.NEXT_PUBLIC_MANUAL_EDITOR_ENABLED;
  });

  it("makes video creation primary and keeps the ideas ledger as the secondary destination", () => {
    render(
      <WorkspaceHome
        plan={{ id: "plan-1", items: [], activation_status: "ready" } as never}
        onRefresh={jest.fn()}
        onPlanChange={jest.fn()}
        onError={jest.fn()}
      />,
    );

    expect(
      screen.getByRole("heading", { name: /turn raw clips into a video worth sharing/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /make a video with kria/i }),
    ).toHaveAttribute("href", "/create");
    expect(screen.getByRole("link", { name: /plan content/i })).toHaveAttribute(
      "href",
      "#ideas",
    );
    expect(screen.getByText("Existing ideas ledger").parentElement).toHaveAttribute(
      "id",
      "ideas",
    );
    expect(screen.queryByText(/edit myself/i)).not.toBeInTheDocument();
  });

  it("exposes the same manual editor only when its acceptance flag is enabled", () => {
    process.env.NEXT_PUBLIC_MANUAL_EDITOR_ENABLED = "true";

    render(
      <WorkspaceHome
        plan={{ id: "plan-1", items: [], activation_status: "ready" } as never}
        onRefresh={jest.fn()}
        onPlanChange={jest.fn()}
        onError={jest.fn()}
      />,
    );

    expect(screen.getByRole("link", { name: /edit myself/i })).toHaveAttribute(
      "href",
      "/create/manual",
    );
  });
});
