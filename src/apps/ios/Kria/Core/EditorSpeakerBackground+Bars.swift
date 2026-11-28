import Foundation
import KriaMediaEngine

/// KRI-1656: the Background tool's timeline bars, as pure arithmetic on `EditorSpeakerBackground`.
///
/// The lane shows what renders. A bar is either a timed window, or a visible piece of the fill: the part of the video no
/// window, no `own` layout window and no full-screen window covers. Where the creator takes a bar away the editor puts an
/// `own` layout window (her own take, unmasked and full-frame, KRI-1558) so the video shows instead of a hidden fill.
///
/// Invariants every editing function here keeps (the session calls `normalizeBars` last):
///  1. no window sits under an `own` row (the engine draws `own` over any background, so the window would be invisible);
///  2. no span of the video is left to a hidden fill: a gap shorter than the wire's half second cannot hold an `own` row,
///     so a neighbouring bar absorbs it;
///  3. only `own` rows are rewritten outside the span an edit freed, so planner or chat sections (`full`, `small`,
///     `picture`) elsewhere survive;
///  4. every row stays on the wire's rules (sorted, non-overlapping, at least half a second, at most 40).
struct EditorBackgroundSpan: Equatable, Sendable {
    var start: Double
    var end: Double
    init(_ start: Double, _ end: Double) { self.start = start; self.end = end }
    var length: Double { end - start }
}

extension EditorSpeakerBackground {
    /// The shortest bar this file creates: a hair over the wire's half second (the server compares `end - start < 0.5`
    /// in floating point, so two millisecond-grid times exactly half a second apart can land just under it).
    static let barMinSeconds = SpeakerBackgroundLimits.minWindowSeconds + 0.001
    /// Visible fill pieces shorter than this are not drawn as bars.
    static let barRegionFloor = 0.05
    private static let eps = 1e-6

    // MARK: Ids

    /// Client-local id of the n-th visible fill piece (`fill-<n>`); never written to the wire.
    static func fillRegionID(_ n: Int) -> String { "fill-\(n)" }

    static func isFillRegionID(_ id: String) -> Bool { id.hasPrefix("fill-") }

    /// The next free `bg-<n>` window id.
    static func nextWindowID(in windows: [EditorSpeakerBackgroundWindow]) -> String {
        var n = windows.count
        while windows.contains(where: { $0.localID == localID(n) }) { n += 1 }
        return localID(n)
    }

    static func grid(_ value: Double) -> Double { (value * 1_000).rounded() / 1_000 }
    /// `value` on the millisecond grid, rounded toward `rule` (`.up` for a start that must not fall short of `value`).
    static func grid(_ value: Double, _ rule: FloatingPointRoundingRule) -> Double { (value * 1_000).rounded(rule) / 1_000 }

    /// Where a new bar starts inside `lower...latest`: at `time`, rounded up to the millisecond grid, and never later than
    /// `latest` (rounded down, so the part left after it keeps its length).
    static func barStart(at time: Double, lower: Double, latest: Double) -> Double {
        let wanted = min(max(time, lower), latest)
        return wanted >= latest ? grid(latest, .down) : min(grid(wanted, .up), grid(latest, .down))
    }

    // MARK: Span arithmetic

    /// Sorted, with overlapping and touching spans joined and empty ones dropped.
    static func mergedSpans(_ spans: [EditorBackgroundSpan]) -> [EditorBackgroundSpan] {
        var result: [EditorBackgroundSpan] = []
        for span in spans.filter({ $0.length > eps }).sorted(by: { $0.start < $1.start }) {
            if var last = result.last, span.start <= last.end + eps {
                last.end = max(last.end, span.end)
                result[result.count - 1] = last
            } else {
                result.append(span)
            }
        }
        return result
    }

    /// `spans` with every part inside `cuts` removed.
    static func subtracting(_ cuts: [EditorBackgroundSpan], from spans: [EditorBackgroundSpan]) -> [EditorBackgroundSpan] {
        let cuts = mergedSpans(cuts)
        var result: [EditorBackgroundSpan] = []
        for span in spans {
            var pieces = [span]
            for cut in cuts {
                var next: [EditorBackgroundSpan] = []
                for piece in pieces {
                    if cut.end <= piece.start + eps || cut.start >= piece.end - eps { next.append(piece); continue }
                    if cut.start > piece.start + eps { next.append(EditorBackgroundSpan(piece.start, cut.start)) }
                    if cut.end < piece.end - eps { next.append(EditorBackgroundSpan(cut.end, piece.end)) }
                }
                pieces = next
            }
            result += pieces
        }
        return result.filter { $0.length > eps }
    }

