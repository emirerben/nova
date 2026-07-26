/** @jest-environment node */

import { readFileSync } from "node:fs";
import path from "node:path";

describe("metadata route owners", () => {
  const owners = {
    "src/app/page.tsx": "landing",
    "src/app/generative/layout.tsx": "generative",
    "src/app/plan/layout.tsx": "plan",
    "src/app/plan/persona/layout.tsx": "persona",
    "src/app/plan/style/page.tsx": "style",
    "src/app/plan/items/[id]/layout.tsx": "planItem",
    "src/app/plan/items/[id]/edit/layout.tsx": "editor",
    "src/app/plan/items/[id]/transcript/layout.tsx": "transcript",
    "src/app/library/layout.tsx": "library",
    "src/app/template-jobs/layout.tsx": "renderStatus",
  } as const;

  it.each(Object.entries(owners))("%s owns ROUTE_METADATA.%s", (file, owner) => {
    const source = readFileSync(path.join(process.cwd(), file), "utf8");
    expect(source).toContain(`ROUTE_METADATA.${owner}`);
  });

  it("keeps public route metadata out of the global layout", () => {
    const source = readFileSync(
      path.join(process.cwd(), "src/app/layout.tsx"),
      "utf8",
    );

    expect(source).not.toContain("ROUTE_METADATA");
    expect(source).not.toContain("alternates:");
    expect(source).not.toContain("openGraph:");
    expect(source).not.toContain("twitter:");
    expect(source).not.toContain("robots:");
  });
});
