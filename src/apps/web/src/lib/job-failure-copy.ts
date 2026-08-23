export interface JobFailureMessage {
  title: string;
  detail: string;
  action: "review_media" | "retry_upload" | "retry_render" | "contact_support";
  actionLabel: string;
}

/**
 * One user-safe failure taxonomy for every creator-facing job surface.
 *
 * Backend values come from multiple pipelines and older rows, so matching is
 * deliberately category-based. Raw worker details and enum strings must never
 * be rendered as user copy.
 */
export function jobFailureCopy(reason?: string | null): JobFailureMessage {
  const key = (reason ?? "").trim().toLowerCase();

  if (
    key.includes("storage") ||
    key.includes("upload") ||
    key.includes("download") ||
    key.includes("network") ||
    key.includes("fetch")
  ) {
    return {
      title: "We couldn’t move your files",
      detail: "Your files are still here. Retry the transfer without uploading them again.",
      action: "retry_upload",
      actionLabel: "Retry transfer",
    };
  }

  if (
    key.includes("invalid_media") ||
    key.includes("clip_read_error") ||
    key.includes("unsupported_media") ||
    key.includes("user_clip_unusable") ||
    key.includes("unusable_footage") ||
    key.includes("photo")
  ) {
    return {
      title: "Review your footage",
      detail: "One or more files couldn’t be used. Review the named upload, replace it, then generate again.",
      action: "review_media",
      actionLabel: "Review footage",
    };
  }

  if (key.includes("timeout") || key.includes("timed_out") || key.includes("timed out")) {
    return {
      title: "The render took too long",
      detail: "Your footage and direction are saved. Retry the render.",
      action: "retry_render",
      actionLabel: "Retry render",
    };
  }

  if (
    key.includes("encoder") ||
    key.includes("ffmpeg") ||
    key.includes("render") ||
    key.includes("processing_failed")
  ) {
    return {
      title: "The render didn’t finish",
      detail: "Your setup is saved. Retry the render without uploading again.",
      action: "retry_render",
      actionLabel: "Retry render",
    };
  }

  return {
    title: "Kria couldn’t finish this video",
    detail: "Try again. If it keeps happening, send the support reference below so we can trace it.",
    action: "contact_support",
    actionLabel: "Retry video",
  };
}
