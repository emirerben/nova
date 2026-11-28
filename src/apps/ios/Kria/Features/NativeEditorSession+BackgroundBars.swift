import Foundation
import KriaMediaEngine
import SwiftUI

/// KRI-1656: the Background tool's timeline bars. Every background is a bar on the BACKGROUND lane: a timed window, or a
/// visible piece of the fill. The pure arithmetic is `EditorSpeakerBackground+Bars.swift`; this file is the session's side:
/// which gate opens which behaviour, the playhead, one undo step per edit, and the messages when an edit cannot be made.
///
/// Bars mode (`canEditSpeakerSections`, protocol 59): a span no bar covers shows her own take (an `own` layout window).
/// Without it the older model stays: the fill is the one bar under the timed windows, shown but not trimmed.
extension NativeEditorSession {
    /// Gaps between bars show her own take, and fill pieces can be trimmed, moved and replaced.
    var backgroundBarsMode: Bool { canEditSpeakerSections }

    /// The span the lane covers: the end of the cut timeline.
    var backgroundBarDuration: Double { backgroundWindowsCeiling }

    /// The playhead in the same cut-timeline seconds the windows are written in.
    private var backgroundPlayheadSeconds: Double { timelineProjection.unprojectOutputTime(currentTime) }

    /// Bars for the lane: each visible piece of the fill, with its id (`fill-<n>`).
    var backgroundFillRegions: [(id: String, span: EditorBackgroundSpan)] {
        document.speakerBackground?.visibleFillRegions(duration: backgroundBarDuration) ?? []
    }

    /// The pool visual a bar shows: its window's media, or the fill's. Nil for a colour piece.
    func backgroundBarMedia(id: String) -> (mediaID: String, kind: VisualMediaKind)? {
        guard let background = document.speakerBackground else { return nil }
        if let window = background.windows.first(where: { $0.localID == id }) { return (window.mediaID, window.kind) }
        guard let mediaID = background.mediaID, let kind = background.kind,
              backgroundFillRegions.contains(where: { $0.id == id }) else { return nil }
        return (mediaID, kind)
    }

    /// True while a bar with this id (window or fill piece) exists.
    func backgroundBarExists(id: String) -> Bool {
        backgroundBarMedia(id: id) != nil || (document.speakerBackground?.color != nil && backgroundFillRegions.contains { $0.id == id })
    }

    /// The pool item lit in the Background tool: the media of the bar under the playhead.
    var backgroundMediaAtPlayhead: String? {
        guard let background = document.speakerBackground else { return nil }
        let t = backgroundPlayheadSeconds
        if let window = background.windows.first(where: { $0.startS <= t && t < $0.endS }) { return window.mediaID }
        let underFill = backgroundFillRegions.contains { $0.span.start <= t && t < $0.span.end }
        return underFill ? background.mediaID : nil
    }

    /// Whether Remove is offered for this bar: a window with the timed controls, a fill piece in bars mode (its span then shows her
    /// own take) or, without sections, the fill itself while nothing sits over it.
    func canRemoveBackgroundBar(id: String) -> Bool {
        if document.speakerBackground?.windows.contains(where: { $0.localID == id }) == true { return canEditSpeakerBackgroundWindows }
        return canEditSpeakerBackground && (backgroundBarsMode || (document.speakerBackground?.windows.isEmpty ?? false))
    }

    /// Whether Replace is offered: a window needs the timed controls; a media piece of the fill can always be swapped, a colour
    /// piece needs them (its replacement becomes a window).
    func canReplaceBackgroundBar(id: String) -> Bool {
        if document.speakerBackground?.windows.contains(where: { $0.localID == id }) == true { return canEditSpeakerBackgroundWindows }
        return canEditSpeakerBackground && (document.speakerBackground?.color == nil || canEditSpeakerBackgroundWindows)
    }

    /// Whether the lane may move or trim this bar: a window follows the timed-background gate, a visible piece of the fill
    /// needs bars mode (the span it gives up has to be able to show her own take).
    func canEditBackgroundBarTiming(id: String) -> Bool {
        if document.speakerBackground?.windows.contains(where: { $0.localID == id }) == true { return canEditSpeakerBackgroundWindows }
        return backgroundBarsMode
    }

