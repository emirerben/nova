import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import DirectorSuggestions, {
  directorWillChange,
} from "@/app/plan/items/[id]/_editor/DirectorSuggestions";
import type { DirectorAppliedReceipt } from "@/lib/edit-copilot/useEditDirector";
import type { EditorSuggestion } from "@/lib/plan-api";

describe("DirectorSuggestions applied receipts", () => {
  it("disables review while a non-retryable budget breaker is active", () => {
    render(
      <DirectorSuggestions
        suggestions={[]}
        appliedReceipts={[]}
        historyVersion={0}
        reviewed
        loading={false}
        reviewBlocked
        error="Reviews become available tomorrow."
        modelUsed=""
        generation={null}
        onAccept={jest.fn()}
        onDismiss={jest.fn()}
        onRefresh={jest.fn()}
        onRevealApplied={jest.fn()}
        onCancelGeneration={jest.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: "Review limit reached" })).toBeDisabled();
  });

  it("describes Stadium Diffusion as a clip-look change", () => {
    expect(directorWillChange({
      id: "look-1",
      category: "effect",
      title: "Give the action clip a stadium look",
      rationale: "The highlight bloom suits this celebration.",
      expected_benefit: "A stronger cinematic finish.",
      confidence: 0.9,
      start_s: 0,
      end_s: 3,
      apply_mode: "instant",
      ops: [{ op: "set_look_preset", slot_index: 0, look_preset: "stadium_diffusion" }],
    })).toEqual(["Clip look"]);
  });

  it("shows an honest empty review and operation-derived change summary", () => {
    const props = {
      appliedReceipts: [] as DirectorAppliedReceipt[],
      historyVersion: 0,
      reviewed: true,
      loading: false,
      error: null,
      modelUsed: "gemini-3.1-pro-preview",
      generation: null,
      onAccept: jest.fn(),
      onDismiss: jest.fn(),
      onRefresh: jest.fn(),
      onRevealApplied: jest.fn(),
      onCancelGeneration: jest.fn(),
    };
    const { rerender } = render(<DirectorSuggestions suggestions={[]} {...props} />);
    expect(screen.getByText("No changes recommended")).toBeInTheDocument();

    rerender(
      <DirectorSuggestions
        suggestions={[{
          id: "cut-1",
          category: "hook_pacing",
          title: "Tighten the pause",
          rationale: "The opening pause slows the hook.",
          expected_benefit: "A faster opening.",
          confidence: 0.9,
          start_s: 1,
          end_s: 1.6,
          apply_mode: "server_async",
          ops: [{ op: "apply_speech_cut_candidate", candidate_id: "candidate-1" }],
        }]}
        {...props}
      />,
    );
    expect(screen.getByText("Will change")).toBeInTheDocument();
    expect(screen.getByText(
      "Remove the reviewed speech span and retime captions, text, and effects",
    )).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply & rebuild" })).toBeInTheDocument();
  });

  it("forwards Accept and Dismiss only from explicit suggestion actions", () => {
    const suggestion: EditorSuggestion = {
      id: "cut-1",
      category: "hook_pacing",
      title: "Tighten the pause",
      rationale: "The opening pause slows the hook.",
      expected_benefit: "A faster opening.",
      confidence: 0.9,
      start_s: 1,
      end_s: 1.6,
      apply_mode: "instant",
      ops: [{ op: "set_title", title: "Faster" }],
    };
    const onAccept = jest.fn();
    const onDismiss = jest.fn();

    render(
      <DirectorSuggestions
        suggestions={[suggestion]}
        appliedReceipts={[]}
        historyVersion={0}
        reviewed={true}
        loading={false}
        error={null}
        modelUsed="gemini-3.1-pro-preview"
        omniMaxCostPerSecondUsd={0.11}
        generation={null}
        onAccept={onAccept}
        onDismiss={onDismiss}
        onRefresh={jest.fn()}
        onRevealApplied={jest.fn()}
        onCancelGeneration={jest.fn()}
      />,
    );

    expect(onAccept).not.toHaveBeenCalled();
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Accept" }));
    fireEvent.click(screen.getByRole("button", { name: "Dismiss Tighten the pause" }));
    expect(onAccept).toHaveBeenCalledWith(suggestion);
    expect(onDismiss).toHaveBeenCalledWith(suggestion);
  });

  it("requires the accessible cost dialog before starting an Omni generation", () => {
    const suggestion: EditorSuggestion = {
      id: "omni-1",
      category: "effect",
      title: "Generate a visual bridge",
      rationale: "A generated insert connects the two shots.",
      expected_benefit: "Smoother visual continuity.",
      confidence: 0.9,
      start_s: 2,
      end_s: 6,
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A four-second visual bridge",
        insert_at_s: 2,
        duration_s: 4,
      },
    };
    const onAccept = jest.fn();

    render(
      <DirectorSuggestions
        suggestions={[suggestion]}
        appliedReceipts={[]}
        historyVersion={0}
        reviewed={true}
        loading={false}
        error={null}
        modelUsed="gemini-3.1-pro-preview"
        omniMaxCostPerSecondUsd={0.11}
        generation={null}
        onAccept={onAccept}
        onDismiss={jest.fn()}
        onRefresh={jest.fn()}
        onRevealApplied={jest.fn()}
        onCancelGeneration={jest.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Generate & add" }));
    expect(screen.getByRole("alertdialog", {
      name: "Generate this experimental clip?",
    })).toHaveTextContent("Estimated maximum provider cost: $0.44");
    expect(onAccept).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Keep current draft" }));
    expect(onAccept).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Generate & add" }));
    fireEvent.click(screen.getByRole("button", { name: "Generate & add" }));
    expect(onAccept).toHaveBeenCalledWith(suggestion, { omniCostConfirmed: true });
  });

  it("rounds a fractional Omni maximum up to cents", () => {
    const suggestion: EditorSuggestion = {
      id: "omni-fractional",
      category: "effect",
      title: "Generate a fractional bridge",
      rationale: "A generated insert connects the shots.",
      expected_benefit: "Smoother continuity.",
      confidence: 0.9,
      start_s: 2,
      end_s: 5.3,
      apply_mode: "omni_async",
      ops: [],
      omni: {
        action: "generate_insert",
        prompt: "A short visual bridge",
        insert_at_s: 2,
        duration_s: 3.3,
      },
    };

    render(
      <DirectorSuggestions
        suggestions={[suggestion]}
        appliedReceipts={[]}
        historyVersion={0}
        reviewed={true}
        loading={false}
        error={null}
        modelUsed="gemini-3.1-pro-preview"
        omniMaxCostPerSecondUsd={0.11}
        generation={null}
        omniDispatchPending={false}
        onAccept={jest.fn()}
        onDismiss={jest.fn()}
        onRefresh={jest.fn()}
        onRevealApplied={jest.fn()}
        onCancelGeneration={jest.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Generate & add" }));
    expect(screen.getByRole("alertdialog")).toHaveTextContent(
      "Estimated maximum provider cost: $0.37",
    );
  });

  it("keeps every accepted recommendation visible with its exact delta and replay action", () => {
    const receipts: DirectorAppliedReceipt[] = [
      {
        id: "sound-1",
        suggestionId: "sound",
        title: "Punch up the hook text",
        startS: 0,
        endS: 0,
        changes: [{ label: "Sound effect", from: "none", to: "Visual enter accent" }],
        previewFocus: { kind: "sfx", id: "sfx-1", seekS: 0 },
      },
      {
        id: "type-2",
        suggestionId: "type",
        title: "Modernize closing typography",
        startS: 103.8,
        endS: 107.8,
        changes: [{
          label: "Font",
          from: "PlayfairDisplay-Bold",
          to: "Montserrat Bold",
        }],
        previewFocus: { kind: "text", id: "closing", seekS: 105.8 },
      },
    ];
    const onRevealApplied = jest.fn();

    render(
      <DirectorSuggestions
        suggestions={[]}
        appliedReceipts={receipts}
        historyVersion={0}
        reviewed={true}
        loading={false}
        error={null}
        modelUsed="gemini-3.1-pro-preview"
        generation={null}
        onAccept={jest.fn()}
        onDismiss={jest.fn()}
        onRefresh={jest.fn()}
        onRevealApplied={onRevealApplied}
        onCancelGeneration={jest.fn()}
      />,
    );

    expect(screen.getByLabelText("Staged Kria suggestions")).toHaveTextContent(
      "Punch up the hook text",
    );
    expect(screen.getByLabelText("Staged Kria suggestions")).toHaveTextContent(
      "Font: PlayfairDisplay-Bold, now Montserrat Bold",
    );
    expect(screen.getAllByText("Showing this moment in preview.")).toHaveLength(2);

    fireEvent.click(screen.getAllByRole("button", { name: "Show again" })[1]);
    expect(onRevealApplied).toHaveBeenCalledWith(receipts[1]);
  });

  it("marks receipts changed and disables replay after editor history moves on", () => {
    const receipt: DirectorAppliedReceipt = {
      id: "type-2",
      suggestionId: "type",
      title: "Modernize closing typography",
      startS: 103.8,
      endS: 107.8,
      changes: [{ label: "Font", from: "Playfair", to: "Montserrat" }],
      undoVersion: 1,
      previewFocus: { kind: "text", id: "closing", seekS: 105.8 },
    };

    render(
      <DirectorSuggestions
        suggestions={[]}
        appliedReceipts={[receipt]}
        historyVersion={2}
        reviewed={true}
        loading={false}
        error={null}
        modelUsed="gemini-3.1-pro-preview"
        generation={null}
        onAccept={jest.fn()}
        onDismiss={jest.fn()}
        onRefresh={jest.fn()}
        onRevealApplied={jest.fn()}
        onCancelGeneration={jest.fn()}
      />,
    );

    expect(screen.getByText("Changed since")).toBeInTheDocument();
    expect(screen.getByText("The preview has changed since this edit.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show again" })).not.toBeInTheDocument();
  });

  it("brings the next actionable recommendation into view", () => {
    const scrollIntoView = jest.fn();
    Object.defineProperty(Element.prototype, "scrollIntoView", {
      configurable: true,
      value: scrollIntoView,
    });
    const recommendation = (id: string): EditorSuggestion => ({
      id,
      category: "text",
      title: `Recommendation ${id}`,
      rationale: "Improve clarity.",
      expected_benefit: "The change is easier to notice.",
      confidence: 0.9,
      start_s: 0,
      end_s: 1,
      apply_mode: "instant",
      ops: [{ op: "set_title", title: id }],
    });
    const props = {
      appliedReceipts: [] as DirectorAppliedReceipt[],
      historyVersion: 0,
      reviewed: true,
      loading: false,
      error: null,
      modelUsed: "gemini-3.1-pro-preview",
      generation: null,
      onAccept: jest.fn(),
      onDismiss: jest.fn(),
      onRefresh: jest.fn(),
      onRevealApplied: jest.fn(),
      onCancelGeneration: jest.fn(),
    };
    const { rerender } = render(
      <DirectorSuggestions suggestions={[recommendation("one"), recommendation("two")]} {...props} />,
    );
    rerender(<DirectorSuggestions suggestions={[recommendation("two")]} {...props} />);

    expect(scrollIntoView).toHaveBeenCalledTimes(2);
  });
});
