import Foundation

/// Signed URLs are deliberately excluded: signatures rotate while the source
/// video and its recovery budget stay the same. Legacy servers omit identity.
struct LibraryPosterKey: Hashable {
    let jobID: UUID
    let identity: String?
    let variantID: String?

    init(_ project: ProjectSummary) {
        jobID = project.id
        identity = project.posterIdentity
        variantID = project.outputVariantID
    }
}
