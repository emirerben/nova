import validFixture from "../../../../api/tests/fixtures/copilot-ops/valid.json";
import invalidFixture from "../../../../api/tests/fixtures/copilot-ops/invalid.json";
import { validateCopilotOp, type CopilotValidationSnapshot } from "@/lib/edit-copilot/ops";

const validationSnapshot: CopilotValidationSnapshot = {
  text_bars: [{ id: "bar-0" }, { id: "bar-1" }],
  slots: [
    { output_start_s: 0, output_end_s: 3 },
    { output_start_s: 3, output_end_s: 6 },
    { output_start_s: 6, output_end_s: 8 },
  ],
};

describe("edit-copilot op contract fixtures", () => {
  it("accepts every shared valid op fixture", () => {
    for (const testCase of validFixture.cases) {
      expect(validateCopilotOp(testCase.op, validationSnapshot)).toMatchObject({
        ok: true,
      });
    }
  });

  it("rejects every shared invalid op fixture", () => {
    for (const testCase of invalidFixture.cases) {
      expect(validateCopilotOp(testCase.op, validationSnapshot)).toMatchObject({
        ok: false,
      });
    }
  });
});
