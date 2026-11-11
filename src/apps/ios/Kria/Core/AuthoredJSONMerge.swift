import Foundation
import KriaMediaEngine

/// Three-way authored-value merge. Missing wire identities stay ambiguous rather than becoming array indexes.
enum AuthoredJSONMerge {
    enum Preference: Equatable { case local, remote }
    struct Conflict: Equatable {
        let path: [String]
        let base: JSONValue?
        let local: JSONValue?
        let remote: JSONValue?
    }
    struct Result {
        let value: JSONValue?
        let conflicts: [Conflict]
    }

    static func merge(
        base: JSONValue?, local: JSONValue?, remote: JSONValue?,
        preference: Preference? = nil, atomicPaths: Set<[String]> = []
    ) -> Result {
        value(base: base, local: local, remote: remote, path: [], preference: preference, atomicPaths: atomicPaths)
    }

    private static func conflict(
        base: JSONValue?, local: JSONValue?, remote: JSONValue?, path: [String], preference: Preference?
    ) -> Result {
        if let preference { return Result(value: preference == .local ? local : remote, conflicts: []) }
        return Result(value: local, conflicts: [Conflict(path: path, base: base, local: local, remote: remote)])
    }

    private static func value(
        base: JSONValue?, local: JSONValue?, remote: JSONValue?, path: [String],
        preference: Preference?, atomicPaths: Set<[String]>
    ) -> Result {
        if local == base { return Result(value: remote, conflicts: []) }
        if remote == base || local == remote { return Result(value: local, conflicts: []) }
        guard path.count < 64, !atomicPaths.contains(path) else {
            return conflict(base: base, local: local, remote: remote, path: path, preference: preference)
        }
        if case .object(let left)? = local, case .object(let right)? = remote {
            let original: [String: JSONValue]
            if case .object(let fields)? = base { original = fields }
            else if base == nil { original = [:] }
            else { return conflict(base: base, local: local, remote: remote, path: path, preference: preference) }
            var fields: [String: JSONValue] = [:]
            var conflicts: [Conflict] = []
            for key in Set(original.keys).union(left.keys).union(right.keys).sorted() {
                let merged = value(base: original[key], local: left[key], remote: right[key], path: path + [key], preference: preference, atomicPaths: atomicPaths)
                fields[key] = merged.value
                conflicts += merged.conflicts
            }
            return Result(value: .object(fields), conflicts: conflicts)
        }
        if case .array(let original)? = base, case .array(let left)? = local, case .array(let right)? = remote,
           let key = identityKey([original, left, right]) {
            return entities(base: original, local: left, remote: right, key: key, path: path, preference: preference, atomicPaths: atomicPaths)
        }
        return conflict(base: base, local: local, remote: remote, path: path, preference: preference)
    }

    private static func identityKey(_ arrays: [[JSONValue]]) -> String? {
        for key in ["id", "slot_id"] {
            let valid = arrays.allSatisfy { rows in
                let ids = rows.compactMap { row -> String? in
                    guard case .object(let fields) = row, case .string(let id)? = fields[key], !id.isEmpty else { return nil }
                    return id
                }
                return ids.count == rows.count && Set(ids).count == ids.count
            }
            if valid { return key }
        }
        return nil
    }

    private static func identifiers(_ rows: [JSONValue], key: String) -> [String] {
        rows.compactMap { $0.objectValue?[key]?.stringValue }
    }

