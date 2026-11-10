import Foundation

/// What the chat screen decides about preparing the editor while the creator reads. The value is the identity of the
/// decision: `.task(id:)` restarts the warm-up only when the project, its rendered job or variant, or whether
/// warming is allowed changes, never on a chat poll that changes none of them.
struct EditorWarmPlan: Hashable, Sendable {
    struct Facts: Sendable {
        var videoIsReady: Bool
        var formatIsKnown: Bool
        var isSlidePost: Bool
        var canOpenVideoEditor: Bool
        var sceneIsActive: Bool
        var chatIsIdle: Bool
        var isOnline: Bool
        /// The system's Low Data Mode (`NWPath.isConstrained`). Warming downloads full-resolution sources.
        var isLowDataMode: Bool
    }

    /// How long the chat has to be idle before the warm-up starts, so it does not compete with the chat's own first paint.
    static let settleDelay: Duration = .milliseconds(500)

    let projectID: UUID
    let jobID: UUID?
    let variantID: String?
    let isWarmable: Bool

    init(project: ProjectSummary, facts: Facts) {
        projectID = project.id
        jobID = project.activeJobID
        variantID = project.outputVariantID
        isWarmable = facts.videoIsReady && facts.formatIsKnown && !facts.isSlidePost && facts.canOpenVideoEditor
            && facts.sceneIsActive && facts.chatIsIdle && facts.isOnline && !facts.isLowDataMode
            && project.activeJobID != nil
    }
}
