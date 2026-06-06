/**
 * Tests PR6: PayoffField emptyText prop.
 * - Custom emptyText shown when variants is null.
 * - Default text shown when emptyText prop is not provided.
 */

import React from "react";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";
import { PayoffField } from "@/components/progress";

describe("PayoffField emptyText prop", () => {
  it("shows custom emptyText when variants is null", () => {
    render(
      <PayoffField
        variants={null}
        renderCard={() => null}
        emptyText="Your video will appear here"
      />,
    );
    expect(screen.getByText("Your video will appear here")).toBeInTheDocument();
  });

  it("shows default text when emptyText is not provided", () => {
    render(<PayoffField variants={null} renderCard={() => null} />);
    expect(screen.getByText("Your edits will appear here")).toBeInTheDocument();
  });

  it("shows default text when emptyText is undefined", () => {
    render(
      <PayoffField variants={null} renderCard={() => null} emptyText={undefined} />,
    );
    expect(screen.getByText("Your edits will appear here")).toBeInTheDocument();
  });

  it("custom emptyText does not appear when variants has entries", () => {
    const variants = [{ variant_id: "v1", render_status: "ready" }];
    render(
      <PayoffField
        variants={variants}
        renderCard={(v) => <div key={v.variant_id}>card</div>}
        emptyText="Your video will appear here"
      />,
    );
    // Empty state is hidden when variants are present
    expect(screen.queryByText("Your video will appear here")).toBeNull();
  });
});
