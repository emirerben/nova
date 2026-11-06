import Foundation
import KriaMediaEngine

/// Server metadata always comes from the current server draft; authored values merge by stable identity.
enum SlidePostMerge {
    struct Result {
        let draft: SlidePostDraft
        let conflicts: [AuthoredJSONMerge.Conflict]
    }
    enum Failure: Error { case malformedDraft, invalidMergedDraft(String) }

    static func merge(base: SlidePostDraft, local: SlidePostDraft, remote: SlidePostDraft,
                      preference: AuthoredJSONMerge.Preference? = nil, forceCanvasConflict: Bool = false) throws -> Result {
        for draft in [base, local, remote] {
            guard Set(draft.slides.map(\.id)).count == draft.slides.count,
                  draft.slides.allSatisfy({ !$0.id.isEmpty }) else { throw Failure.malformedDraft }
        }
        let original = Dictionary(uniqueKeysWithValues: base.slides.map { ($0.id, $0) })
        let left = Dictionary(uniqueKeysWithValues: local.slides.map { ($0.id, $0) })
        let right = Dictionary(uniqueKeysWithValues: remote.slides.map { ($0.id, $0) })
        var atomic: Set<[String]> = []
        if forceCanvasConflict || local.platformProfile != base.platformProfile || local.aspect != base.aspect ||
            remote.platformProfile != base.platformProfile || remote.aspect != base.aspect {
            atomic.insert(["$canvas"])
        }
        for id in Set(original.keys).union(left.keys).union(right.keys) {
            atomic.insert(["$canvas", "slides", "id=\(id)", "edits", "$geometry"])
        }
        for id in Set(original.keys).intersection(left.keys).intersection(right.keys) {
            guard let b = original[id], let l = left[id], let r = right[id] else { continue }
            let localSourceChanged = l.assetID != b.assetID || l.kind != b.kind
            let remoteSourceChanged = r.assetID != b.assetID || r.kind != b.kind
            if (localSourceChanged && r != b) || (remoteSourceChanged && l != b) {
                atomic.insert(["$canvas", "slides", "id=\(id)"])
            }
            if (b.edits?.texts != nil) != (l.edits?.texts != nil) || (b.edits?.texts != nil) != (r.edits?.texts != nil) {
                atomic.insert(["$canvas", "slides", "id=\(id)", "edits"])
            }
        }
        let merged = AuthoredJSONMerge.merge(base: try content(base), local: try content(local), remote: try content(remote),
                                             preference: preference, atomicPaths: atomic)
        guard case .object(var fields)? = merged.value,
              case .object(let canvas)? = fields.removeValue(forKey: "$canvas") else { throw Failure.malformedDraft }
        for (key, value) in canvas { fields[key] = value }
        guard
              case .array(let rows)? = fields["slides"],
              case .string(let coverID)? = fields.removeValue(forKey: "cover_id") else { throw Failure.malformedDraft }
        var restored: [JSONValue] = []
        for row in rows {
            guard case .object(var slide) = row else { throw Failure.malformedDraft }
            if case .object(var edits)? = slide["edits"] {
                if case .object(let geometry)? = edits.removeValue(forKey: "$geometry") {
                    for (key, value) in geometry { edits[key] = value }
                }
                slide["edits"] = .object(edits)
            }
            restored.append(.object(slide))
        }
        fields["slides"] = .array(restored)
        guard let coverIndex = restored.firstIndex(where: { $0.objectValue?["id"]?.stringValue == coverID }) else {
            if !atomic.contains(["$canvas"]) {
                return try merge(base: base, local: local, remote: remote, preference: preference, forceCanvasConflict: true)
            }
            throw Failure.invalidMergedDraft("The selected cover was removed. Choose a cover before saving.")
        }
        fields["cover_index"] = .number(Double(coverIndex))
        guard case .object(var remoteFields) = try json(remote) else { throw Failure.malformedDraft }
        for (key, value) in fields { remoteFields[key] = value }
        var draft = try JSONDecoder().decode(SlidePostDraft.self, from: JSONEncoder().encode(JSONValue.object(remoteFields)))
        for index in draft.slides.indices {
            if let texts = draft.slides[index].edits?.texts { draft.slides[index].edits?.setTexts(texts) }
            if draft.slides[index].edits?.stickers == nil, draft.slides[index].edits == SlidePostEdits() { draft.slides[index].edits = nil }
        }
        if merged.conflicts.isEmpty, let message = draft.validationMessage {
            if !atomic.contains(["$canvas"]) {
                return try merge(base: base, local: local, remote: remote, preference: preference, forceCanvasConflict: true)
            }
            throw Failure.invalidMergedDraft(message)
        }
        return Result(draft: draft, conflicts: merged.conflicts)
    }

    private static func json(_ draft: SlidePostDraft) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(draft))
    }

    private static func content(_ draft: SlidePostDraft) throws -> JSONValue {
        guard draft.slides.indices.contains(draft.coverIndex), case .object(var fields) = try json(draft) else {
            throw Failure.malformedDraft
        }
        for key in ["schema_version", "version", "rendered_version", "user_edited", "brief_binding", "cover_index"] {
            fields.removeValue(forKey: key)
        }
        fields["cover_id"] = .string(draft.slides[draft.coverIndex].id)
        fields["slides"] = .array(try draft.slides.map { slide in
            guard case .object(var row) = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(slide)) else {
                throw Failure.malformedDraft
            }
            let edits = slide.edits ?? SlidePostEdits()
            // Default edits share a representation; explicit empty sticker arrays still mean clear.
            guard case .object(var values) = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(edits)) else {
                throw Failure.malformedDraft
            }
            if edits.texts != nil { values.removeValue(forKey: "text") }
            var geometry: [String: JSONValue] = [:]
            for key in ["framing", "fit", "tilt"] { geometry[key] = values.removeValue(forKey: key) ?? .null }
            values["$geometry"] = .object(geometry)
            row["edits"] = .object(values)
            return .object(row)
        })
        var canvas: [String: JSONValue] = [:]
        for key in ["platform_profile", "aspect", "slides", "cover_id"] { canvas[key] = fields.removeValue(forKey: key) }
        fields["$canvas"] = .object(canvas)
        return .object(fields)
    }
}
