import Foundation

/// Phone-side port of the server's `rebase_guided_text` (`services/kria_editor_timeline.py`:
/// `_follow_window`, `_preserve_label_offsets`, `_project_span`, `_is_untouched`).
///
/// A guided story times every text on absolute output seconds, so a trim / extend / reorder /
/// delete used to strand text at its old time (the editor retimed only the slots). This pure
/// function re-times, onto the new slots:
///  - clip-bound text (`clip-label-*`, `montage-text-*`): re-windowed onto the clip it follows;
///  - guided titles and pins: re-anchored to the new timeline;
///  - every other text (chat / creator text, group titles, ...): projected through SOURCE time,
///    the way the server projects its other lanes. Its start moves to the earliest surviving
///    point, its end is anchored one frame early and moved back, and it only disappears when
///    every clip under it is gone.
/// Caption cues, narration labels (non-participant) and lyric bars are returned untouched, and no
/// text, style or position field ever changes.
///
/// Parity is pinned by `Tests/Fixtures/guided_label_rebase_vectors.json`, which the
/// server's own `rebase_guided_text` generates and both sides assert.
///
/// Documented phone-only deviations (the server cannot see what the phone cannot):
///  - A text whose old range overlaps no clip at all (already stale) is left where it is; the
///    server drops it. Dropping a creator's text because of an old inconsistency is the worse failure.
///  - A `clip-label-<cut>` / `montage-text-<cut>` bar with no `segment_id` and no media link
///    (the editor's slots carry no `media_id`, and the cut-to-media table lives on the server)
///    is treated as ordinary timed text and projected, where the server resolves it through the
///    approved proposal.
enum GuidedLabelRebase {
    static let minBarS: Double = 0.2
    static let edgeS: Double = 0.05
    /// A bar that carries a segment_id is only clip-bound if it still fills that segment.
    static let segmentFitToleranceS: Double = 0.15
    static let labelPrefix = "clip-label-"
    static let labelMediaPrefix = "clip-label-media-"
    /// Per-shot montage copy is bound to one clip exactly like a label
    /// (server `_GUIDED_CLIP_TEXT_PREFIXES`; keep in step).
    static let montageTextPrefix = "montage-text-"
    static let openingTitleID = "guided-title"
    static let closingTitleID = "guided-closing-title"
    /// KRI-523: whole-video corner text (`guided-pinned-<i>`) rides the timeline like a title.
    /// KRI-525: a RANGED pin that fills its clip follows that clip like a label; any other
    /// ranged pin goes through the same time rules as a title (`rebaseTimed`).
    static let pinnedPrefix = "guided-pinned-"

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

    /// `mediaByClipIndex` fills in the media id for slots that carry none: the editor's guided
    /// timeline slots name their source by `clip_index` only, so `clip-label-media-<media_id>`
    /// bars could not be matched to a clip without it.
    static func windows(of slots: [EditorTimelineSlot], mediaByClipIndex: [Int: String] = [:]) -> [SlotWindow] {
        NativeEditorInteraction.timelineProjection(slots: slots, carousel: nil).baseClipWindows.map { window in
            let slot = slots[window.sourceIndex]
            return SlotWindow(
                id: slot.id ?? "", parentID: slot.parentSegmentID ?? "",
                mediaID: slot.raw["media_id"]?.stringValue ?? mediaByClipIndex[slot.clipIndex],
                start: window.start, end: window.end,
                sourceStart: slot.inS,
                sourceEnd: slot.inS + (window.end - window.start) * (slot.raw["playback_rate"]?.numberValue ?? 1)
            )
        }
    }

    /// True when the text lane has anything to follow. `includeFreeText` is true for a guided
    /// story, where every text rides the timeline; anywhere else only the guided ids do.
    /// Required (no default) so a caller can never disagree with `rebase` about the archetype.
    static func hasLabels(_ elements: [EditorTextElement], includeFreeText: Bool) -> Bool {
        elements.contains { isGuidedText($0, includeFreeText: includeFreeText) }
    }

