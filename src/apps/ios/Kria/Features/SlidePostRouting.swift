import Foundation
import SwiftUI

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

    /// Which full screen a project shows. A slide post is ALWAYS the slide workspace (never the video
    /// editor, never the generic chat); a started project whose format is not yet known shows a neutral
    /// loading screen instead of flashing generic chat chrome or a video entry; only a project that is
    /// definitely not a slide post (or a brand-new draft) shows the generic chat.
    enum Screen: Equatable {
        case slideWorkspace
        case resolvingFormat
        case genericChat
    }

    static func screen(
        selectedFormat: CreationFormat?,
        project: ProjectSummary,
        thread: CreationThread?,
        isChoosingFormat: Bool
    ) -> Screen {
        if !isChoosingFormat, isSlidePost(selectedFormat: selectedFormat, project: project, thread: thread) { return .slideWorkspace }
        if project.status != .draft, !isFormatKnown(selectedFormat: selectedFormat, project: project, thread: thread) { return .resolvingFormat }
        return .genericChat
    }
}

/// Shown while a started project's format is still unknown. It has a way out (the projects drawer) and
/// a retry once loading gives up, but no editor, chat or format chrome.
struct SlidePostResolvingView: View {
    let title: String
    let failed: Bool
    let openProjects: () -> Void
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            WorkspaceTopRow(title: title) {
                Button(action: openProjects) { KriaIcon(.menu).frame(width: 44, height: 44).kriaFloatingSurface(Circle()) }
                    .accessibilityLabel("Open projects")
            } trailing: {
                Color.clear.frame(width: 44, height: 44).accessibilityHidden(true)
            }
            .buttonStyle(.plain).foregroundStyle(KriaColor.ink)
            Spacer()
            if failed {
                VStack(spacing: 12) {
                    Text("Couldn’t open this project.").font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.zinc)
                    Button("Try again", action: retry).buttonStyle(CanonicalSecondaryButtonStyle()).frame(width: 160)
                }
            } else {
                ProgressView("Opening your project…")
            }
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityIdentifier("format-resolving")
    }
}
