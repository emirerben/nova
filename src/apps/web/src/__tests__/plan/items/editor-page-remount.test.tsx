import "@testing-library/jest-dom";
import React, { useEffect, useState } from "react";
import { render, screen } from "@testing-library/react";

let mockItemId = "item-1";
let mockVariantId = "song_text";
let mockMounts = 0;
let mockUnmounts = 0;

jest.mock("next/navigation", () => ({
  useParams: () => ({ id: mockItemId }),
  useSearchParams: () => new URLSearchParams(`variant=${mockVariantId}`),
}));

jest.mock("@/app/plan/items/[id]/_editor/EditorShell", () =>
  function MockEditorShell({
    itemId,
    variantParam,
  }: {
    itemId: string;
    variantParam: string | null;
  }) {
    const [instance] = useState(() => ++mockMounts);
    useEffect(
      () => () => {
        mockUnmounts += 1;
      },
      [],
    );
    return (
      <div data-testid="editor-instance">
        {`${itemId}:${variantParam}:${instance}`}
      </div>
    );
  },
);

import EditPage from "@/app/plan/items/[id]/edit/page";

describe("plan-item editor route", () => {
  beforeEach(() => {
    mockItemId = "item-1";
    mockVariantId = "song_text";
    mockMounts = 0;
    mockUnmounts = 0;
  });

  it("remounts the editor shell when client navigation changes item or variant", () => {
    const view = render(<EditPage />);
    expect(screen.getByTestId("editor-instance")).toHaveTextContent(
      "item-1:song_text:1",
    );

    mockVariantId = "original_text";
    view.rerender(<EditPage />);
    expect(screen.getByTestId("editor-instance")).toHaveTextContent(
      "item-1:original_text:2",
    );
    expect(mockUnmounts).toBe(1);

    mockItemId = "item-2";
    view.rerender(<EditPage />);

    expect(screen.getByTestId("editor-instance")).toHaveTextContent(
      "item-2:original_text:3",
    );
    expect(mockUnmounts).toBe(2);
  });
});