    static func isGuidedText(_ element: EditorTextElement, includeFreeText: Bool) -> Bool {
        if isUntouched(element) { return false }
        if isClipLabel(element) || isAnchoredTitle(element) { return true }
        return includeFreeText && !element.isReadOnly
    }

    private static func isAnchoredTitle(_ element: EditorTextElement) -> Bool {
        element.id == openingTitleID || element.id == closingTitleID || element.id.hasPrefix(pinnedPrefix)
    }

    static func isClipLabel(_ element: EditorTextElement) -> Bool {
        element.id.hasPrefix(labelPrefix) && !isUntouched(element)
    }

    /// Per-shot montage copy. Clip-bound like a label, but only on a guided story: other
    /// archetypes' `montage-text-*` bars keep their own timing.
    static func isMontageText(_ element: EditorTextElement) -> Bool {
        element.id.hasPrefix(montageTextPrefix) && !isUntouched(element)
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
        newSlots: [EditorTimelineSlot],
        mediaByClipIndex: [Int: String] = [:],
        includeFreeText: Bool
    ) -> [EditorTextElement] {
        let old = windows(of: oldSlots, mediaByClipIndex: mediaByClipIndex)
        let new = windows(of: newSlots, mediaByClipIndex: mediaByClipIndex)
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
                if let clipPin = clipBoundPin(element, old: old, oldTotal: oldTotal) {
                    // Server parity: a clip-bound bar whose clip is gone is dropped.
                    guard let target = follow(clipPin, media: nil, in: new) else { continue }
                    var updated = element
                    updated.startS = round6(target.start)
                    updated.endS = round6(target.end)
                    updated.raw["segment_id"] = .string(target.id)
                    result.append(updated)
                    continue
                }
                if let title = rebaseTimed(element, old: old, new: new, oldTotal: oldTotal, newTotal: newTotal, anchoredByID: true) {
                    result.append(title)
                }
                continue
            }
            guard isClipLabel(element) || (includeFreeText && isMontageText(element)) else {
                guard includeFreeText, !element.isReadOnly else { result.append(element); continue }
                if let timed = rebaseFreeText(element, old: old, new: new, oldTotal: oldTotal, newTotal: newTotal) {
                    result.append(timed)
                }
                continue
            }
            var media: String?
            if element.id.hasPrefix(labelMediaPrefix) {
                let rest = String(element.id.dropFirst(labelMediaPrefix.count))
                media = rest.isEmpty ? nil : rest
            }
            let oldWindow = identify(element, media: media, in: old)
            // A montage text the creator stretched past its clip is a span, not a clip-bound bar
            // (server: `rebase_guided_text` escape test).
            let stretched = isMontageText(element) && oldWindow.map {
                element.startS < $0.start - segmentFitToleranceS || element.endS > $0.end + segmentFitToleranceS
            } == true
            if stretched || (oldWindow == nil && media == nil) {
                // No segment_id and no media link: nothing ties this bar to a clip, so it is
                // ordinary timed text (see the deviations in the type comment).
                guard includeFreeText, !element.isReadOnly else { result.append(element); continue }
                if let timed = rebaseFreeText(element, old: old, new: new, oldTotal: oldTotal, newTotal: newTotal) {
                    result.append(timed)
                }
                continue
            }
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

    /// Ordinary text (chat / creator text, group titles): travels with the clip it exactly fills,
    /// otherwise is projected through source time. Server `rebase_guided_text`'s non-label path.
    private static func rebaseFreeText(
        _ element: EditorTextElement, old: [SlotWindow], new: [SlotWindow], oldTotal: Double, newTotal: Double
    ) -> EditorTextElement? {
        if let segmentID = element.raw["segment_id"]?.stringValue, !segmentID.isEmpty,
           let candidate = old.first(where: { $0.id == segmentID }),
           abs(element.startS - candidate.start) <= segmentFitToleranceS,
           abs(element.endS - candidate.end) <= segmentFitToleranceS {
            guard let target = follow(candidate, media: nil, in: new) else { return nil }
            var updated = element
            updated.startS = round6(target.start)
            updated.endS = round6(target.end)
            updated.raw["segment_id"] = .string(target.id)
            return updated
        }
        return rebaseTimed(element, old: old, new: new, oldTotal: oldTotal, newTotal: newTotal, anchoredByID: false)
    }