    private func commitBackgroundBars(_ next: EditorSpeakerBackground, bars: Bool) -> Bool {
        var value = next
        let duration = backgroundBarDuration
        if bars { value.normalizeBars(duration: duration) }
        guard value.isWireValid(duration: duration, endsInside: bars) else {
            visualError = String(localized: "That’s as many background changes as one video can hold.")
            return false
        }
        // Nothing left to show: the lane is empty, so the background goes (as "None" does).
        if bars, value.windows.isEmpty, value.fillSpans(duration: duration).allSatisfy({ $0.length < EditorSpeakerBackground.barMinSeconds }) {
            return removeSpeakerBackground()
        }
        guard value != document.speakerBackground else { return true }
        transactDocument(section: .speakerBackground) { $0.speakerBackground = value }
        return true
    }

    // MARK: Add

    /// Adds one pool photo or video as a bar from the playhead: inside a bar it splits that bar there, in a gap it fills from
    /// the playhead to the gap's end. With no background yet the bar covers the whole video. Where the timed controls are
    /// closed, it replaces the single fill as before. One undo step.
    @discardableResult
    func addSpeakerBackground(mediaID: String, kind: VisualMediaKind) -> Bool {
        guard canEditSpeakerBackground else { return false }
        guard canEditSpeakerBackgroundWindows else { return setSpeakerBackground(mediaID: mediaID, kind: kind) }
        let bars = backgroundBarsMode
        guard document.speakerBackground != nil || bars else { return setSpeakerBackground(mediaID: mediaID, kind: kind) }
        var next = document.speakerBackground ?? EditorSpeakerBackground(mediaID: mediaID, kind: kind)
        guard next.addBar(mediaID: mediaID, kind: kind, at: document.speakerBackground == nil ? 0 : backgroundPlayheadSeconds,
                          duration: backgroundBarDuration, bars: bars) != nil else {
            visualError = String(localized: "There isn’t room for another background here. Move the playhead, or make a bar shorter.")
            return false
        }
        return commitBackgroundBars(next, bars: bars)
    }

    // MARK: Replace

    /// Swaps the picture or video of one bar for another pool item. The bar keeps its span; a video starts from its beginning.
    @discardableResult
    func replaceSpeakerBackground(barID: String, mediaID: String, kind: VisualMediaKind) -> Bool {
        guard canEditSpeakerBackground, var next = document.speakerBackground else { return false }
        let duration = backgroundBarDuration
        let bars = backgroundBarsMode
        if let index = next.windows.firstIndex(where: { $0.localID == barID }) {
            guard canEditSpeakerBackgroundWindows else { return false }
            next.windows[index].mediaID = mediaID
            next.windows[index].kind = kind
            next.windows[index].sourceStartS = 0
            next.windows[index].focus = next.knownFocus(mediaID: mediaID, kind: kind)
            return commitBackgroundBars(next, bars: bars)
        }
        guard let region = next.visibleFillRegions(duration: duration).first(where: { $0.id == barID }) else { return false }
        if next.color != nil {
            // A colour piece has no media to swap: the new picture takes its span as a window.
            guard canEditSpeakerBackgroundWindows,
                  region.span.length >= EditorSpeakerBackground.barMinSeconds,
                  next.windows.count < SpeakerBackgroundLimits.maxWindows else { return false }
            next.windows.append(EditorSpeakerBackgroundWindow(localID: EditorSpeakerBackground.nextWindowID(in: next.windows), mediaID: mediaID, kind: kind,
                                                              sourceStartS: 0, startS: region.span.start, endS: region.span.end,
                                                              focus: next.knownFocus(mediaID: mediaID, kind: kind)))
            next.windows.sort { $0.startS < $1.startS }
            return commitBackgroundBars(next, bars: bars)
        }
        guard bars else { return setSpeakerBackground(mediaID: mediaID, kind: kind) }
        guard next.materializeMediaFill(duration: duration),
              let index = next.windows.firstIndex(where: { $0.localID == barID }) else { return false }
        next.windows[index].mediaID = mediaID
        next.windows[index].kind = kind
        next.windows[index].sourceStartS = 0
        next.windows[index].focus = next.knownFocus(mediaID: mediaID, kind: kind)
        return commitBackgroundBars(next, bars: bars)
    }

    // MARK: Remove

