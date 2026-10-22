import Foundation
import KriaMediaEngine

extension NativeEditorSession {
    /// Changes a clip's output duration from its existing source span. The
    /// span is captured before replacing the rate, so 2x -> .5x is absolute
    /// (not a compounded fourfold edit) and reset restores the same footage.
    func setFootagePlaybackRate(_ selection: EditorSelection, rate: Double) {
        guard rate.isFinite, (0.25...4).contains(rate), let section = footageSection(selection) else { return }
        guard selection.kind != .clip || speedBlockedReason(for: selection) == nil else { return }
        let resetting = abs(rate - 1) < 0.000_001
        guard canEditFootage(section, keys: ["footage.retime", "playback_rate"], operation: "playback_rate", resetting: resetting) else { return }
        let clearValue = footageClearValue(selection, key: "playback_rate")
        if selection.kind == .clip, rendersOnDevice {
            _ = setClipPlaybackRate(selection, rate: rate, resetting: resetting, clearValue: clearValue)
            return
        }
        transactDocument(section: section) { document in
            mutateFootage(selection, in: &document) { raw in
                if !resetting { raw["playback_rate"] = .number(rate) }
                else if let clearValue { raw["playback_rate"] = clearValue }
                else { raw.removeValue(forKey: "playback_rate") }
            }
        }
    }

    /// A speed edit owns rate and duration together. `duration_s` is output
    /// seconds; source span is either the native span or the effective rate
    /// times the current output duration. Keep `in_s` untouched.
    private func setClipPlaybackRate(_ selection: EditorSelection, rate: Double, resetting: Bool, clearValue: JSONValue?) -> Bool {
        guard let found = sourceWindowSlot(for: selection),
              let index = document.clips.firstIndex(where: { $0.id == found.slot.id }) else { return false }
        let outputDuration = sourceWindowLength(slot: found.slot, clip: found.clip)
        let sourceEnd = found.slot.raw["source_end_s"]?.numberValue
        let nativeSpan = found.slot.raw["native_source_span_s"]?.numberValue
        let derivedSpan = outputDuration * effectiveSourceRate(slot: found.slot, clip: found.clip)
        let sourceSpan = sourceEnd.map { $0 - found.slot.inS } ?? nativeSpan ?? derivedSpan
        guard sourceSpan.isFinite, sourceSpan >= 0.1 else { return false }
        let sourceEndValue = found.slot.inS + sourceSpan
        let exactNewDuration = sourceSpan / rate
        guard exactNewDuration.isFinite, exactNewDuration >= 0.1,
              let newDuration = NativeEditorInteraction.sourcePreservingOutputDuration(
            sourceStart: found.slot.inS, sourceEnd: sourceEndValue, rate: rate
        ) else { return false }
        let existingRate = found.slot.raw["playback_rate"]?.numberValue ?? 1
        let existingSourceEnd = found.slot.raw["source_end_s"]?.numberValue
        let expectedRateIsStored = !resetting && found.slot.raw["playback_rate"] == .number(rate)
        let resetIsCleared = resetting && found.slot.raw["playback_rate"] == nil
        if abs(outputDuration - newDuration) <= 0.000_001,
           abs(existingRate - rate) <= 0.000_001,
           abs((existingSourceEnd ?? .nan) - sourceEndValue) <= 0.000_001,
           (expectedRateIsStored || resetIsCleared) {
            return true
        }
        transactDocument(section: .timeline) { document in
            document.clips[index].durationS = newDuration
            document.clips[index].durationBeats = nil
            // The explicit end is the stable source anchor used by save,
            // reopen and later rate assignments. It avoids recovering a span
            // from a rounded output duration.
            document.clips[index].raw["source_end_s"] = .number(sourceEndValue)
            if !resetting { document.clips[index].raw["playback_rate"] = .number(rate) }
            else if let clearValue { document.clips[index].raw["playback_rate"] = clearValue }
            else { document.clips[index].raw.removeValue(forKey: "playback_rate") }
        }
        return true
    }

    func setFootageCrop(_ selection: EditorSelection, crop: NormalizedSourceRect?) {
        guard let section = footageSection(selection),
              canEditFootage(section, keys: ["footage.crop", "source_crop"], operation: "source_crop", resetting: crop == nil) else { return }
        if let crop, (try? crop.validate()) == nil { return }
        let clearValue = footageClearValue(selection, key: "source_crop")
        transactDocument(section: section) { document in
            mutateFootage(selection, in: &document) { raw in
                if let crop { raw["source_crop"] = .object(["x": .number(crop.x), "y": .number(crop.y), "width": .number(crop.width), "height": .number(crop.height)]) }
                else if let clearValue { raw["source_crop"] = clearValue }
                else { raw.removeValue(forKey: "source_crop") }
            }
        }
    }

    /// A clip's crop and speed also carry a `clips.<operation>` capability,
    /// which device variants close. Returning to the default stays under the
    /// section's own gate: a value saved before that clamp must stay clearable,
    /// or the edit could never save again.
    private func canEditFootage(_ section: EditorSection, keys: [String], operation: String, resetting: Bool) -> Bool {
        if resetting { return canEditOperation(keys + [section.rawValue], section: section) }
        // A phone render opens speed only when this exact clip operation is
        // advertised. Overlay/visual retiming stays closed on-device.
        if operation == "playback_rate", section == .timeline, rendersOnDevice {
            return document.capabilities["clips.playback_rate"]?.editable == true
        }
        // KRI-44: a clip's crop is decided by the server's explicit `clips.source_crop`.
        if operation == "source_crop", section == .timeline, rendersOnDevice { return canCropClips }
        guard !rendersOnDevice else { return false }
        let clipKeys: [String] = section == .timeline ? ["clips.\(operation)"] : []
        return canEditOperation(keys + clipKeys + [section.rawValue], section: section)
    }

    private func footageClearValue(_ selection: EditorSelection, key: String) -> JSONValue? {
        guard selection.kind == .clip,
              let slotID = timelineClips.first(where: { $0.id.uuidString == selection.id })?.slotID else { return nil }
        return clipSlotClearValue(slotID: slotID, key: key)
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