    /// The old slot a ranged pin is bound to, or nil when the pin is anchored (spans the whole
    /// video), not a pin, or no longer fills the segment it names (`rebase_guided_text`).
    private static func clipBoundPin(
        _ element: EditorTextElement, old: [SlotWindow], oldTotal: Double
    ) -> SlotWindow? {
        guard element.id.hasPrefix(pinnedPrefix) else { return nil }
        let spansWholeVideo = element.startS <= edgeS && oldTotal > 0 && element.endS >= oldTotal - edgeS
        guard !spansWholeVideo,
              let segmentID = element.raw["segment_id"]?.stringValue, !segmentID.isEmpty,
              let window = old.first(where: { $0.id == segmentID }),
              abs(element.startS - window.start) <= segmentFitToleranceS,
              abs(element.endS - window.end) <= segmentFitToleranceS
        else { return nil }
        return window
    }

    /// Match `rebase_guided_text`: actual edge positions determine anchoring;
    /// manually inset titles travel through source time, including split children.
    private static func rebaseTimed(
        _ element: EditorTextElement, old: [SlotWindow], new: [SlotWindow], oldTotal: Double, newTotal: Double,
        anchoredByID: Bool
    ) -> EditorTextElement? {
        let start = element.startS, end = element.endS
        let opening = start <= edgeS
        let closing = oldTotal > 0 && end >= oldTotal - edgeS
        let frameS = 1.0 / 30.0
        var newStart: Double
        var newEnd: Double
        var updated = element
        // Edge anchoring belongs to titles and pins. Ordinary text that merely touches an edge
        // (a group title over the first clips) follows its footage; only text spanning the
        // WHOLE video keeps tracking the whole video.
        if (opening && closing) || ((opening || closing) && anchoredByID) {
            if opening && closing {
                newStart = 0; newEnd = newTotal
            } else if opening {
                newStart = start; newEnd = min(end, newTotal)
            } else {
                newEnd = newTotal; newStart = max(0, newTotal - (end - start))
            }
            if end - start >= minBarS && newEnd - newStart < minBarS {
                newEnd = min(newTotal, newStart + minBarS)
                newStart = max(0, newEnd - minBarS)
            }
        } else {
            guard let (a, b) = projectSpan(start, end, old: old, new: new) else {
                // Every clip under the text is gone: drop it. A text over no clip at all was
                // already stale: leave it (phone-only deviation).
                return old.contains(where: { overlap(start, end, $0.start, $0.end) > 0.000001 }) ? nil : element
            }
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

    /// The server's `_project_span`: the start and (one-frame-early) end of `[start, end)` on the
    /// new clips. When an edge sits in a deleted clip the range survives as long as ANY clip under
    /// it does: it re-anchors to the earliest start and latest end of the surviving pieces.
    private static func projectSpan(
        _ start: Double, _ end: Double, old: [SlotWindow], new: [SlotWindow]
    ) -> ((time: Double, id: String), (time: Double, id: String))? {
        let frameS = 1.0 / 30.0
        if let first = project(start, old: old, new: new),
           let last = project(max(start, end - frameS), old: old, new: new) {
            return (first, last)
        }
        var heads: [(time: Double, id: String)] = []
        var tails: [(time: Double, id: String)] = []
        for window in old {
            let lo = max(start, window.start), hi = min(end, window.end)
            if hi - lo <= 0.000001 { continue }
            if let head = project(lo, old: old, new: new), let tail = project(max(lo, hi - frameS), old: old, new: new) {
                heads.append(head); tails.append(tail)
            }
        }
        // First minimum / first maximum, like Python's min()/max().
        guard var head = heads.first, var tail = tails.first else { return nil }
        for candidate in heads.dropFirst() where candidate.time < head.time { head = candidate }
        for candidate in tails.dropFirst() where candidate.time > tail.time { tail = candidate }
        return (head, tail)
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
