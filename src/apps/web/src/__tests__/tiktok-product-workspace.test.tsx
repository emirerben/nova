import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import TikTokProductWorkspace from "@/app/tiktok/TikTokProductWorkspace";

describe("public TikTok product workspace", () => {
  test("renders the exact approved video when the server resolves one", () => {
    render(<TikTokProductWorkspace videoSrc="https://cdn.example.com/approved.mp4" />);

    expect(screen.getByLabelText("The exact video you approved")).toHaveAttribute(
      "src",
      "https://cdn.example.com/approved.mp4",
    );
    expect(screen.queryByText("A quiet morning in the studio")).not.toBeInTheDocument();
  });

  test("renders a useful approved-render fallback when no video is available", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    expect(screen.getAllByText("The exact video you approved")).toHaveLength(2);
    expect(screen.getByText("A quiet morning in the studio")).toBeInTheDocument();
    expect(screen.queryByLabelText("The exact video you approved")).not.toBeInTheDocument();
  });

  test("requires manual privacy and music confirmation before review", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    const reviewButton = screen.getByRole("button", { name: "Review submission" });
    const privacy = screen.getByRole("radio", { name: /Only you/ });
    const music = screen.getByRole("checkbox", { name: /right to use the music/ });

    expect(privacy).not.toBeChecked();
    expect(music).not.toBeChecked();
    expect(reviewButton).toBeDisabled();

    fireEvent.click(privacy);
    expect(reviewButton).toBeDisabled();

    fireEvent.click(music);
    expect(reviewButton).toBeEnabled();
  });

  test("requires and summarizes the available commercial-content declaration", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    const commercial = screen.getByRole("checkbox", {
      name: "This video promotes a brand, product, or service",
    });
    expect(commercial).not.toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "Your brand" })).not.toBeInTheDocument();

    fireEvent.click(commercial);
    const ownBrand = screen.getByRole("checkbox", { name: "Your brand" });
    expect(ownBrand).not.toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "Branded content — unavailable with Only you" }),
    ).toBeDisabled();

    fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /right to use the music/ }));
    expect(screen.getByRole("button", { name: "Review submission" })).toBeDisabled();

    fireEvent.click(ownBrand);
    fireEvent.click(screen.getByRole("button", { name: "Review submission" }));

    expect(screen.getByText("Commercial content")).toBeInTheDocument();
    expect(screen.getByText("Your brand")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(
      screen.getByRole("checkbox", {
        name: "This video promotes a brand, product, or service",
      }),
    ).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Your brand" })).toBeChecked();
  });

  test("walks through confirmation and a private lifecycle receipt without publishing", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /right to use the music/ }));
    fireEvent.click(screen.getByRole("button", { name: "Review submission" }));

    expect(screen.getByRole("heading", { name: "Confirm the exact submission" })).toBeInTheDocument();
    expect(screen.getByText("@review_sandbox")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Complete preview" }));

    expect(screen.getByRole("heading", { name: "Published privately on TikTok" })).toBeInTheDocument();
    expect(screen.getByText("Visibility: Only you")).toBeInTheDocument();
    expect(screen.getByText(/never submits a post/i)).toBeInTheDocument();
  });

  test("demonstrates the video.upload handoff without claiming that it creates a post", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    fireEvent.click(screen.getByRole("radio", { name: /Finish in TikTok/ }));
    expect(screen.getByRole("heading", { name: "TikTok app inbox handoff" })).toBeInTheDocument();
    expect(screen.getAllByText(/inbox notification/i)).not.toHaveLength(0);
    expect(screen.getByRole("button", { name: "Review handoff" })).toBeDisabled();

    fireEvent.click(screen.getByRole("checkbox", { name: /must open the TikTok app on my phone/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /right to use the music/ }));
    fireEvent.click(screen.getByRole("button", { name: "Review handoff" }));

    expect(screen.getByRole("heading", { name: "Confirm the TikTok handoff" })).toBeInTheDocument();
    expect(screen.getByText("Open the notification in the TikTok app")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Preview inbox handoff" }));

    expect(screen.getByRole("heading", { name: "Waiting in your TikTok inbox" })).toBeInTheDocument();
    expect(screen.getByText("No post created")).toBeInTheDocument();
  });

  test("shows creator edits and disclosures in confirmation and supports going back", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    fireEvent.change(screen.getByRole("textbox", { name: "Caption and hashtags" }), {
      target: { value: "Updated caption #review" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: "Allow comments" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: "This video promotes a brand, product, or service" }),
    );
    fireEvent.click(screen.getByRole("checkbox", { name: "Your brand" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: "This content includes AI-generated material" }),
    );
    fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /right to use the music/ }));
    fireEvent.click(screen.getByRole("button", { name: "Review submission" }));

    expect(screen.getByText("Updated caption #review")).toBeInTheDocument();
    expect(screen.getByText("Allowed")).toBeInTheDocument();
    expect(screen.getByText("Declared")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    expect(screen.getByRole("textbox", { name: "Caption and hashtags" })).toHaveValue(
      "Updated caption #review",
    );
    expect(screen.getByRole("checkbox", { name: "Allow comments" })).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "This content includes AI-generated material" }),
    ).toBeChecked();
  });

  test("restart returns to details and clears submission choices", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    fireEvent.click(screen.getByRole("checkbox", { name: "Allow comments" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: "This content includes AI-generated material" }),
    );
    fireEvent.click(screen.getByRole("radio", { name: /Only you/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /right to use the music/ }));
    fireEvent.click(screen.getByRole("button", { name: "Review submission" }));
    fireEvent.click(screen.getByRole("button", { name: "Complete preview" }));
    fireEvent.click(screen.getByRole("button", { name: "Restart walkthrough" }));

    expect(screen.getByRole("heading", { name: "Creator-controlled details" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Only you/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Allow comments" })).not.toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: "This video promotes a brand, product, or service" }),
    ).not.toBeChecked();
    expect(screen.queryByRole("checkbox", { name: "Your brand" })).not.toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", { name: "This content includes AI-generated material" }),
    ).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /right to use the music/ })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Review submission" })).toBeDisabled();
  });

  test("keeps unsupported TikTok interactions disabled", () => {
    render(<TikTokProductWorkspace videoSrc={null} />);

    expect(screen.getByRole("checkbox", { name: "Allow Duet" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "Allow Stitch" })).toBeDisabled();
  });
});
