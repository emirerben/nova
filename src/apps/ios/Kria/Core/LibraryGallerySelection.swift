import Foundation
import Photos

/// Which Photos media the in-app gallery lists.
struct LibraryMediaKinds: OptionSet, Sendable {
    let rawValue: Int
    static let images = LibraryMediaKinds(rawValue: 1)
    static let videos = LibraryMediaKinds(rawValue: 2)

    var mediaTypes: [PHAssetMediaType] {
        var result: [PHAssetMediaType] = []
        if contains(.videos) { result.append(.video) }
        if contains(.images) { result.append(.image) }
        return result
    }
}

/// Ordered, limit-capped selection behind the in-app Photos gallery (the system picker's
/// `.continuousAndOrdered` semantics): pick order is kept and numbered, nothing is added past `limit`,
/// and a limit of 1 swaps rather than refuses. Pure value type so tap and slide-to-select are unit-testable.
struct LibraryGallerySelection: Equatable, Sendable {
    private(set) var ids: [String]
    let limit: Int

    init(ids: [String] = [], limit: Int) {
        self.limit = max(1, limit)
        var seen = Set<String>()
        self.ids = ids.filter { seen.insert($0).inserted }
    }

    func contains(_ id: String) -> Bool { ids.contains(id) }
    /// 1-based pick number.
    func number(of id: String) -> Int? { ids.firstIndex(of: id).map { $0 + 1 } }
    var isFull: Bool { ids.count >= limit }

    enum ToggleResult: Equatable { case added, removed, swapped, refusedAtLimit }

    @discardableResult
    mutating func toggle(_ id: String) -> ToggleResult {
        if let index = ids.firstIndex(of: id) {
            ids.remove(at: index)
            return .removed
        }
        if limit == 1 {
            ids = [id]
            return .swapped
        }
        guard !isFull else { return .refusedAtLimit }
        ids.append(id)
        return .added
    }

    /// Slide-to-select: adds (in touch order, until the limit) or removes exactly `touched`. Callers apply
    /// this to a snapshot taken when the slide began, so reversing the finger un-applies tiles.
    mutating func apply(_ touched: [String], selected: Bool) {
        if selected {
            for id in touched where !ids.contains(id) {
                guard !isFull else { return }
                ids.append(id)
            }
        } else {
            let drop = Set(touched)
            ids.removeAll { drop.contains($0) }
        }
    }
}
