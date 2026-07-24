// @ts-nocheck
import React from "react";
import { readFileSync } from "fs";
import { join } from "path";
import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom";

import { BeamLoader } from "@/components/progress/BeamLoader";

describe("BeamLoader", () => {
  it("test_beam_loader_renders_children: content stays visible inside decorative wrapper", () => {
    render(
      <BeamLoader>
        <span>Rendering preview</span>
      </BeamLoader>,
    );

    expect(screen.getByText("Rendering preview")).toBeInTheDocument();
  });

  it("test_beam_loader_decorative_layers_hidden: beam layers are ignored by screen readers", () => {
    const { container } = render(
      <BeamLoader>
        <span>Thinking</span>
      </BeamLoader>,
    );

    const decorative = container.querySelectorAll(
      ".beam-loader__bloom, .beam-loader__beam, .beam-loader__line",
    );
    expect(decorative).toHaveLength(3);
    decorative.forEach((layer) => {
      expect(layer).toHaveAttribute("aria-hidden", "true");
    });
  });

  it("test_beam_loader_tone_and_mode_attrs: exposes CSS data attributes for variants", () => {
    const { container } = render(
      <BeamLoader tone="light" mode="line" strength="medium" active={false}>
        <span>Loading</span>
      </BeamLoader>,
    );

    const wrapper = container.querySelector(".beam-loader");
    expect(wrapper).toHaveAttribute("data-tone", "light");
    expect(wrapper).toHaveAttribute("data-mode", "line");
    expect(wrapper).toHaveAttribute("data-strength", "medium");
    expect(wrapper).toHaveAttribute("data-active", "false");
  });

  it("test_beam_loader_status_label: optional aria label makes the wrapper a live status", () => {
    render(
      <BeamLoader ariaLabel="Kria is thinking">
        <span>Thinking</span>
      </BeamLoader>,
    );

    const status = screen.getByRole("status", { name: "Kria is thinking" });
    expect(status).toHaveAttribute("aria-live", "polite");
  });

  it("test_beam_loader_css_reduced_motion: global CSS disables beam animation", () => {
    const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(css).toContain(".beam-loader__beam");
    expect(css).toContain("animation: none !important");
  });

  it("test_beam_loader_css_uses_perimeter_mask_not_blob: glow stays on the border", () => {
    const css = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");
    expect(css).toContain("conic-gradient");
    expect(css).toContain("@property --beam-angle");
    expect(css).toContain("from var(--beam-angle)");
    expect(css).toContain("height: 100%");
    expect(css).toContain("-webkit-mask-composite: xor");
    expect(css).not.toContain("transform: rotate(1turn)");
    expect(css).not.toContain("radial-gradient(circle at 50%");
  });
});
