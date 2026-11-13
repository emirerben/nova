import Foundation

/// Canvas lanes follow serial timeline units; changing one panel never moves its peers or canvas lanes.
enum NativeSplitTimelineRebase {
    private struct Unit: Equatable {
        let ids: Set<String>
        var start: Double
        var end: Double
    }

    private static func units(_ document: EditorDocument) -> [Unit] {
        let projection = NativeEditorInteraction.timelineProjection(slots: document.clips, carousel: nil, splitSections: document.splitScreenSections)
        var result: [Unit] = [], seen = Set<String>()
        for window in projection.baseClipWindows {
            let slot = document.clips[window.sourceIndex]
            if let id = slot.splitSectionID, let section = document.splitScreenSections.first(where: { $0.id == id }) {
                guard seen.insert(id).inserted else { continue }
                let start = window.start - slot.splitOffsetS
                result.append(Unit(ids: Set(document.clips.filter { $0.splitSectionID == id }.compactMap(\.id)), start: start, end: start + section.durationS))
            } else {
                result.append(Unit(ids: Set([slot.id].compactMap { $0 }), start: window.start, end: window.end))
            }
        }
        return result
    }

    static func rebase(_ next: inout EditorDocument, from previous: EditorDocument) {
        guard !previous.splitScreenSections.isEmpty || !next.splitScreenSections.isEmpty else { return }
        let speech = previous.clips.contains { $0.id?.hasPrefix("native-phone-talking-source") == true }
        let old = units(previous), new = units(next)
        guard old != new else { return }
        let oldEnd = old.last?.end ?? 0, newEnd = new.last?.end ?? 0
        func target(_ unit: Unit) -> (start: Double, end: Double)? {
            let matches = new.filter { !$0.ids.isDisjoint(with: unit.ids) }
            guard let first = matches.map(\.start).min(), let last = matches.map(\.end).max() else { return nil }
            return (first, last)
        }
        func point(_ time: Double) -> Double {
            if time >= oldEnd { return max(0, newEnd + time - oldEnd) }
            guard let unit = old.last(where: { $0.start <= time && time < $0.end }) else { return min(time, newEnd) }
            guard let destination = target(unit) else {
                let following = old.filter { $0.start >= unit.end }.compactMap(target).first
                return following?.start ?? newEnd
            }
            return min(destination.end, destination.start + max(0, time - unit.start))
        }
        func interval(_ start: Double, _ end: Double) -> (Double, Double) {
            guard end > start else { let t = point(start); return (t, t) }
            let parts = old.compactMap { unit -> (Double, Double)? in
                guard unit.start < end, unit.end > start, let destination = target(unit) else { return nil }
                return (min(destination.end, destination.start + max(0, start - unit.start)),
                        min(destination.end, destination.start + min(unit.end - unit.start, end - unit.start)))
            }
            guard let a = parts.map({ $0.0 }).min(), let b = parts.map({ $0.1 }).max() else { let t = point(start); return (t, t) }
            return (a, max(a, end > oldEnd ? newEnd + end - oldEnd : b))
        }
        remap(&next, speech: speech, point: point, interval: interval)
    }

    /// The timeline lost `spans` (base seconds, disjoint). Every lane after a span moves earlier by its length and a
    /// lane inside one shrinks to the span's start, so a lane wholly inside it goes with it.
    static func removeTime(_ spans: [(start: Double, end: Double)], from next: inout EditorDocument) {
        let spans = spans.filter { $0.end > $0.start }
        guard !spans.isEmpty else { return }
        func point(_ time: Double) -> Double { time - spans.reduce(0) { $0 + max(0, min(time, $1.end) - $1.start) } }
        let speech = next.clips.contains { $0.id?.hasPrefix("native-phone-talking-source") == true }
        remap(&next, speech: speech, point: point) { (point($0), point($1)) }
    }

