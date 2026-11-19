import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import PageError from "@/app/error";

const error = Object.assign(new Error("backend detail"), { digest: "support-123" });

describe("creator error boundaries", () => {
  it("uses safe recovery copy and a home destination", () => {
    render(<PageError error={error} reset={jest.fn()} />);

    expect(screen.getByRole("heading", { name: "This page couldn't load" })).toBeInTheDocument();
    expect(
      screen.getByText("Reload this page, or go back to the home page."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to home" })).toHaveAttribute("href", "/");
    expect(screen.getByText("Support reference: support-123")).toBeInTheDocument();
    expect(screen.queryByText("backend detail")).not.toBeInTheDocument();
  });
});
