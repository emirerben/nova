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

    static func editorDestination(isSlidePost: Bool) -> EditorDestination {
        isSlidePost ? .slideWorkspace : .videoEditor
    }
}
