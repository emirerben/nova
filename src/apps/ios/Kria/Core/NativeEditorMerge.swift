import Foundation
import KriaMediaEngine

/// One latest-job check owns a bounded wait window across all of its GETs.
struct NativeEditorReadWindow {
    enum Action: Equatable { case ready, wait(Duration), paused }
    private let deadline: ContinuousClock.Instant

    init(started: ContinuousClock.Instant) {
        deadline = started.advanced(by: .seconds(300))
    }
    func remaining(now: ContinuousClock.Instant) -> Duration {
        max(.zero, now.duration(to: deadline))
    }
    func action(cooldown: NativeEditorReadCooldown, now: ContinuousClock.Instant) -> Action {
        let wait = cooldown.delay(now: now)
        guard !cooldown.isPaused, now <= deadline, wait <= remaining(now: now) else { return .paused }
        return wait > .zero ? .wait(wait) : .ready
    }
}

struct NativeEditorReadCooldown {
    private var deadline: ContinuousClock.Instant?
    private(set) var isPaused = false
    mutating func record(seconds: Double, now: ContinuousClock.Instant) {
        guard seconds.isFinite, seconds > 0 else { return }
        // Do not overflow the clock or retry sooner than an unrepresentable hint.
        guard seconds <= 365 * 24 * 60 * 60 else { isPaused = true; return }
        let next = now.advanced(by: .seconds(seconds))
        deadline = max(next, deadline ?? next)
    }
    mutating func prune(now: ContinuousClock.Instant) {
        if let deadline, deadline <= now { self.deadline = nil }
    }
    func delay(now: ContinuousClock.Instant) -> Duration {
        deadline.map { max(.zero, now.duration(to: $0)) } ?? .zero
    }
}

/// A load resets the lifetime; a later notification replaces the pending ticket.
/// Finishing stale work never consumes the newest request.
struct NativeEditorLatestGate {
    struct Ticket: Equatable {
        let lifetime: UUID
        let request: UUID
        let threadID: UUID
    }
    private var lifetime = UUID()
    var loadLifetime: UUID { lifetime }
    private(set) var pending: Ticket?

    mutating func enqueue(threadID: UUID) -> Ticket {
        let ticket = Ticket(lifetime: lifetime, request: UUID(), threadID: threadID)
        pending = ticket
        return ticket
    }
    func matches(_ ticket: Ticket) -> Bool { pending == ticket && lifetime == ticket.lifetime }
    mutating func finish(_ ticket: Ticket) { if matches(ticket) { pending = nil } }
    mutating func reset() { lifetime = UUID(); pending = nil }
}

/// Wire-value merge only. The session adapter must supply an effective song value
/// containing its source authority; a relative volume/window overlay is insufficient.
enum NativeEditorMerge {
    enum DocumentResult {
        case resolved(EditorDocument)
        case conflicts([AuthoredJSONMerge.Conflict])
        case unsupported(String)
    }