    private static func entities(
        base: [JSONValue], local: [JSONValue], remote: [JSONValue], key: String, path: [String],
        preference: Preference?, atomicPaths: Set<[String]>
    ) -> Result {
        let oldIDs = identifiers(base, key: key), leftIDs = identifiers(local, key: key), rightIDs = identifiers(remote, key: key)
        let old = Dictionary(uniqueKeysWithValues: zip(oldIDs, base))
        let left = Dictionary(uniqueKeysWithValues: zip(leftIDs, local))
        let right = Dictionary(uniqueKeysWithValues: zip(rightIDs, remote))
        var merged: [String: JSONValue] = [:]
        var conflicts: [Conflict] = []
        for id in Set(oldIDs).union(leftIDs).union(rightIDs).sorted() {
            let itemPath = path + ["\(key)=\(id)"]
            let result: Result
            if old[id] == nil, let l = left[id], let r = right[id], l != r {
                result = conflict(base: nil, local: l, remote: r, path: itemPath, preference: preference)
            } else {
                result = value(base: old[id], local: left[id], remote: right[id], path: itemPath, preference: preference, atomicPaths: atomicPaths)
            }
            merged[id] = result.value
            conflicts += result.conflicts
        }
        let kept = Set(merged.keys)
        let common = Set(oldIDs).intersection(leftIDs).intersection(rightIDs).intersection(kept)
        let oldOrder = oldIDs.filter { common.contains($0) }
        let leftOrder = leftIDs.filter { common.contains($0) }
        let rightOrder = rightIDs.filter { common.contains($0) }
        let incompatible = leftOrder != oldOrder && rightOrder != oldOrder && leftOrder != rightOrder
        let backbone = incompatible && preference == .remote ? rightOrder : (leftOrder != oldOrder ? leftOrder : rightOrder)
        let order = incompatible && preference == nil ? nil : ordered(backbone: backbone, common: common, local: leftIDs, remote: rightIDs, kept: kept)
        if let order { return Result(value: .array(order.compactMap { merged[$0] }), conflicts: conflicts) }

        let selection = conflict(
            base: .array(oldIDs.map(JSONValue.string)), local: .array(leftIDs.map(JSONValue.string)),
            remote: .array(rightIDs.map(JSONValue.string)), path: path + ["$order"], preference: preference
        )
        conflicts += selection.conflicts
        if let preference {
            let preferred = preference == .remote ? rightIDs : leftIDs
            let other = preference == .remote ? leftIDs : rightIDs
            let resolved = ordered(backbone: preferred.filter { kept.contains($0) }, common: common,
                                   local: preferred, remote: other, kept: kept, acceptCompatibleEdges: true)
            if let resolved { return Result(value: .array(resolved.compactMap { merged[$0] }), conflicts: conflicts) }
        }
        let preferred = leftIDs + rightIDs
        var seen = Set<String>()
        let fallback = preferred.filter { kept.contains($0) && seen.insert($0).inserted }
        return Result(value: .array(fallback.compactMap { merged[$0] }), conflicts: conflicts)
    }

    private static func ordered(backbone: [String], common: Set<String>, local: [String], remote: [String], kept: Set<String>, acceptCompatibleEdges: Bool = false) -> [String]? {
        var edges = Dictionary(uniqueKeysWithValues: kept.map { ($0, Set<String>()) })
        func reaches(_ start: String, _ target: String) -> Bool {
            var pending = [start], seen = Set<String>()
            while let node = pending.popLast() {
                if node == target { return true }
                if seen.insert(node).inserted { pending.append(contentsOf: edges[node, default: []]) }
            }
            return false
        }
        func connect(_ values: [String], insertionsOnly: Bool) {
            guard values.count > 1 else { return }
            for (a, b) in zip(values, values.dropFirst()) where !insertionsOnly || !common.contains(a) || !common.contains(b) {
                if acceptCompatibleEdges && reaches(b, a) { continue }
                edges[a, default: []].insert(b)
            }
        }
        connect(backbone, insertionsOnly: false)
        connect(local.filter { kept.contains($0) }, insertionsOnly: true)
        connect(remote.filter { kept.contains($0) }, insertionsOnly: true)
        var priority: [String: Int] = [:]
        for id in local + remote where priority[id] == nil { priority[id] = priority.count }
        var remaining = kept
        var result: [String] = []
        while !remaining.isEmpty {
            let incoming = remaining.reduce(into: Set<String>()) { $0.formUnion(edges[$1, default: []].intersection(remaining)) }
            guard let next = remaining.subtracting(incoming).min(by: { (priority[$0] ?? Int.max, $0) < (priority[$1] ?? Int.max, $1) }) else { return nil }
            result.append(next)
            remaining.remove(next)
        }
        return result
    }
}