    private var windowSpans: [EditorBackgroundSpan] { windows.map { EditorBackgroundSpan($0.startS, $0.endS) } }
    private var ownSpans: [EditorBackgroundSpan] {
        layoutWindows.filter { $0.layout == .own }.map { EditorBackgroundSpan($0.startS, $0.endS) }
    }
    private var fullScreenSpans: [EditorBackgroundSpan] { fullScreenWindows.map { EditorBackgroundSpan($0.startS, $0.endS) } }

    /// Every span of `0..<duration` no window, `own` row or full-screen window covers: where the fill itself shows.
    func fillSpans(duration: Double) -> [EditorBackgroundSpan] {
        guard duration > Self.eps else { return [] }
        return Self.subtracting(windowSpans + ownSpans + fullScreenSpans, from: [EditorBackgroundSpan(0, duration)])
    }

    /// The visible fill pieces the lane draws as bars, each with its client-local id. Ids count up from `fill-0` and skip
    /// any id a window already carries (a window made from a fill keeps its `fill-<n>` id, so a drag in flight keeps its bar).
    func visibleFillRegions(duration: Double) -> [(id: String, span: EditorBackgroundSpan)] {
        var used = Set(windows.map(\.localID))
        var n = 0
        return fillSpans(duration: duration).filter { $0.length >= Self.barRegionFloor }.map { span in
            while used.contains(Self.fillRegionID(n)) { n += 1 }
            let id = Self.fillRegionID(n)
            used.insert(id)
            n += 1
            return (id, span)
        }
    }

    /// The span of one bar, window or visible fill piece.
    func barSpan(id: String, duration: Double) -> EditorBackgroundSpan? {
        if let window = windows.first(where: { $0.localID == id }) { return EditorBackgroundSpan(window.startS, window.endS) }
        return visibleFillRegions(duration: duration).first { $0.id == id }?.span
    }

    /// True when the lane is made of bars only: windows (and `own` gaps) tile the video, so no media fill of any real length
    /// shows by itself. A whole-video fill, or a chat-made sequence with a tail, is not.
    func isBarsShaped(duration: Double) -> Bool {
        guard mediaID != nil, !windows.isEmpty || layoutWindows.contains(where: { $0.layout == .own }) else { return false }
        return fillSpans(duration: duration).allSatisfy { $0.length < Self.barMinSeconds }
    }

    // MARK: Wire rules

    /// The wire's rules for every list this file edits. With `endsInside` (bars mode, where `normalizeBars` has just trimmed
    /// everything to the edit) windows and layout rows must also end inside it, as the server's commit checks.
    func isWireValid(duration: Double, endsInside: Bool = true) -> Bool {
        (windows.isEmpty || Self.windowsAreOrdered(windows))
            && (layoutWindows.isEmpty || Self.layoutWindowsAreOrdered(layoutWindows))
            && (fullScreenWindows.isEmpty || Self.fullScreenWindowsAreOrdered(fullScreenWindows))
            && (!endsInside || (layoutWindows.allSatisfy { $0.endS <= duration + 1e-6 } && windows.allSatisfy { $0.endS <= duration + 1e-6 }))
    }

    // MARK: Layout rows

    /// Replaces the layout rows: sorted, touching rows of one layout and frame joined, pieces under the wire's half second dropped.
    private mutating func setLayouts(_ rows: [EditorLayoutWindow]) {
        var merged: [EditorLayoutWindow] = []
        for row in rows.sorted(by: { $0.startS < $1.startS }) {
            if var last = merged.last, abs(row.startS - last.endS) <= Self.eps, row.layout == last.layout, row.speakerFrame == last.speakerFrame {
                last.endS = row.endS
                merged[merged.count - 1] = last
            } else {
                merged.append(row)
            }
        }
        layoutWindows = merged.filter { $0.endS - $0.startS >= SpeakerBackgroundLimits.minWindowSeconds }
    }

