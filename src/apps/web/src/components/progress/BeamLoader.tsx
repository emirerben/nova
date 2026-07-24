"use client";

import { type ReactNode } from "react";

type BeamTone = "dark" | "light";
type BeamMode = "frame" | "line" | "pulse";
type BeamStrength = "subtle" | "medium";

interface BeamLoaderProps {
  tone?: BeamTone;
  mode?: BeamMode;
  active?: boolean;
  strength?: BeamStrength;
  ariaLabel?: string;
  className?: string;
  children: ReactNode;
}

/**
 * Decorative Beam-style loading wrapper for long-running video and AI states.
 * The child owns layout; this wrapper only paints non-interactive motion layers.
 */
export function BeamLoader({
  tone = "dark",
  mode = "frame",
  active = true,
  strength = "subtle",
  ariaLabel,
  className = "",
  children,
}: BeamLoaderProps) {
  const statusProps = ariaLabel
    ? {
        role: "status",
        "aria-live": "polite" as const,
        "aria-label": ariaLabel,
      }
    : {};

  return (
    <div
      className={["beam-loader", className].filter(Boolean).join(" ")}
      data-tone={tone}
      data-mode={mode}
      data-active={active ? "true" : "false"}
      data-strength={strength}
      {...statusProps}
    >
      <span className="beam-loader__bloom" aria-hidden="true" />
      <span className="beam-loader__beam" aria-hidden="true" />
      <span className="beam-loader__line" aria-hidden="true" />
      <div className="beam-loader__content">{children}</div>
    </div>
  );
}
