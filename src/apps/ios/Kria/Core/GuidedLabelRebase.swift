import Foundation

/// Phone-side port of the server's `rebase_guided_text` for the per-clip label
/// lane (`services/kria_editor_timeline.py`: `_follow_window`,
/// `_preserve_label_offsets`, `_is_untouched`).
///
/// A guided story times each clip's label bar on absolute output windows, so a
/// trim / extend / reorder / delete used to strand every later label at its old
/// time (the editor retimed only the slot). This pure function re-windows ONLY
/// clip-bound labels onto their slots and guided titles onto the new timeline.
/// Captions, narration labels, lyric bars and other creator text are returned
/// untouched, and no text, style or position field ever changes.
///
/// Parity is pinned by `Tests/Fixtures/guided_label_rebase_vectors.json`, which the
/// server's own `rebase_guided_text` generates and both sides assert.
///
/// Documented phone-only deviation: a label whose old slot cannot be identified at all
/// (no matching `segment_id`, no `clip-label-media-<media_id>` link) is left where it is.
/// The server drops it; on the phone dropping a creator's text because an id was missing is
/// the worse failure.
enum GuidedLabelRebase {
    static let minBarS: Double = 0.2
    static let edgeS: Double = 0.05
    static let labelPrefix = "clip-label-"
    static let labelMediaPrefix = "clip-label-media-"
    static let openingTitleID = "guided-title"
    static let closingTitleID = "guided-closing-title"

    /// One active slot's output window on the editor's base clock (the same walk the
    /// timeline renders, including transition overlap).
    struct SlotWindow: Equatable {
        let id: String
        let parentID: String
        let mediaID: String?
        let start: Double
        let end: Double
        let sourceStart: Double
        let sourceEnd: Double
    }

