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
            var sections = payload?["sections"]?.objectValue ?? payload ?? raw
            // Promote fallback rows without expanding defaults into untouched raw fields.
            // Otherwise adopting a clean remote timeline looks like a new local edit.
            if document.splitScreenIsValid {
                if sections["timeline_slots"] == nil,
                   let fallbackRows = raw["user_timeline"]?.objectValue?["slots"] {
                    sections["timeline_slots"] = fallbackRows
                }
                if sections["split_screen_sections"] != nil
                    || raw["user_timeline"]?.objectValue?["split_screen_sections"] != nil
                    || !document.splitScreenSections.isEmpty {
                    sections["split_screen_sections"] = .array(document.splitScreenSections.map(\.json))
                }
            }
            let deletions = payload?["deletions"] ?? (document.deletions.isEmpty ? nil : .array(document.deletions.map {
                .object(["kind": .string($0.kind), "id": .string($0.id)])
            }))
            return State(sections: sections,
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
        guard decoded.splitScreenIsValid,
              let validated = decoded.mergeValidationSnapshot(preservingOpaqueFrom: remote),
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

    struct Converged {
        /// The version the editor shows: always valid, never a pending decision.
        let document: EditorDocument
        /// The creator's edits rebuilt on `latest` (so it carries the latest base generation). Nil when
        /// there is nothing to restore or the rebuild does not resolve and validate.
        let undo: EditorDocument?
        /// True when a creator-changed lane was dropped in favour of `latest`.
        let replacedCreatorEdits: Bool
    }

    /// Total: the latest version wins every overlap. `local` is nil when the editor is clean. Lanes in
    /// `assistantSections` were staged from an AI draft: they replay like any other edit, but replacing them is
    /// never reported as replacing the creator's edits.
    static func converge(base: EditorDocument, local: EditorDocument?, latest: EditorDocument,
                         baseSong: JSONValue?, latestSong: JSONValue?,
                         assistantSections: Set<EditorSection> = []) -> Converged {
        guard let local else { return Converged(document: latest, undo: nil, replacedCreatorEdits: false) }
        func merged(_ preference: AuthoredJSONMerge.Preference?) -> EditorDocument? {
            guard case .resolved(let value) = documents(base: base, local: local, remote: latest,
                baseSong: baseSong, remoteSong: latestSong, preference: preference) else { return nil }
            return value
        }
        let creatorSections = EditorSection.allCases.filter {
            !assistantSections.contains($0) && local.snapshot(for: $0) != base.snapshot(for: $0)
        }
        let creatorChangedOther = local.editorState != base.editorState || local.deletions != base.deletions
            || local.userSong != base.userSong
        let creatorFirst = merged(.local)
        let document: EditorDocument
        var replaced = false
        if let clean = merged(nil) {
            document = clean
        } else {
            document = merged(.remote) ?? latest
            // Only a lane the creator changed, where the overlap went to the latest, counts as replaced.
            replaced = creatorFirst.map { first in
                creatorSections.contains { first.snapshot(for: $0) != document.snapshot(for: $0) }
                    || (creatorChangedOther && (first.editorState != document.editorState
                        || first.deletions != document.deletions || first.userSong != document.userSong))
            } ?? (!creatorSections.isEmpty || creatorChangedOther)
        }
        // Without an overlap the creator's edits already sit on `latest`; nothing separate to restore.
        let undo = creatorFirst.flatMap { $0 == document ? nil : $0 }
        return Converged(document: document, undo: undo, replacedCreatorEdits: replaced)
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
        "timeline_slots", "split_screen_sections", "speech_timing_basis", "clip_audio", "text_elements",
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

    /// A rendered read regenerates this compatibility text mirror. Recognize
    /// content only when every unique mirror ID agrees with canonical text;
    /// compare all remaining mirror fields and ordering without normalization.
    private static func textComparisonSections(_ state: State) -> [String: JSONValue]? {
        var sections = state.sections
        guard var mirror = sections["ios_editor"]?.objectValue,
              let rows = mirror["text"]?.arrayValue,
              let canonical = sections["text_elements"]?.arrayValue,
              rows.count == canonical.count else { return nil }
        var textByID: [String: JSONValue] = [:]
        for row in canonical {
            guard let value = row.objectValue, let id = value["id"]?.stringValue, !id.isEmpty,
                  case .string? = value["text"], textByID[id] == nil else { return nil }
            textByID[id] = value["text"]
        }
        var ids = Set<String>(), normalized: [JSONValue] = []
        for row in rows {
            guard var value = row.objectValue, let id = value["id"]?.stringValue,
                  ids.insert(id).inserted, let text = textByID[id], value["content"] == text else { return nil }
            value.removeValue(forKey: "content")
            normalized.append(.object(value))
        }
        mirror["text"] = .array(normalized)
        sections["ios_editor"] = .object(mirror)
        return sections
    }

    /// A trim cannot invalidate wording/font changes to the same existing rows.
    /// Unknown fields, timing, source identity, insertion, deletion and order remain
    /// coupled. The exception deliberately recognizes a small authored vocabulary.
    private static func independentTextEdit(_ changed: State, from base: State) -> Bool {
        let sameOtherFields: Bool
        if changed.sections.filter({ $0.key != "text_elements" }) == base.sections.filter({ $0.key != "text_elements" }) {
            sameOtherFields = true
        } else if let left = textComparisonSections(changed), let right = textComparisonSections(base) {
            sameOtherFields = left.filter({ $0.key != "text_elements" }) == right.filter({ $0.key != "text_elements" })
        } else { sameOtherFields = false }
        guard changed.editorState == base.editorState, changed.deletions == base.deletions,
              changed.userSong == base.userSong,
              sameOtherFields,
              case .array(let before)? = base.sections["text_elements"],
              case .array(let after)? = changed.sections["text_elements"], before.count == after.count else { return false }
        let allowed: Set<String> = ["text", "font_family", "font_size_px"]
        var ids = Set<String>()
        return zip(before, after).allSatisfy { old, new in
            guard let a = old.objectValue, let b = new.objectValue,
                  let id = a["id"]?.stringValue, !id.isEmpty, ids.insert(id).inserted,
                  b["id"] == a["id"] else { return false }
            return a.filter { !allowed.contains($0.key) } == b.filter { !allowed.contains($0.key) }
        }
    }

    private static func hasOnlyStableTimelineTrim(_ changed: State, from base: State) -> Bool {
        guard changed.editorState == base.editorState, changed.deletions == base.deletions,
              changed.userSong == base.userSong,
              changed.sections.filter({ $0.key != "timeline_slots" }) == base.sections.filter({ $0.key != "timeline_slots" }),
              case .array(let before)? = base.sections["timeline_slots"],
              case .array(let after)? = changed.sections["timeline_slots"],
              !before.isEmpty, before.count == after.count else { return false }
        let trim: Set<String> = ["in_s", "duration_s", "duration_beats"]
        func stableFields(_ fields: [String: JSONValue]) -> [String: JSONValue] {
            var result = fields.filter { !trim.contains($0.key) }
            // Editing serializes these schema defaults even when the original
            // snapshot omitted them. Preserve every unknown field and every
            // nondefault transition/look value in the identity comparison.
            for key in ["parent_segment_id", "look_adjustments", "transition_duration_s"] where result[key] == .null {
                result.removeValue(forKey: key)
            }
            if result["transition_after"] == .string("cut") { result.removeValue(forKey: "transition_after") }
            return result
        }
        var ids = Set<String>()
        return zip(before, after).allSatisfy { old, new in
            guard let a = old.objectValue, let b = new.objectValue,
                  let id = (a["slot_id"] ?? a["id"])?.stringValue, !id.isEmpty,
                  ids.insert(id).inserted else { return false }
            return stableFields(a) == stableFields(b)
        }
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
            || local.sections["split_screen_sections"] != base.sections["split_screen_sections"]
            || remote.sections["split_screen_sections"] != base.sections["split_screen_sections"]
            || local.editorState != base.editorState || remote.editorState != base.editorState
        let requiresCoupledDeletion = deletionChanged && [base, local, remote].contains { state in
            guard case .array(let rows)? = state.deletions else { return state.deletions != nil }
            return rows.contains { row in
                guard let kind = row.objectValue?["kind"]?.stringValue else { return true }
                return deletionLanes[kind] == nil
            }
        }
        let dependent = authored.subtracting(["title"])
        let independentTextAndTrim = (independentTextEdit(local, from: base) && hasOnlyStableTimelineTrim(remote, from: base))
            || (independentTextEdit(remote, from: base) && hasOnlyStableTimelineTrim(local, from: base))
        let grouped = requiresCoupledDeletion || (timelineChanged && !independentTextAndTrim)
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