    /// Her own take over `spans` (clipped to the edit): every layout row inside them gives way, rows outside are kept.
    /// A span shorter than a bar is skipped (`closeSlivers` hands it to a neighbour).
    mutating func coverWithOwn(_ spans: [EditorBackgroundSpan], duration: Double) {
        let clipped = Self.mergedSpans(spans).compactMap { span -> EditorBackgroundSpan? in
            let value = EditorBackgroundSpan(max(0, span.start), min(duration, span.end))
            return value.length >= SpeakerBackgroundLimits.minWindowSeconds ? value : nil
        }
        guard !clipped.isEmpty else { return }
        var rows: [EditorLayoutWindow] = layoutWindows.flatMap { row -> [EditorLayoutWindow] in
            Self.subtracting(clipped, from: [EditorBackgroundSpan(row.startS, row.endS)]).map { piece in
                var copy = row
                copy.startS = piece.start
                copy.endS = piece.end
                return copy
            }
        }
        rows += clipped.map { EditorLayoutWindow(startS: $0.start, endS: $0.end, layout: .own) }
        setLayouts(rows)
    }

    /// Takes `own` coverage off `spans` (rows of any other layout are untouched), so a bar placed there shows.
    mutating func uncoverOwn(_ spans: [EditorBackgroundSpan]) {
        guard !spans.isEmpty, layoutWindows.contains(where: { $0.layout == .own }) else { return }
        let rows: [EditorLayoutWindow] = layoutWindows.flatMap { row -> [EditorLayoutWindow] in
            guard row.layout == .own else { return [row] }
            return Self.subtracting(spans, from: [EditorBackgroundSpan(row.startS, row.endS)]).map { piece in
                var copy = row
                copy.startS = piece.start
                copy.endS = piece.end
                return copy
            }
        }
        setLayouts(rows)
    }

    // MARK: Normalising

    /// Hands every media-fill gap too short to hold an `own` row to the bar next to it: the previous window grows to the
    /// gap's end, else the next window grows back, else a neighbouring `own` row does.
    mutating func closeSlivers(duration: Double) {
        guard duration > Self.eps, mediaID != nil else { return }
        for gap in fillSpans(duration: duration) where gap.length < SpeakerBackgroundLimits.minWindowSeconds {
            if let index = windows.lastIndex(where: { abs($0.endS - gap.start) <= Self.eps }) {
                windows[index].endS = gap.end
            } else if let index = windows.firstIndex(where: { abs($0.startS - gap.end) <= Self.eps }) {
                windows[index].startS = gap.start
            } else if let index = layoutWindows.firstIndex(where: { $0.layout == .own && abs($0.endS - gap.start) <= Self.eps }) {
                layoutWindows[index].endS = gap.end
            } else if let index = layoutWindows.firstIndex(where: { $0.layout == .own && abs($0.startS - gap.end) <= Self.eps }) {
                layoutWindows[index].startS = gap.start
            }
        }
        setLayouts(layoutWindows)
    }

    /// Keeps the hidden fill on something a bar still shows: while no window uses the fill's media, the first window's
    /// media becomes the fill (no extra bytes to load, and nothing shows it).
    mutating func keepFillBehindWindows() {
        guard case .media(let id, let kind, _) = fill, let first = windows.first else { return }
        if windows.contains(where: { $0.mediaID == id && $0.kind == kind }) { return }
        fill = .media(mediaID: first.mediaID, kind: first.kind, sourceStartS: first.sourceStartS)
        focus = nil
    }

    /// Trims every window and layout row to the edit's end (the server refuses layout rows past it).
    mutating func clamp(toDuration duration: Double) {
        guard duration > Self.eps else { return }
        windows = windows.compactMap { window in
            guard window.startS < duration else { return nil }
            var copy = window
            copy.endS = min(window.endS, duration)
            return copy.endS - copy.startS >= SpeakerBackgroundLimits.minWindowSeconds ? copy : nil
        }
        setLayouts(layoutWindows.compactMap { row in
            guard row.startS < duration else { return nil }
            var copy = row
            copy.endS = min(row.endS, duration)
            return copy
        })
    }

    /// The whole "bars mode" repair, run after every edit: windows are never hidden under `own`, slivers are closed, the
    /// hidden fill still has a bar behind it, and nothing runs past the edit.
    mutating func normalizeBars(duration: Double) {
        clamp(toDuration: duration)
        uncoverOwn(windowSpans)
        closeSlivers(duration: duration)
        keepFillBehindWindows()
    }

    // MARK: Fill pieces to windows