    /// Takes one bar away. In bars mode the freed span shows her own take. A bar the older model cannot free (the fill under
    /// other backgrounds) is left alone: false.
    @discardableResult
    func removeSpeakerBackgroundBar(id: String) -> Bool {
        guard canEditSpeakerBackground, var next = document.speakerBackground else { return false }
        let bars = backgroundBarsMode
        let isWindow = next.windows.contains { $0.localID == id }
        if !isWindow && !bars {
            // The fill itself, with no sections: it goes only when nothing sits over it.
            guard next.windows.isEmpty, backgroundFillRegions.contains(where: { $0.id == id }) else { return false }
            return removeSpeakerBackground()
        }
        guard (!isWindow || canEditSpeakerBackgroundWindows), next.removeBar(id: id, duration: backgroundBarDuration, bars: bars) else { return false }
        if let selected = selection, selected.kind == .speakerBackgroundWindow, selected.id == id { select(nil) }
        return commitBackgroundBars(next, bars: bars)
    }

    // MARK: Colour from the playhead

    /// Sets the background colour from the playhead: the span from there to the end of the bar or gap it lands in shows the
    /// colour (a bar there is shortened, an `own` gap is covered). A colour is one fill, so every colour piece takes it. With
    /// no background the whole video is that colour. Without sections it replaces the fill as before.
    @discardableResult
    func setSpeakerBackgroundColorFromPlayhead(hex: String) -> Bool {
        guard canEditSpeakerBackgroundColor, let color = EditorSpeakerBackground.normalizedColor(hex) else { return false }
        guard backgroundBarsMode else { return setSpeakerBackgroundColor(hex: color) }
        guard var next = document.speakerBackground else {
            transactDocument(section: .speakerBackground) { $0.speakerBackground = EditorSpeakerBackground(color: color) }
            return true
        }
        let duration = backgroundBarDuration
        guard next.materializeMediaFill(duration: duration) else {
            visualError = String(localized: "That’s as many background changes as one video can hold.")
            return false
        }
        if next.color != nil, next.fillSpans(duration: duration).contains(where: { $0.start <= backgroundPlayheadSeconds && backgroundPlayheadSeconds < $0.end }) {
            // Already colour here: recolour only.
            next.fill = .color(color)
            return commitBackgroundBars(next, bars: true)
        }
        guard let span = next.reserveSpan(at: backgroundPlayheadSeconds, duration: duration) else {
            visualError = String(localized: "There isn’t room for a colour here. Move the playhead, or make a bar shorter.")
            return false
        }
        next.fill = .color(color)
        next.focus = nil
        next.uncoverOwn([span])
        return commitBackgroundBars(next, bars: true)
    }

    // MARK: Crop (a focus on the wire)

    /// The crop the creator sees for one bar, as the focus it is stored as: a window's own, else the fill's.
    func speakerBackgroundFocus(barID: String) -> SpeakerBackgroundFocus? {
        guard let background = document.speakerBackground else { return nil }
        if let window = background.windows.first(where: { $0.localID == barID }) { return window.focus }
        return background.focus
    }

    /// Writes one bar's crop. `nil` clears it (the centred cover-fit). The whole focus is written, so a drag that moves the
    /// crop up or down is kept. Closed while the server does not open `background_focus`. One undo step per gesture.
    @discardableResult
    func setSpeakerBackgroundFocus(_ focus: SpeakerBackgroundFocus?, barID: String) -> Bool {
        guard canEditSpeakerBackgroundFocus, var background = document.speakerBackground else { return false }
        if let focus { guard (SpeakerBackgroundLimits.minFocusZoom...SpeakerBackgroundLimits.maxFocusZoom).contains(focus.zoom),
                              (0...1).contains(focus.x), (0...1).contains(focus.y) else { return false } }
        if let index = background.windows.firstIndex(where: { $0.localID == barID }) {
            guard background.windows[index].focus != focus else { return false }
            background.windows[index].focus = focus
        } else {
            guard background.mediaID != nil, backgroundFillRegions.contains(where: { $0.id == barID }), background.focus != focus else { return false }
            background.focus = focus
        }
        transactDocument(section: .speakerBackground) { $0.speakerBackground = background }
        return true
    }

    /// The real upright frame a bar's crop is drawn on: the photo's own pixels, or a video's frame at its start.
    func cropSourceFrame(forBackgroundBar id: String) async -> UIImage? {
        guard let media = backgroundBarMedia(id: id) else { return nil }
        let key = EditorSpeakerBackground.sourceKey(forMediaID: media.mediaID)
        guard let url = await backgroundSourceURL(forKey: key, mediaID: media.mediaID, kind: media.kind) else { return nil }
        let start = document.speakerBackground?.windows.first(where: { $0.localID == id })?.sourceStartS ?? document.speakerBackground?.sourceStartS ?? 0
        return await KriaCropFrameLoader.image(at: url, time: start)
    }
}
