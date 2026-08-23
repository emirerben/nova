import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import PageError from "@/app/error";
import GlobalError from "@/app/global-error";

const error = Object.assign(new Error("backend detail"), { digest: "support-123" });

describe("creator error boundaries", () => {
  it("uses safe recovery copy and a videos destination", () => {
    render(<PageError error={error} reset={jest.fn()} />);

    expect(screen.getByRole("heading", { name: "This page couldn't load" })).toBeInTheDocument();
    expect(
      screen.getByText("Your saved videos are safe. Reload this page, or return to your videos."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to My videos" })).toHaveAttribute("href", "/plan");
    expect(screen.getByText("Support reference: support-123")).toBeInTheDocument();
    expect(screen.queryByText("backend detail")).not.toBeInTheDocument();
  });

  it("keeps the global boundary aligned with the creator boundary", () => {
    render(<GlobalError error={error} reset={jest.fn()} />);

    expect(screen.getByRole("heading", { name: "This page couldn't load" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to My videos" })).toHaveAttribute("href", "/plan");
    expect(screen.getByText("Support reference: support-123")).toBeInTheDocument();
  });
});
