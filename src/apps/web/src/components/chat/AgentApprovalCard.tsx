"use client";

import { forwardRef, type HTMLAttributes, type ReactNode } from "react";

import { Card } from "@/components/ui/card";
import { cn } from "@/lib/cn";

export interface AgentApprovalCardProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  icon?: ReactNode;
  badge?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  density?: "default" | "compact";
}

/**
 * Explicit-decision shell adapted from AICSS Approval Card.
 * Source: https://www.aicss.dev/components/approval-card (MIT, AICSS 2026).
 * It is intentionally timer-free: Nova never approves or renders automatically.
 */
export const AgentApprovalCard = forwardRef<HTMLDivElement, AgentApprovalCardProps>(
  function AgentApprovalCard(
    {
      icon,
      badge,
      title,
      description,
      actions,
      density = "default",
      className,
      children,
      ...props
    },
    ref,
  ) {
    const compact = density === "compact";

    return (
      <Card
        ref={ref}
        className={cn(
          "border-border bg-background shadow-[0_1px_3px_rgba(12,12,14,0.05)]",
          compact ? "rounded-xl p-3" : "rounded-2xl p-4",
          className,
        )}
        {...props}
      >
        <div className="flex items-start gap-3">
          {icon ? (
            <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-muted text-foreground">
              {icon}
            </div>
          ) : null}
          <div className="min-w-0 flex-1">
            {badge ? <div className="mb-2">{badge}</div> : null}
            <div className={cn("font-semibold text-foreground", compact ? "text-sm" : "text-base")}>{title}</div>
            {description ? <div className="mt-1 text-sm leading-5 text-muted-foreground">{description}</div> : null}
          </div>
        </div>
        {children ? <div className={cn(compact ? "mt-3" : "mt-4")}>{children}</div> : null}
        {actions ? <div className={cn("flex flex-wrap gap-2", compact ? "mt-3" : "mt-4")}>{actions}</div> : null}
      </Card>
    );
  },
);

AgentApprovalCard.displayName = "AgentApprovalCard";
