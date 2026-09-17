import Foundation
import KriaMediaEngine

extension NativeEditorSession {
    /// Writes only the additive KRI-43 fields. Default values are removed so
    /// legacy snapshots remain byte-for-byte shaped as they arrived.
    func setFootagePlaybackRate(_ selection: EditorSelection, rate: Double) {
        guard rate.isFinite, (0.25...4).contains(rate), let section = footageSection(selection),
              canEditOperation(["footage.retime", "playback_rate", section.rawValue], section: section) else { return }
        transactDocument(section: section) { document in
            mutateFootage(selection, in: &document) { raw in
                if abs(rate - 1) < 0.000_001 { raw.removeValue(forKey: "playback_rate") }
                else { raw["playback_rate"] = .number(rate) }
            }
        }
    }

    func setFootageCrop(_ selection: EditorSelection, crop: NormalizedSourceRect?) {
        guard let section = footageSection(selection),
              canEditOperation(["footage.crop", "source_crop", section.rawValue], section: section) else { return }
        if let crop, (try? crop.validate()) == nil { return }
        transactDocument(section: section) { document in
            mutateFootage(selection, in: &document) { raw in
                if let crop { raw["source_crop"] = .object(["x": .number(crop.x), "y": .number(crop.y), "width": .number(crop.width), "height": .number(crop.height)]) }
                else { raw.removeValue(forKey: "source_crop") }
            }
        }
    }

    func footageCrop(for selection: EditorSelection) -> NormalizedSourceRect? {
        let raw: [String: JSONValue]?
        switch selection.kind {
        case .clip:
            raw = timelineClips.first(where: { $0.id.uuidString == selection.id }).flatMap { clip in document.clips.first(where: { $0.id == clip.slotID })?.raw }
        default: raw = visualRaw(selection)
        }
        guard let fields = raw?["source_crop"]?.objectValue,
              let x = fields["x"]?.numberValue, let y = fields["y"]?.numberValue,
              let width = fields["width"]?.numberValue, let height = fields["height"]?.numberValue else { return nil }
        let crop = NormalizedSourceRect(x: x, y: y, width: width, height: height)
        guard (try? crop.validate()) != nil else { return nil }
        return crop
    }

    func footagePlaybackRate(for selection: EditorSelection) -> Double {
        let raw: [String: JSONValue]?
        switch selection.kind {
        case .clip: raw = timelineClips.first(where: { $0.id.uuidString == selection.id }).flatMap { clip in document.clips.first(where: { $0.id == clip.slotID })?.raw }
        default: raw = visualRaw(selection)
        }
        return raw?["playback_rate"]?.numberValue ?? 1
    }

    private func footageSection(_ selection: EditorSelection) -> EditorSection? {
        switch selection.kind {
        case .clip: return .timeline
        case .mediaOverlay: return .mediaOverlays
        case .visualBlock where document.visualBlocks.first(where: { $0.id == selection.id })?.kind == "media": return .visualBlocks
        default: return nil
        }
    }

    private func mutateFootage(_ selection: EditorSelection, in document: inout EditorDocument,
                               body: (inout [String: JSONValue]) -> Void) {
        switch selection.kind {
        case .clip:
            guard let clip = timelineClips.first(where: { $0.id.uuidString == selection.id }),
                  let index = document.clips.firstIndex(where: { $0.id == clip.slotID }) else { return }
            body(&document.clips[index].raw)
        case .mediaOverlay:
            guard let index = document.mediaOverlays.firstIndex(where: { $0.id == selection.id }) else { return }
            body(&document.mediaOverlays[index].raw)
        case .visualBlock:
            guard let index = document.visualBlocks.firstIndex(where: { $0.id == selection.id }), document.visualBlocks[index].kind == "media" else { return }
            body(&document.visualBlocks[index].raw)
        default: return
        }
    }
}