    static func documents(base: EditorDocument, local: EditorDocument, remote: EditorDocument,
                          baseSong: JSONValue?, remoteSong: JSONValue?,
                          preference: AuthoredJSONMerge.Preference? = nil) -> DocumentResult {
        func song(_ document: EditorDocument, authority: JSONValue?) -> JSONValue {
            var edit: [String: JSONValue] = [:]
            if let value = document.userSong {
                edit["volume"] = value.volume.map(JSONValue.number)
                edit["start"] = value.windowStartS.map(JSONValue.number)
                edit["end"] = value.windowEndS.map(JSONValue.number)
                if value.removed { edit["removed"] = .bool(true) }
            }
            return .object(["authority": authority ?? .null, "edit": .object(edit)])
        }
        func state(_ document: EditorDocument, authority: JSONValue?) -> State {
            let raw = document.encodeSnapshot()
            let payload = raw["editor_payload"]?.objectValue
            let deletions = payload?["deletions"] ?? (document.deletions.isEmpty ? nil : .array(document.deletions.map {
                .object(["kind": .string($0.kind), "id": .string($0.id)])
            }))
            return State(sections: payload?["sections"]?.objectValue ?? payload ?? raw,
                         editorState: payload?["editor_state"] ?? (document.editorState == "empty" ? .string("empty") : nil), deletions: deletions,
                         userSong: song(document, authority: authority))
        }
        let merged = merge(base: state(base, authority: baseSong), local: state(local, authority: baseSong),
                           remote: state(remote, authority: remoteSong), preference: preference)
        guard let selected = merged.state else { return .conflicts(merged.conflicts) }
        let selectedSong = selected.userSong?.objectValue ?? [:]
        guard selectedSong["authority"] == (remoteSong ?? .null) else {
            return .unsupported("The song changed in the latest version. Use the latest conflicting changes, then edit the new song.")
        }
        var raw = remote.encodeSnapshot()
        if var payload = raw["editor_payload"]?.objectValue {
            payload["sections"] = .object(selected.sections)
            payload["editor_state"] = selected.editorState
            payload["deletions"] = selected.deletions
            raw["editor_payload"] = .object(payload)
        } else {
            raw = selected.sections
        }
        var decoded = EditorDocument(snapshot: raw)
        if let deletionValue = selected.deletions {
            guard let data = try? JSONEncoder().encode(deletionValue),
                  let intents = try? JSONDecoder().decode([EditorDeletion].self, from: data),
                  intents.allSatisfy({ !$0.kind.isEmpty && !$0.id.isEmpty }),
                  deletionValue == .array(intents.map({ .object(["kind": .string($0.kind), "id": .string($0.id)]) })) else {
                return .unsupported("The latest deletion information could not be read safely. Your edits are still here.")
            }
            decoded.deletions = intents
        }
        if let state = selected.editorState {
            guard let value = state.stringValue, ["empty", "renderable"].contains(value) else {
                return .unsupported("The latest editor state could not be read safely. Your edits are still here.")
            }
            decoded.editorState = value
        }
        guard let validated = decoded.mergeValidationSnapshot(preservingOpaqueFrom: remote),
              preservesExplicitValues(.object(selected.sections), in:
                validated["editor_payload"]?.objectValue?["sections"] ?? .object(validated)),
              validIntervals(.object(selected.sections)) else {
            return .unsupported("These edits cannot be combined safely. Your changes are still here; review the conflicting timing or media before saving.")
        }
        let edit = selectedSong["edit"]?.objectValue ?? [:]
        let songEdit = EditorUserSongState(volume: edit["volume"]?.numberValue,
            windowStartS: edit["start"]?.numberValue, windowEndS: edit["end"]?.numberValue,
            removed: edit["removed"] == .bool(true))
        decoded.userSong = songEdit.isEmpty ? nil : songEdit
        decoded.revision = remote.revision
        decoded.capabilities = remote.capabilities
        return .resolved(decoded)
    }

    private static func preservesExplicitValues(_ selected: JSONValue, in encoded: JSONValue) -> Bool {
        switch (selected, encoded) {
        case (.object(let a), .object(let b)):
            return a.allSatisfy { key, value in b[key].map { preservesExplicitValues(value, in: $0) } ?? false }
        case (.array(let a), .array(let b)):
            return a.count == b.count && zip(a, b).allSatisfy { preservesExplicitValues($0.0, in: $0.1) }
        default: return selected == encoded
        }
    }

    private static func validIntervals(_ value: JSONValue) -> Bool {
        switch value {
        case .object(let fields):
            for keys in intervalGroups.values {
                if let start = fields[keys[0]]?.numberValue, let end = fields[keys[1]]?.numberValue,
                   !start.isFinite || !end.isFinite || start < 0 || end < start { return false }
            }
            return fields.values.allSatisfy(validIntervals)
        case .array(let values): return values.allSatisfy(validIntervals)
        default: return true
        }
    }
    struct State: Equatable {
        var sections: [String: JSONValue]
        var editorState: JSONValue?
        var deletions: JSONValue?
        var userSong: JSONValue?
    }