    static func windows(of slots: [EditorTimelineSlot]) -> [SlotWindow] {
        NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil).baseClipWindows.map { window in
            let slot = slots[window.sourceIndex]
            return SlotWindow(
                id: slot.id ?? "", parentID: slot.parentSegmentID ?? "",
                mediaID: slot.raw["media_id"]?.stringValue,
                start: window.start, end: window.end,
                sourceStart: slot.inS,
                sourceEnd: slot.inS + (window.end - window.start) * (slot.raw["playback_rate"]?.numberValue ?? 1)
            )
        }
    }

    /// True when the label lane has anything to follow.
    static func hasLabels(_ elements: [EditorTextElement]) -> Bool {
        elements.contains(where: isGuidedText)
    }

    static func isGuidedText(_ element: EditorTextElement) -> Bool {
        !isUntouched(element) && (isClipLabel(element) || isAnchoredTitle(element))
    }

    private static func isAnchoredTitle(_ element: EditorTextElement) -> Bool {
        element.id == openingTitleID || element.id == closingTitleID
    }

    static func isClipLabel(_ element: EditorTextElement) -> Bool {
        element.id.hasPrefix(labelPrefix) && !isUntouched(element)
    }

    private static func isUntouched(_ element: EditorTextElement) -> Bool {
        let params = element.raw["source_params"]?.objectValue ?? [:]
        if params["source"]?.stringValue == "caption_cue" { return true }
        if let kind = params["narration_label_kind"], kind != .null, kind.stringValue != "participant" { return true }
        return element.id.hasPrefix("lyric_")
    }

    static func rebase(
        _ elements: [EditorTextElement],
        oldSlots: [EditorTimelineSlot],
        newSlots: [EditorTimelineSlot]
    ) -> [EditorTextElement] {
        let old = windows(of: oldSlots)
        let new = windows(of: newSlots)
        // Never wipe every label because the new timeline is momentarily empty.
        guard !new.isEmpty, !old.isEmpty else { return elements }
        let oldTotal = old.map(\.end).max() ?? 0
        let newTotal = new.map(\.end).max() ?? 0
        var result: [EditorTextElement] = []
        for element in elements {
            guard !isUntouched(element) else { result.append(element); continue }
            // IDs exempt titles from clip-label matching even when a previous
            // server projection stamped a segment_id onto the bar.
            if isAnchoredTitle(element) {
                if let title = rebaseTitle(element, old: old, new: new, oldTotal: oldTotal, newTotal: newTotal) {
                    result.append(title)
                }
                continue
            }
            guard isClipLabel(element) else { result.append(element); continue }
            var media: String?
            if element.id.hasPrefix(labelMediaPrefix) {
                let rest = String(element.id.dropFirst(labelMediaPrefix.count))
                media = rest.isEmpty ? nil : rest
            }
            let oldWindow = identify(element, media: media, in: old)
            if oldWindow == nil && media == nil { result.append(element); continue }
            guard let target = follow(oldWindow, media: media, in: new) else { continue }
            var updated = element
            var start = target.start, end = target.end
            if let oldWindow {
                (start, end) = preserveOffsets(
                    start: element.startS, end: element.endS, old: oldWindow, window: (target.start, target.end)
                )
            }
            updated.startS = round6(start)
            updated.endS = round6(end)
            updated.raw["segment_id"] = .string(target.id)
            result.append(updated)
        }
        return result
    }

    /// Match `rebase_guided_text`: actual edge positions determine anchoring;
    /// manually inset titles travel through source time, including split children.
    private static func rebaseTitle(
        _ element: EditorTextElement, old: [SlotWindow], new: [SlotWindow], oldTotal: Double, newTotal: Double
    ) -> EditorTextElement? {
        let start = element.startS, end = element.endS
        let opening = start <= edgeS
        let closing = oldTotal > 0 && end >= oldTotal - edgeS
        let frameS = 1.0 / 30.0
        var newStart: Double
        var newEnd: Double
        var updated = element
        if opening || closing {
            if opening && closing {
                newStart = 0; newEnd = newTotal
            } else if opening {
                newStart = start; newEnd = min(end, newTotal)
            } else {
                newEnd = newTotal; newStart = max(0, newTotal - (end - start))
            }
            if newEnd - newStart < minBarS {
                newEnd = min(newTotal, newStart + minBarS)
                newStart = max(0, newEnd - minBarS)
            }
        } else {
            guard let a = project(start, old: old, new: new),
                  let b = project(max(start, end - frameS), old: old, new: new) else { return nil }
            newStart = a.time
            newEnd = min(newTotal, b.time + frameS)
            if newEnd - newStart <= frameS / 2 {
                if let segment = new.first(where: { $0.id == a.id }) { newEnd = min(newTotal, segment.end) }
                if newEnd - newStart <= frameS / 2 {
                    newEnd = min(newTotal, newStart + minBarS)
                    newStart = max(0, newEnd - minBarS)
                }
            }
            if end - start >= minBarS && newEnd - newStart < minBarS {
                newEnd = min(newTotal, newStart + minBarS)
                newStart = max(0, newEnd - minBarS)
            }
            updated.raw["segment_id"] = .string(a.id)
        }
        updated.startS = round6(newStart)
        updated.endS = round6(newEnd)
        return updated
    }

    /// The server's `make_time_projector`: later overlaps/split children win;
    /// deleted anchors disappear, and trimmed source positions clamp to an edge.
    private static func project(_ time: Double, old: [SlotWindow], new: [SlotWindow]) -> (time: Double, id: String)? {
        let anchor: SlotWindow
        let sourceTime: Double
        if let window = old.reversed().first(where: { $0.start <= time && time < $0.end }) {
            anchor = window
            sourceTime = min(window.sourceEnd, window.sourceStart + max(0, time - window.start))
        } else if let last = old.last, abs(time - last.end) <= 0.000001 {
            anchor = last; sourceTime = last.sourceEnd
        } else { return nil }
        let candidates = new.filter {
            anchor.id.isEmpty ? $0.mediaID == anchor.mediaID : ($0.id == anchor.id || $0.parentID == anchor.id)
        }
        let containing = candidates.last { $0.sourceStart - 0.000001 <= sourceTime && sourceTime <= $0.sourceEnd + 0.000001 }
        let target = containing ?? candidates.min {
            min(abs(sourceTime - $0.sourceStart), abs(sourceTime - $0.sourceEnd)) <
                min(abs(sourceTime - $1.sourceStart), abs(sourceTime - $1.sourceEnd))
        }
        guard let target else { return nil }
        let clamped = min(target.sourceEnd, max(target.sourceStart, sourceTime))
        let projected = min(target.end, max(target.start, target.start + clamped - target.sourceStart))
        return (round6((projected * 30).rounded(.toNearestOrEven) / 30), target.id)
    }

    private static func round6(_ value: Double) -> Double { (value * 1_000_000).rounded() / 1_000_000 }

    private static func overlap(_ a0: Double, _ a1: Double, _ b0: Double, _ b1: Double) -> Double {
        max(0, min(a1, b1) - max(a0, b0))
    }

    private static func identify(_ element: EditorTextElement, media: String?, in old: [SlotWindow]) -> SlotWindow? {
        if let segmentID = element.raw["segment_id"]?.stringValue, !segmentID.isEmpty,
           let match = old.first(where: { $0.id == segmentID }) {
            return match
        }
        guard let media else { return nil }
        let candidates = old.filter { $0.mediaID == media }
        // First maximal overlap wins, like Python's max().
        var best: SlotWindow?
        var bestOverlap = -1.0
        for candidate in candidates {
            let value = overlap(element.startS, element.endS, candidate.start, candidate.end)
            if value > bestOverlap { best = candidate; bestOverlap = value }
        }
        return best
    }

    private static func follow(_ old: SlotWindow?, media: String?, in new: [SlotWindow]) -> (start: Double, end: Double, id: String)? {
        var targets: [Int] = []
        if let old, !old.id.isEmpty {
            var family: Set<String> = [old.id]
            var grew = true
            while grew {
                grew = false
                for slot in new where !slot.parentID.isEmpty && family.contains(slot.parentID) && !family.contains(slot.id) {
                    family.insert(slot.id); grew = true
                }
            }
            targets = new.indices.filter { family.contains(new[$0].id) }
        } else if let media, let first = new.firstIndex(where: { $0.mediaID == media }) {
            targets = [first]
        }
        guard let first = targets.first, let last = targets.last else { return nil }
        let contiguous = targets == Array(first...last)
        let head = new[first]
        let tail = contiguous ? new[last] : head
        return (head.start, tail.end, head.id)
    }

    /// Keep a hand-set start inset / end gap through a retime, scaled by new_len/old_len.
    private static func preserveOffsets(
        start: Double, end: Double, old: SlotWindow, window: (start: Double, end: Double)
    ) -> (Double, Double) {
        let inset = max(0, start - old.start)
        let gap = max(0, old.end - end)
        if inset <= edgeS / 2 && gap <= edgeS / 2 { return (window.start, window.end) }
        let newLength = window.end - window.start
        if newLength <= minBarS { return (window.start, window.end) }
        let oldLength = old.end - old.start
        let ratio = oldLength > 0 ? newLength / oldLength : 1
        let scaledInset = inset > edgeS / 2 ? inset * ratio : 0
        let scaledGap = gap > edgeS / 2 ? gap * ratio : 0
        let outStart = min(window.start + scaledInset, window.end - minBarS)
        let outEnd = max(min(window.end - scaledGap, window.end), outStart + minBarS)
        return (outStart, outEnd)
    }
}
