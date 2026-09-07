"use client";

import {
  forwardRef,
  type ChangeEvent,
  type KeyboardEvent,
  type Ref,
  type ReactNode,
} from "react";
import { ArrowUp } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/cn";

export interface AgentComposerProps {
  value: string;
  onValueChange: (value: string) => void;
  onSubmit: () => void;
  disabled?: boolean;
  submitDisabled?: boolean;
  multiline?: boolean;
  maxLength?: number;
  placeholder?: string;
  inputLabel: string;
  submitLabel?: string;
  leadingAction?: ReactNode;
  status?: ReactNode;
  rows?: number;
  className?: string;
  inputClassName?: string;
}

/**
 * Controlled creator-agent input adapted from AICSS AI Agent Input.
 * Source: https://www.aicss.dev/components/ai-agent-input (MIT, AICSS 2026).
 * Nova owns submission, queueing, uploads, and busy-state semantics.
 */
export const AgentComposer = forwardRef<
  HTMLInputElement | HTMLTextAreaElement,
  AgentComposerProps
>(function AgentComposer(
  {
    value,
    onValueChange,
    onSubmit,
    disabled = false,
    submitDisabled = false,
    multiline = true,
    maxLength,
    placeholder,
    inputLabel,
    submitLabel = "Send message",
    leadingAction,
    status,
    rows = 1,
    className,
    inputClassName,
  },
  ref,
) {
  const blocked = disabled || submitDisabled || value.trim().length === 0;

  function submit() {
    if (!blocked) onSubmit();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return;
    if (event.nativeEvent.isComposing || event.keyCode === 229) {
      return;
    }
    event.preventDefault();
    submit();
  }

  const sharedProps = {
    value,
    maxLength,
    placeholder,
    disabled,
    "aria-label": inputLabel,
    onChange: (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
      onValueChange(event.target.value),
    onKeyDown: handleKeyDown,
  };

  return (
    <form
      className={cn(
        "rounded-2xl border border-border bg-background p-2 shadow-[0_1px_3px_rgba(12,12,14,0.06)] focus-within:ring-1 focus-within:ring-ring",
        className,
      )}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="flex items-end gap-2">
        {leadingAction}
        {multiline ? (
          <Textarea
            {...sharedProps}
            ref={ref as Ref<HTMLTextAreaElement>}
            rows={rows}
            className={cn(
              "max-h-32 min-h-11 flex-1 resize-none border-0 bg-transparent py-3 shadow-none focus-visible:ring-0",
              inputClassName,
            )}
          />
        ) : (
          <Input
            {...sharedProps}
            ref={ref as Ref<HTMLInputElement>}
            type="text"
            className={cn(
              "h-11 flex-1 border-0 bg-transparent shadow-none focus-visible:ring-0 sm:h-11",
              inputClassName,
            )}
          />
        )}
        <Button
          type="submit"
          size="icon"
          disabled={blocked}
          aria-label={submitLabel}
          className="size-11 shrink-0 rounded-full bg-foreground text-background hover:bg-foreground/90"
        >
          <ArrowUp className="size-4" aria-hidden="true" />
        </Button>
      </div>
      {status ? <div className="px-1 pt-2">{status}</div> : null}
    </form>
  );
});

AgentComposer.displayName = "AgentComposer";