    struct Result {
        /// Unresolved conflicts deliberately provide no adoptable state.
        let state: State?
        let conflicts: [AuthoredJSONMerge.Conflict]
    }

    private static let authored: Set<String> = [
        "timeline_slots", "speech_timing_basis", "clip_audio", "text_elements",
        "caption_cues", "caption_meta", "mix", "music", "music_track_id", "music_window",
        "background_music", "sound_effects", "media_overlays", "visual_blocks",
        "motion_scenes", "motion_runtime_hash", "camera_effects", "drawings",
        "speaker_background", "carousel_moment", "lyrics", "title", "orientation", "landscape_fit"
    ]
    private static let intervalGroups = [
        "$nativeInterval": ["start_s", "end_s"],
        "$nativeSourceInterval": ["source_start_s", "source_end_s"],
        "$nativeTrimInterval": ["trim_start_s", "trim_end_s"]
    ]
    private static let deletionLanes = ["text": "text_elements", "sound_effect": "sound_effects",
        "media_overlay": "media_overlays", "visual_block": "visual_blocks", "motion_scene": "motion_scenes",
        "camera_effect": "camera_effects", "drawing": "drawings"]

    private static func keyedDeletions(_ value: JSONValue?) -> JSONValue {
        guard let value else { return .array([]) }
        guard case .array(let rows) = value else { return value }
        return .array(rows.map { row in
            guard let fields = row.objectValue, fields.count == 2,
                  let kind = fields["kind"]?.stringValue, let id = fields["id"]?.stringValue else { return row }
            return .object(["id": .string("\(kind):\(id)"), "kind": .string(kind), "target_id": .string(id)])
        })
    }

