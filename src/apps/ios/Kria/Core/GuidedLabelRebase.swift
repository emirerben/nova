import Foundation

/// Phone-side port of the server's `rebase_guided_text` for the per-clip label
/// lane (`services/kria_editor_timeline.py`: `_follow_window`,
/// `_preserve_label_offsets`, `_is_untouched`).
///
/// A guided story times each clip's label bar on absolute output windows, so a
/// trim / extend / reorder / delete used to strand every later label at its old
/// time (the editor retimed only the slot). This pure function re-windows ONLY
/// the clip-bound label bars (`clip-label-*`) onto the slot they follow. Captions,
/// narration labels, lyric bars, titles and the creator's own text are returned
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

    /// One active slot's output window on the editor's base clock (the same walk the
    /// timeline renders, including transition overlap).
    struct SlotWindow: Equatable {
        let id: String
        let parentID: String
        let mediaID: String?
        let start: Double
        let end: Double
    }

    static func windows(of slots: [EditorTimelineSlot]) -> [SlotWindow] {
        NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil).baseClipWindows.map { window in
            let slot = slots[window.sourceIndex]
            return SlotWindow(
                id: slot.id ?? "", parentID: slot.parentSegmentID ?? "",
                mediaID: slot.raw["media_id"]?.stringValue,
                start: window.start, end: window.end
            )
        }
    }

    /// True when the label lane has anything to follow.
    static func hasLabels(_ elements: [EditorTextElement]) -> Bool {
        elements.contains(where: isClipLabel)
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
        var result: [EditorTextElement] = []
        for element in elements {
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
