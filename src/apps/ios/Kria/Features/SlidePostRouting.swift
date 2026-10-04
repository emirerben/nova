import Foundation

/// The single decision for "is this project a slide post?" and where an
/// "open editor" action lands. A slide post must never reach `NativeEditorView`
/// (the video timeline); every signal that identifies one is honoured here so
/// no path depends on a single, momentarily-nil field (selected format, the
/// thread's plan-item id, or the gallery row's variant id).
enum SlidePostRouting {
    enum EditorDestination: Equatable {
        case slideWorkspace
        case videoEditor
    }

    static func isSlidePost(
        selectedFormat: CreationFormat?,
        project: ProjectSummary,
        thread: CreationThread?
    ) -> Bool {
        if selectedFormat == .slides || project.isSlidePost { return true }
        guard let thread else { return false }
        if CreationFormat(thread: thread) == .slides { return true }
        if thread.summary.isSlidePost { return true }
        return thread.job?.variants.contains { $0.variantID == "slides" } == true
    }

    /// True once ANY signal says what kind of project this is. A ready project that
    /// has no signal yet (cold drawer open before the thread loaded, stale cache row)
    /// must not expose a video-editor entry: it could be a slide post.
    static func isFormatKnown(
        selectedFormat: CreationFormat?,
        project: ProjectSummary,
        thread: CreationThread?
    ) -> Bool {
        selectedFormat != nil || thread != nil
            || project.editFormat != nil || project.outputVariantID != nil
    }

    /// The only gate for the video editor (Chat|Editor switch, "Open editor",
    /// "Open current cut"): known format AND not a slide post.
    static func canOpenVideoEditor(
        selectedFormat: CreationFormat?,
        project: ProjectSummary,
        thread: CreationThread?
    ) -> Bool {
        isFormatKnown(selectedFormat: selectedFormat, project: project, thread: thread)
            && !isSlidePost(selectedFormat: selectedFormat, project: project, thread: thread)
    }

    static func editorDestination(isSlidePost: Bool) -> EditorDestination {
        isSlidePost ? .slideWorkspace : .videoEditor
    }
}