    /// Turns every visible media-fill piece of bar length into a window of its own (same media, same aim, id `fill-<n>`), so
    /// the piece can be trimmed, moved or replaced like any window. False when that would pass the 40-window cap.
    /// A colour fill has no windows; its pieces stay pieces.
    mutating func materializeMediaFill(duration: Double) -> Bool {
        guard case .media(let id, let kind, let sourceStart) = fill else { return true }
        let regions = visibleFillRegions(duration: duration).filter { $0.span.length >= Self.barMinSeconds }
        guard !regions.isEmpty else { return true }
        guard windows.count + regions.count <= SpeakerBackgroundLimits.maxWindows else { return false }
        for region in regions {
            windows.append(EditorSpeakerBackgroundWindow(localID: region.id, mediaID: id, kind: kind, sourceStartS: sourceStart,
                                                         startS: region.span.start, endS: region.span.end, focus: focus))
        }
        windows.sort { $0.startS < $1.startS }
        return true
    }

    // MARK: Adding a bar

    /// The aim an existing window, or the fill, already has for this media, so a second shot of the same recording keeps it.
    func knownFocus(mediaID: String, kind: VisualMediaKind) -> SpeakerBackgroundFocus? {
        windows.first(where: { $0.mediaID == mediaID && $0.kind == kind && $0.focus != nil })?.focus
            ?? (self.mediaID == mediaID && self.kind == kind ? focus : nil)
    }

    /// Clears the span a new bar (or a colour) takes from `time` (edit seconds) and returns it. Inside a window the old window
    /// keeps the part before `time` and the new span is the rest of it; in a gap the span runs from `time` to the gap's end
    /// (the next window, or the end of the video). Closer than a bar to where the old window or gap starts, the span takes
    /// all of it (an old window is then removed). Nil, with nothing changed, when no bar-length span is there.
    mutating func reserveSpan(at time: Double, duration: Double) -> EditorBackgroundSpan? {
        let minimum = Self.barMinSeconds
        guard duration >= minimum, time.isFinite else { return nil }
        let t = min(max(0, time), duration - 0.001)
        if let index = windows.firstIndex(where: { $0.startS <= t && t < $0.endS }) {
            let old = windows[index]
            // A window too short to split (the wire allows exactly half a second) is replaced whole.
            let split = Self.barStart(at: t, lower: old.startS, latest: old.endS - minimum)
            if split - old.startS >= minimum {
                windows[index].endS = split
                return EditorBackgroundSpan(split, old.endS)
            }
            // Closer than a bar to the start: the new span replaces the whole window.
            windows.remove(at: index)
            return EditorBackgroundSpan(old.startS, old.endS)
        }
        guard let gap = Self.subtracting(windowSpans, from: [EditorBackgroundSpan(0, duration)]).first(where: { $0.start <= t && t < $0.end }),
              gap.length >= minimum else { return nil }
        let start = t - gap.start < minimum ? gap.start : Self.barStart(at: t, lower: gap.start, latest: gap.end - minimum)
        return EditorBackgroundSpan(start, gap.end)
    }

    /// Adds one bar from `time` (see `reserveSpan`). With `bars` the visible media fill is turned into windows first and
    /// the span under the new bar loses its `own` row. Returns the new bar's id, or nil (nothing changed) when no bar-length
    /// span is left or the 40-window cap would be passed.
    mutating func addBar(mediaID: String, kind: VisualMediaKind, at time: Double, duration: Double, bars: Bool) -> String? {
        var next = self
        if bars { guard next.materializeMediaFill(duration: duration) else { return nil } }
        let aim = next.knownFocus(mediaID: mediaID, kind: kind)
        let id = Self.nextWindowID(in: next.windows)
        guard let span = next.reserveSpan(at: time, duration: duration), span.length >= SpeakerBackgroundLimits.minWindowSeconds,
              next.windows.count < SpeakerBackgroundLimits.maxWindows else { return nil }
        next.windows.append(EditorSpeakerBackgroundWindow(localID: id, mediaID: mediaID, kind: kind, sourceStartS: 0,
                                                         startS: span.start, endS: span.end, focus: aim))
        next.windows.sort { $0.startS < $1.startS }
        if bars { next.uncoverOwn([span]) }
        self = next
        return id
    }

    // MARK: Removing and replacing

    /// Takes one bar away. A window is removed; a visible fill piece is covered with `own`. With `bars` the span that was
    /// freed shows her own take. False when the id names nothing.
    mutating func removeBar(id: String, duration: Double, bars: Bool) -> Bool {
        if let index = windows.firstIndex(where: { $0.localID == id }) {
            let span = EditorBackgroundSpan(windows[index].startS, windows[index].endS)
            windows.remove(at: index)
            if bars { coverWithOwn([span], duration: duration) }
            return true
        }
        guard bars, let region = visibleFillRegions(duration: duration).first(where: { $0.id == id }) else { return false }
        coverWithOwn([region.span], duration: duration)
        return true
    }
}
