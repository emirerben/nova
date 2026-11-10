import Foundation
import KriaMediaEngine

/// Native adapter of speech_cut_state.reproject_authored_speech_records.
/// Windows come from the editor's canonical projection; all timed lanes travel
/// through source occurrences in the same undo transaction. Music is independent.
enum NativeSpeechTimelineProjection {
    private struct Window {
        let slot: EditorTimelineSlot
        let start: Double
        let end: Double
        var rate: Double { slot.raw["playback_rate"]?.numberValue ?? 1 }
        var captionEnd: Double {
            guard let sourceEnd = slot.raw["source_end_s"]?.numberValue else { return end }
            return start + min(end - start, max(0, sourceEnd - slot.inS) / rate)
        }
    }
    private static func windows(_ slots: [EditorTimelineSlot]) -> [Window] {
        NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil).baseClipWindows.map {
            let slot = slots[$0.sourceIndex]
            let start = slot.raw["_speech_output_start_s"]?.numberValue ?? $0.start
            return Window(slot: slot, start: start, end: start + ($0.end - $0.start))
        }
    }
    private static func rounded(_ t: Double) -> Double { (t * 1000).rounded() / 1000 }
    private static func start(_ row: [String: JSONValue]) -> Double {
        if let t = row["start_s"]?.numberValue { return t }
        if let t = row["start"]?.numberValue { return t }
        return row["at_s"]?.numberValue ?? 0
    }

    static func records(_ records: [JSONValue], oldSlots: [EditorTimelineSlot], newSlots: [EditorTimelineSlot], captions: Bool = false) -> [JSONValue] {
        let old = windows(oldSlots), new = windows(newSlots)
        var output: [[String: JSONValue]] = []
        for raw in records {
            guard let row = raw.objectValue else { continue }
            let point = row["at_s"] != nil
            let sk = point ? "at_s" : (row["start_s"] != nil ? "start_s" : "start")
            let ek = point ? "at_s" : (row["end_s"] != nil ? "end_s" : "end")
            guard let start = row[sk]?.numberValue, let end = row[ek]?.numberValue else { output.append(row); continue }
            var parts: [[String: JSONValue]] = []
            let pointOwner = old.last { $0.start <= start && start < $0.end }?.slot.id
            for prior in old {
                if point && prior.slot.id != pointOwner { continue }
                let primary = new.first { $0.slot.id == prior.slot.id && $0.slot.clipIndex == prior.slot.clipIndex }
                let targets = new.filter { target in
                    guard target.slot.clipIndex == prior.slot.clipIndex else { return false }
                    if target.slot.id == prior.slot.id { return true }
                    guard target.slot.parentSegmentID == prior.slot.id else { return false }
                    // Duplicating into another panel is a new picture, not a second caption occurrence.
                    return target.slot.splitSectionID == (primary?.slot.splitSectionID ?? prior.slot.splitSectionID)
                        && target.slot.splitPanelIndex == (primary?.slot.splitPanelIndex ?? prior.slot.splitPanelIndex)
                }
                for target in targets {
                func project(_ t: Double) -> Double {
                    target.start + (prior.slot.inS + (t - prior.start) * prior.rate - target.slot.inS) / target.rate
                }
                let lo = max(start, prior.start), hi = min(end, captions ? prior.captionEnd : prior.end)
                let a = max(target.start, project(lo)), b = min(captions ? target.captionEnd : target.end, project(hi))
                if point {
                    guard prior.start <= start, start < prior.end, target.start <= project(start), project(start) < target.end else { continue }
                } else if hi <= lo || b <= a + 1e-9 { continue }
                var item = row
                item[sk] = .number(rounded(a)); item[ek] = .number(rounded(b))
                if let originalWords = row["words"]?.arrayValue {
                    let words: [JSONValue] = originalWords.compactMap { value in
                        guard var word = value.objectValue else { return nil }
                        let wk = word["start_s"] != nil ? "start_s" : "start", we = word["end_s"] != nil ? "end_s" : "end"
                        guard let ws = word[wk]?.numberValue, let weValue = word[we]?.numberValue else { return nil }
                        let wa = max(a, project(ws)), wb = min(b, project(weValue))
                        guard wb > wa + 1e-9 else { return nil }
                        word[wk] = .number(rounded(wa)); word[we] = .number(rounded(wb))
                        return .object(word)
                    }
                    if captions && words.isEmpty { continue }
                    item["words"] = .array(words)
                    if captions && words.count != originalWords.count {
                        item["text"] = .string(words.compactMap { $0.objectValue?["text"]?.stringValue ?? $0.objectValue?["word"]?.stringValue }.joined(separator: " "))
                    }
                }
                if let original = row["word_timings"]?.arrayValue {
                    let timings: [JSONValue] = original.compactMap { value in
                        guard var word = value.objectValue else { return nil }
                        let ws = word["start_s"]?.numberValue ?? 0
                        let we = word["end_s"]?.numberValue ?? (ws + (word["duration_cs"]?.numberValue ?? 5) / 100)
                        let wa = max(a, project(start + ws)), wb = min(b, project(start + we))
                        guard wb > wa + 1e-9 else { return nil }
                        word["start_s"] = .number(rounded(wa - a)); word["end_s"] = .number(rounded(wb - a))
                        if word["duration_cs"] != nil { word["duration_cs"] = .number(((wb - wa) * 100).rounded()) }
                        return .object(word)
                    }
                    item["word_timings"] = .array(timings)
                    if timings.count != original.count { item["text"] = .string(timings.compactMap { $0.objectValue?["text"]?.stringValue }.joined(separator: " ")) }
                }
                if var params = item["source_params"]?.objectValue, let schedule = params["reveal_schedule_s"]?.arrayValue {
                    params["reveal_schedule_s"] = .array(schedule.compactMap { value in
                        guard let t = value.numberValue else { return nil }
                        let mapped = project(t)
                        return a <= mapped && mapped < b ? .number(rounded(mapped)) : nil
                    })
                    item["source_params"] = .object(params)
                }
                if !parts.isEmpty, let id = item["id"]?.stringValue { item["id"] = .string("\(id)-part-\(parts.count)") }
                parts.append(item)
                }
            }
            output.append(contentsOf: parts)
        }
        return output.sorted { start($0) < start($1) }.map(JSONValue.object)
    }

    static func ownerID(_ row: [String: JSONValue]) -> String? {
        row["segment_id"]?.stringValue ?? row["source_params"]?.objectValue?["segment_id"]?.stringValue
    }

    private static func sections(_ document: EditorDocument) -> [String: JSONValue] {
        let snapshot = document.encodeSnapshot()
        return snapshot["editor_payload"]?.objectValue?["sections"]?.objectValue
            ?? snapshot["sections"]?.objectValue ?? snapshot
    }
    private static func entry(_ record: JSONValue, slots: [EditorTimelineSlot]) -> JSONValue {
        let keys = Set(["slot_id", "clip_index", "in_s", "duration_s", "playback_rate", "transition_after", "transition_duration_s", "removed", "source_end_s", "parent_segment_id", "split_section_id", "split_panel_index", "split_offset_s"])
        let raw = sections(EditorDocument(clips: slots))["timeline_slots"]?.arrayValue ?? []
        let value = record.objectValue ?? [:]
        let low = value["start_s"]?.numberValue ?? value["start"]?.numberValue ?? value["at_s"]?.numberValue
        let high = value["end_s"]?.numberValue ?? value["end"]?.numberValue ?? value["at_s"]?.numberValue
        let owner = ownerID(value)
        let anchors: [JSONValue] = windows(slots).compactMap { window in
            if let owner { guard window.slot.id == owner || window.slot.parentSegmentID == owner else { return nil } }
            else if window.slot.splitSectionID != nil, window.slot.splitPanelIndex != 0 { return nil }
            if let low, let high {
                if value["at_s"] != nil {
                    guard window.start <= low, low < window.end else { return nil }
                } else if min(high, window.end) <= max(low, window.start) { return nil }
            }
            guard let row = raw.first(where: { $0.objectValue?["slot_id"]?.stringValue == window.slot.id })?.objectValue else { return nil }
            var anchor = row.filter { keys.contains($0.key) }
            anchor["_speech_output_start_s"] = .number(window.start)
            return .object(anchor)
        }
        return .object(["record": record, "slots": .array(anchors)])
    }

    private static func sourceCaptions(_ entry: [String: JSONValue], slots: [EditorTimelineSlot]) -> [JSONValue] {
        let suppressed = Set((entry["suppressed_ids"]?.arrayValue ?? []).compactMap(\.stringValue))
        var output: [JSONValue] = []
        for value in entry["source_groups"]?.arrayValue ?? [] {
            guard let group = value.objectValue, let full = group["slot"]?.objectValue,
                  let id = full["slot_id"]?.stringValue, let target = slots.first(where: { $0.id == id }),
                  let initialStart = group["initial_start_s"]?.numberValue, let initialEnd = group["initial_end_s"]?.numberValue else { continue }
            let end = min(target.inS + (target.durationS ?? 0) * (target.raw["playback_rate"]?.numberValue ?? 1), target.raw["source_end_s"]?.numberValue ?? .infinity)
            for (index, raw) in (entry["source_captions"]?.arrayValue ?? []).enumerated() {
                let identifier = "speech-source-\(id)-\(index)"
                guard !suppressed.contains(identifier), var cue = raw.objectValue else { continue }
                let outside = (cue["words"]?.arrayValue ?? []).filter {
                    guard let word = $0.objectValue, let a = word["start_s"]?.numberValue, let b = word["end_s"]?.numberValue else { return false }
                    return b <= initialStart || a >= initialEnd
                }
                guard outside.contains(where: { word in
                    guard let a = word.objectValue?["start_s"]?.numberValue, let b = word.objectValue?["end_s"]?.numberValue else { return false }
                    return b > target.inS && a < end
                }) else { continue }
                cue["id"] = .string(identifier); cue["words"] = .array(outside)
                cue["text"] = .string(outside.compactMap { $0.objectValue?["text"]?.stringValue }.joined(separator: " "))
                cue["start_s"] = outside.first?.objectValue?["start_s"]; cue["end_s"] = outside.last?.objectValue?["end_s"]
                let old = EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(["timeline_slots": .array([.object(full)])])])]).clips
                output += records([.object(cue)], oldSlots: old, newSlots: slots, captions: true)
            }
        }
        return output
    }

    private static func projectBasis(_ entries: [JSONValue], slots: [EditorTimelineSlot], captions: Bool) -> [JSONValue] {
        let projected: [JSONValue] = entries.flatMap { value -> [JSONValue] in
            guard let entry = value.objectValue else { return [] }
            if entry["source_groups"] != nil { return sourceCaptions(entry, slots: slots) }
            guard let row = entry["record"], let old = entry["slots"] else { return [] }
            let oldSlots = EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(["timeline_slots": old])])]).clips
            return records([row], oldSlots: oldSlots, newSlots: slots, captions: captions)
        }
        return projected.sorted { start($0.objectValue ?? [:]) < start($1.objectValue ?? [:]) }
    }
    private static func synchronize(_ entries: [JSONValue]?, records: [JSONValue], slots: [EditorTimelineSlot], captions: Bool, deleted: Set<String>) -> [JSONValue] {
        guard let entries else { return records.map { entry($0, slots: slots) } }
        var remaining = records, kept: [JSONValue] = []
        let ordered = entries.filter { $0.objectValue?["source_groups"] == nil } + entries.filter { $0.objectValue?["source_groups"] != nil }
        for value in ordered {
            if var group = value.objectValue, group["source_groups"] != nil {
                var suppressed = Set((group["suppressed_ids"]?.arrayValue ?? []).compactMap(\.stringValue)).union(deleted)
                group["suppressed_ids"] = .array(suppressed.sorted().map(JSONValue.string))
                for row in projectBasis([.object(group)], slots: slots, captions: true) {
                    if let index = remaining.firstIndex(of: row) { remaining.remove(at: index) }
                    else if let id = row.objectValue?["id"]?.stringValue { suppressed.insert(id) }
                }
                group["suppressed_ids"] = .array(suppressed.sorted().map(JSONValue.string))
                kept.append(.object(group)); continue
            }
            if let id = value.objectValue?["record"]?.objectValue?["id"]?.stringValue, deleted.contains(id) { continue }
            let visible = projectBasis([value], slots: slots, captions: captions)
            if visible.isEmpty { kept.append(value) }
            else if visible.allSatisfy({ remaining.contains($0) }) {
                kept.append(value)
                for row in visible { if let i = remaining.firstIndex(of: row) { remaining.remove(at: i) } }
            }
        }
        return kept + remaining.map { entry($0, slots: slots) }
    }

    static func requiresCaptionTiming(_ document: EditorDocument) -> Bool {
        guard document.captionMeta["enabled"] != .bool(false),
              !document.captionCues.isEmpty || !(document.speechTimingBasis["caption_cues"]?.arrayValue ?? []).isEmpty,
              let coverage = document.speechTimingBasis["source_windows"]?.arrayValue else { return false }
        return document.clips.contains { slot in
            guard slot.id?.hasPrefix("native-phone-talking-source") == true else { return false }
            let rate = slot.raw["playback_rate"]?.numberValue ?? 1
            let nominalEnd = slot.inS + (slot.durationS ?? 0) * rate
            let end = min(nominalEnd, slot.raw["source_end_s"]?.numberValue ?? nominalEnd)
            return !coverage.contains { value in
                guard let row = value.objectValue, row["slot_id"]?.stringValue == slot.id,
                      let start = row["in_s"]?.numberValue, let length = row["duration_s"]?.numberValue else { return false }
                return slot.inS >= start - 1e-6 && end <= start + length * (row["playback_rate"]?.numberValue ?? 1) + 1e-6
            }
        }
    }

    static func acknowledgeCaptionEdits(_ next: inout EditorDocument, from previous: EditorDocument) {
        if next.clips == previous.clips, next.captionCues != previous.captionCues,
           !previous.speechTimingBasis.isEmpty {
            next.speechTimingBasis["source_windows"] = entry(.object([:]), slots: next.clips).objectValue?["slots"]
        }
    }

    /// `document.clips` with each one's output start, as the Range projection reads them.
    static func projectedSlots(_ clips: [EditorTimelineSlot], splitSections: [EditorSplitScreenSection]) -> [EditorTimelineSlot] {
        let projection = NativeEditorInteraction.timelineProjection(slots: clips, carousel: nil, splitSections: splitSections)
        return clips.enumerated().map { index, slot in
            var copy = slot
            if let window = projection.baseClipWindows.first(where: { $0.sourceIndex == index }) {
                copy.raw["_speech_output_start_s"] = .number(window.start)
            }
            return copy
        }
    }

    /// KRI-1350: the stored colour-moments lane moved from the timeline it was saved on onto the current Range edit,
    /// mirroring the server's `reproject_color_moments`: each moment keeps the span its footage still plays; one whose
    /// footage is gone, that is now under 0.5 s, or that would overlap the one before is dropped. Unchanged when the
    /// edit is not a Talking source edit or its timeline has not changed.
    static func colorMomentsLane(_ lane: JSONValue?, baseSlots: [EditorTimelineSlot], document: EditorDocument) -> JSONValue? {
        guard let lane, case var .object(object) = lane, case let .array(rows)? = object["moments"], !rows.isEmpty,
              baseSlots != document.clips,
              baseSlots.contains(where: { $0.id?.hasPrefix("native-phone-talking-source") == true }) else { return lane }
        let old = projectedSlots(baseSlots, splitSections: document.splitScreenSections)
        let new = projectedSlots(document.clips, splitSections: document.splitScreenSections)
        var moments: [JSONValue] = []
        var lastEnd: Double?
        for row in rows {
            guard var moment = row.objectValue, let start = moment["start"]?.numberValue, let end = moment["end"]?.numberValue else { continue }
            let parts = records([.object(["id": moment["id"] ?? .null, "start_s": .number(start), "end_s": .number(end)])], oldSlots: old, newSlots: new)
            guard let a = parts.compactMap({ $0.objectValue?["start_s"]?.numberValue }).min(),
                  let b = parts.compactMap({ $0.objectValue?["end_s"]?.numberValue }).max(),
                  b - a >= ColorMoment.minimumLength - 1e-6, lastEnd.map({ a >= $0 - 1e-6 }) ?? true else { continue }
            moment["start"] = .number(rounded(a)); moment["end"] = .number(rounded(b))
            moments.append(.object(moment)); lastEnd = b
        }
        object["moments"] = .array(moments)
        return .object(object)
    }

    /// Sound id (`cmsfx-<moment>-<enter|exit>`) -> its moment's edge, for re-pinning a moment's own sound like the
    /// server's `rebind_color_moment_sounds`.
    static func colorMomentEdges(_ lane: JSONValue?) -> [String: Double] {
        var edges: [String: Double] = [:]
        for row in lane?.objectValue?["moments"]?.arrayValue ?? [] {
            guard let moment = row.objectValue, let id = moment["id"]?.stringValue,
                  let start = moment["start"]?.numberValue, let end = moment["end"]?.numberValue else { continue }
            edges["\(colorMomentSoundPrefix)\(id)-enter"] = rounded(start)
            edges["\(colorMomentSoundPrefix)\(id)-exit"] = rounded(end)
        }
        return edges
    }
    static let colorMomentSoundPrefix = "cmsfx-"

    @discardableResult
    static func rebase(_ next: inout EditorDocument, from previous: EditorDocument) -> Bool {
        guard previous.clips != next.clips || previous.splitScreenSections != next.splitScreenSections,
              previous.clips.contains(where: { $0.id?.hasPrefix("native-phone-talking-source") == true }) else { return false }
        func projectedSlots(_ document: EditorDocument) -> [EditorTimelineSlot] {
            Self.projectedSlots(document.clips, splitSections: document.splitScreenSections)
        }
        let previousSlots = projectedSlots(previous), nextSlots = projectedSlots(next)
        var source = sections(next)
        // KRI-1350: a colour moment's own sound never travels through source time on its own; it is re-pinned to its
        // moment's edge below (the server's `reproject_lanes` + `rebind_color_moment_sounds`).
        let momentSounds = next.soundEffects.filter { $0.id.hasPrefix(colorMomentSoundPrefix) }
        if !momentSounds.isEmpty, let lane = source["sound_effects"]?.arrayValue {
            source["sound_effects"] = .array(lane.filter { $0.objectValue?["id"]?.stringValue?.hasPrefix(colorMomentSoundPrefix) != true })
        }
        // Legacy cues get the same stable identities as server canonical_caption_rows.
        source["caption_cues"] = .array(next.captionCues.map { cue in
            var raw = cue.raw
            raw["id"] = .string(cue.id); raw["text"] = .string(cue.text)
            raw["start_s"] = .number(cue.startS); raw["end_s"] = .number(cue.endS)
            return .object(raw)
        })
        source["text_elements"] = .array((source["text_elements"]?.arrayValue ?? []).filter { $0.objectValue?["source_params"]?.objectValue?["source"]?.stringValue != "caption_cue" })
        var output = source, basis = previous.speechTimingBasis
        if let entries = basis["text_elements"]?.arrayValue {
            basis["text_elements"] = .array(entries.filter { $0.objectValue?["record"]?.objectValue?["source_params"]?.objectValue?["source"]?.stringValue != "caption_cue" })
        }
        if basis["source_windows"] == nil { basis["source_windows"] = entry(.object([:]), slots: previousSlots).objectValue?["slots"] }
        let hasSplit = !previous.splitScreenSections.isEmpty || !next.splitScreenSections.isEmpty
        for field in ["caption_cues", "text_elements", "sound_effects", "media_overlays", "visual_blocks", "motion_scenes", "camera_effects", "drawings"] {
            guard let lane = source[field]?.arrayValue else { continue }
            let kinds = ["caption_cues": "caption_cue", "text_elements": "text", "sound_effects": "sound_effect",
                         "media_overlays": "media_overlay", "visual_blocks": "visual_block", "motion_scenes": "motion_scene",
                         "camera_effects": "camera_effect", "drawings": "drawing"]
            let deleted = Set(next.deletions.filter { $0.kind == kinds[field] }.map(\.id))
            // Captions follow source clocks. Other unowned split-canvas records are
            // projected once by NativeSplitTimelineRebase, including uncovered panel gaps.
            let canvasOwned = hasSplit && field != "caption_cues"
            let canvas = canvasOwned ? lane.filter { ownerID($0.objectValue ?? [:]) == nil } : []
            let sourceLane = canvasOwned ? lane.filter { ownerID($0.objectValue ?? [:]) != nil } : lane
            let oldBasis = basis[field]?.arrayValue.map { entries in
                canvasOwned ? entries.filter { ownerID($0.objectValue?["record"]?.objectValue ?? [:]) != nil } : entries
            }
            let entries = synchronize(oldBasis, records: sourceLane, slots: previousSlots, captions: field == "caption_cues", deleted: deleted)
            basis[field] = .array(entries)
            output[field] = .array((projectBasis(entries, slots: nextSlots, captions: field == "caption_cues") + canvas).sorted { start($0.objectValue ?? [:]) < start($1.objectValue ?? [:]) })
        }
        let projected = EditorDocument(snapshot: ["editor_payload": .object(["sections": .object(output)])])
        next.speechTimingBasis = basis
        next.captionCues = projected.captionCues
        next.textElements = projected.textElements
        next.soundEffects = projected.soundEffects
        if !momentSounds.isEmpty {
            let edges = colorMomentEdges(colorMomentsLane(next.colorMoments, baseSlots: next.colorMomentsBaseSlots, document: next))
            next.soundEffects = (projected.soundEffects + momentSounds.compactMap { sound in
                guard let edge = edges[sound.id] else { return nil }
                var moved = sound
                let shift = edge - (sound.pointS ?? sound.startS)
                moved.pointS = edge; moved.startS += shift; moved.endS += shift
                moved.raw["at_s"] = .number(edge)
                return moved
            }).sorted { ($0.pointS ?? $0.startS) < ($1.pointS ?? $1.startS) }
        }
        next.mediaOverlays = projected.mediaOverlays
        next.visualBlocks = projected.visualBlocks
        next.motionScenes = projected.motionScenes
        next.cameraEffects = projected.cameraEffects
        next.drawings = projected.drawings
        return true
    }
}