    private static func remap(_ next: inout EditorDocument, speech: Bool, point: (Double) -> Double,
                              interval: (Double, Double) -> (Double, Double)) {
        func owns(_ raw: [String: JSONValue]) -> Bool { !speech || NativeSpeechTimelineProjection.ownerID(raw) == nil }
        for i in next.captionCues.indices where !speech {
            let row = next.captionCues[i], span = interval(row.startS, row.endS)
            next.captionCues[i].startS = span.0; next.captionCues[i].endS = span.1
            if case .array(let words)? = row.raw["words"] {
                next.captionCues[i].raw["words"] = .array(words.map { word in
                    guard var raw = word.objectValue, let a = raw["start_s"]?.numberValue, let b = raw["end_s"]?.numberValue else { return word }
                    let moved = interval(a, b); raw["start_s"] = .number(moved.0); raw["end_s"] = .number(moved.1)
                    return .object(raw)
                })
            }
        }
        for i in next.soundEffects.indices where owns(next.soundEffects[i].raw) {
            if let t = next.soundEffects[i].pointS { next.soundEffects[i].pointS = point(t) }
            let span = interval(next.soundEffects[i].startS, next.soundEffects[i].endS)
            next.soundEffects[i].startS = span.0; next.soundEffects[i].endS = span.1
        }
        for i in next.mediaOverlays.indices where owns(next.mediaOverlays[i].raw) { let span = interval(next.mediaOverlays[i].startS, next.mediaOverlays[i].endS); next.mediaOverlays[i].startS = span.0; next.mediaOverlays[i].endS = span.1 }
        for i in next.visualBlocks.indices where owns(next.visualBlocks[i].raw) { let span = interval(next.visualBlocks[i].startS, next.visualBlocks[i].endS); next.visualBlocks[i].startS = span.0; next.visualBlocks[i].endS = span.1 }
        for i in next.motionScenes.indices where owns(next.motionScenes[i].raw) { let span = interval(next.motionScenes[i].startS, next.motionScenes[i].endS); next.motionScenes[i].startS = span.0; next.motionScenes[i].endS = span.1 }
        for i in next.cameraEffects.indices where owns(next.cameraEffects[i].raw) { let span = interval(next.cameraEffects[i].startS, next.cameraEffects[i].endS); next.cameraEffects[i].startS = span.0; next.cameraEffects[i].endS = span.1 }
        for i in next.drawings.indices { let span = interval(next.drawings[i].startS, next.drawings[i].endS); next.drawings[i].startS = span.0; next.drawings[i].endS = span.1 }
        if speech {
            for i in next.textElements.indices where owns(next.textElements[i].raw) {
                let span = interval(next.textElements[i].startS, next.textElements[i].endS)
                next.textElements[i].startS = span.0; next.textElements[i].endS = span.1
            }
        }
        for i in next.clipAudio.indices { next.clipAudio[i].startS = point(next.clipAudio[i].startS) }
        if var background = next.speakerBackground {
            for i in background.windows.indices { let span = interval(background.windows[i].startS, background.windows[i].endS); background.windows[i].startS = span.0; background.windows[i].endS = span.1 }
            for i in background.fullScreenWindows.indices { let span = interval(background.fullScreenWindows[i].startS, background.fullScreenWindows[i].endS); background.fullScreenWindows[i].startS = span.0; background.fullScreenWindows[i].endS = span.1 }
            background.windows.removeAll { !$0.isValid }
            background.fullScreenWindows.removeAll { !$0.isValid }
            background.windows.sort { $0.startS < $1.startS }; background.fullScreenWindows.sort { $0.startS < $1.startS }
            next.speakerBackground = background
        }
        next.captionCues.removeAll { $0.endS <= $0.startS }
        next.mediaOverlays.removeAll { $0.endS <= $0.startS }
        next.visualBlocks.removeAll { $0.endS <= $0.startS }
        next.motionScenes.removeAll { $0.endS <= $0.startS }
        next.cameraEffects.removeAll { $0.endS <= $0.startS }
        next.drawings.removeAll { $0.endS <= $0.startS }
    }
}