    static func merge(
        base: State, local: State, remote: State,
        preference: AuthoredJSONMerge.Preference? = nil
    ) -> Result {
        // Explicit deletions may address entities in several lanes. Keep their
        // payload and intent together rather than resurrecting a selected entity
        // while retaining the losing side's delete command.
        let deletionChanged = local.deletions != base.deletions || remote.deletions != base.deletions
        let timelineChanged = local.sections["timeline_slots"] != base.sections["timeline_slots"]
            || remote.sections["timeline_slots"] != base.sections["timeline_slots"]
            || local.editorState != base.editorState || remote.editorState != base.editorState
        let requiresCoupledDeletion = deletionChanged && [base, local, remote].contains { state in
            guard case .array(let rows)? = state.deletions else { return state.deletions != nil }
            return rows.contains { row in
                guard let kind = row.objectValue?["kind"]?.stringValue else { return true }
                return deletionLanes[kind] == nil
            }
        }
        let dependent = authored.subtracting(["title"])
        let grouped = requiresCoupledDeletion || timelineChanged
        var atomicPaths: Set<[String]> = [["dependent"], ["motion"], ["music"], ["canvas"],
            ["backgroundMusic"], ["speakerBackground"], ["editorState"], ["userSong"]]

        // Time endpoints are one authored value, while text/style fields on the
        // same entity can still merge independently.
        func groupIntervals(_ fields: [String: JSONValue]) -> [String: JSONValue] {
            fields.mapValues { value in
                guard case .array(let rows) = value else { return value }
                return .array(rows.map { row in
                    guard var object = row.objectValue else { return row }
                    for (name, keys) in intervalGroups where keys.contains(where: { object[$0] != nil }) {
                        var interval: [String: JSONValue] = [:]
                        for key in keys { interval[key] = object.removeValue(forKey: key) }
                        object[name] = .object(interval)
                    }
                    return .object(object)
                })
            }
        }

        func projection(_ state: State) -> JSONValue {
            var fields = state.sections.filter { authored.contains($0.key) }
            var values: [String: JSONValue] = [:]
            if grouped {
                var group = fields.filter { dependent.contains($0.key) }
                for key in dependent { fields.removeValue(forKey: key) }
                group["$editorState"] = state.editorState
                group["$deletions"] = state.deletions
                group["$userSong"] = state.userSong
                values["dependent"] = .object(group)
            } else {
                func extract(_ keys: [String]) -> JSONValue {
                    var group: [String: JSONValue] = [:]
                    for key in keys { group[key] = fields.removeValue(forKey: key) }
                    return .object(group)
                }
                values["motion"] = extract(["motion_scenes", "motion_runtime_hash"])
                values["music"] = extract(["music", "music_track_id", "music_window"])
                values["canvas"] = extract(["orientation", "landscape_fit"])
                values["backgroundMusic"] = fields.removeValue(forKey: "background_music")
                values["speakerBackground"] = fields.removeValue(forKey: "speaker_background")
                values["editorState"] = state.editorState
                values["deletions"] = keyedDeletions(state.deletions)
                values["userSong"] = state.userSong
            }
            for (lane, value) in fields {
                guard case .array(let rows) = value else { continue }
                for row in rows {
                    for key in ["id", "slot_id"] {
                        guard let id = row.objectValue?[key]?.stringValue else { continue }
                        for name in intervalGroups.keys { atomicPaths.insert(["sections", lane, "\(key)=\(id)", name]) }
                    }
                }
            }
            values["sections"] = .object(groupIntervals(fields))
            return .object(values)
        }

        // Reserved internal group keys must never overwrite an opaque field.
        for state in [base, local, remote] {
            for value in state.sections.values {
                guard case .array(let rows) = value else { continue }
                for row in rows where row.objectValue.map({ !Set($0.keys).isDisjoint(with: intervalGroups.keys) }) == true {
                    return Result(state: nil, conflicts: [.init(path: ["sections"], base: .object(base.sections), local: .object(local.sections), remote: .object(remote.sections))])
                }
            }
        }
        let original = projection(base), left = projection(local), right = projection(remote)
        let merged = AuthoredJSONMerge.merge(
            base: original, local: left, remote: right, preference: preference, atomicPaths: atomicPaths
        )
        guard merged.conflicts.isEmpty, let values = merged.value?.objectValue else {
            return Result(state: nil, conflicts: merged.conflicts)
        }
        // Unknown section-level metadata belongs to the latest server snapshot.
        // Unknown fields inside keyed authored entities were merged above.
        var result = remote
        for key in authored { result.sections.removeValue(forKey: key) }
        for (key, value) in values["sections"]?.objectValue ?? [:] {
            if case .array(let rows) = value {
                result.sections[key] = .array(rows.map { row in
                    guard var object = row.objectValue else { return row }
                    for name in intervalGroups.keys {
                        for (field, value) in object.removeValue(forKey: name)?.objectValue ?? [:] { object[field] = value }
                    }
                    return .object(object)
                })
            } else { result.sections[key] = value }
        }
        if grouped {
            let group = values["dependent"]?.objectValue ?? [:]
            for key in dependent { result.sections[key] = group[key] }
            result.editorState = group["$editorState"]
            result.deletions = group["$deletions"]
            result.userSong = group["$userSong"]
        } else {
            for name in ["motion", "music", "canvas"] {
                for (key, value) in values[name]?.objectValue ?? [:] { result.sections[key] = value }
            }
            result.sections["speaker_background"] = values["speakerBackground"]
            result.sections["background_music"] = values["backgroundMusic"]
            result.editorState = values["editorState"]
            if case .array(let rows)? = values["deletions"] {
                var retained: [JSONValue] = []
                for row in rows {
                    guard let fields = row.objectValue, let kind = fields["kind"]?.stringValue,
                          let id = fields["target_id"]?.stringValue else { retained.append(row); continue }
                    // A conflict choice that retains the live entity must not
                    // retain the other side's command to delete it on Save.
                    if let lane = deletionLanes[kind], case .array(let entities)? = result.sections[lane],
                       entities.contains(where: { $0.objectValue?["id"] == .string(id) && $0.objectValue?["removed"] != .bool(true) }) { continue }
                    retained.append(.object(["kind": .string(kind), "id": .string(id)]))
                }
                result.deletions = retained.isEmpty ? nil : .array(retained)
            } else { result.deletions = values["deletions"] }
            result.userSong = values["userSong"]
        }
        return Result(state: result, conflicts: [])
    }
}
