import Foundation
import KriaMediaEngine

extension NativeEditorSession {
    /// Changes a clip's output duration from its existing source span. The
    /// span is captured before replacing the rate, so 2x -> .5x is absolute
    /// (not a compounded fourfold edit) and reset restores the same footage.
    func setFootagePlaybackRate(_ selection: EditorSelection, rate: Double) {
        guard rate.isFinite, (0.25...4).contains(rate), let section = footageSection(selection) else { return }
        guard selection.kind != .clip || speedBlockedReason(for: selection) == nil else { return }
        // Reset changes the output clock too; a closed device capability must
        // preserve a saved speed until the server permits retiming again.
        guard selection.kind != .clip || !rendersOnDevice || canSetClipPlaybackRate else { return }
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
            sourceStart: found.slot.inS, sourceEnd: sourceEndValue, rate: rate,
            renderedClock: found.slot.raw["timing_mode"]?.stringValue == "rendered"
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

    /// Legacy clearing stays under the section gate for crop and cloud speed.
    /// Device clip speed checks its explicit capability before reaching here,
    /// because resetting it changes duration just like any other speed edit.
    private func canEditFootage(_ section: EditorSection, keys: [String], operation: String, resetting: Bool) -> Bool {
        if resetting { return canEditOperation(keys + [section.rawValue], section: section) }
        // A phone render opens speed only when this exact clip operation is
        // advertised. Overlay/visual retiming stays closed on-device.
        if operation == "playback_rate", section == .timeline, rendersOnDevice {
            return document.capabilities["clips.playback_rate"]?.editable == true
        }
        // KRI-44: a clip's crop is decided by the server's explicit `clips.source_crop`.
        if operation == "source_crop", section == .timeline, rendersOnDevice { return canCropClips }
        // KRI-1136: tilt only exists where the server explicitly lists `clips.tilt`.
        if operation == "tilt" { return canTiltClips }
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

    // MARK: Tilt (KRI-1136)

    var canTiltClips: Bool { document.capabilities["clips.tilt"]?.editable == true }

    /// The stored tilt of a clip, or nil when it is upright with a blank backdrop.
    func clipTilt(for selection: EditorSelection) -> ClipTilt? {
        guard selection.kind == .clip,
              let clip = timelineClips.first(where: { $0.id.uuidString == selection.id }),
              let raw = document.clips.first(where: { $0.id == clip.slotID })?.raw["tilt"],
              let tilt = try? ClipTilt(row: raw) else { return nil }
        return tilt
    }

    /// Why Tilt is greyed out for the selected clip, or nil when it is available.
    func tiltBlockedReason(for selection: EditorSelection?) -> String? {
        guard let selection, selection.kind == .clip else { return "Select a clip to tilt it." }
        if !canTiltClips { return document.capabilities["clips.tilt"]?.reason.map(nativeEditorWireLabel) ?? "Tilt isn’t available for this edit." }
        guard let slotID = timelineClips.first(where: { $0.id.uuidString == selection.id })?.slotID,
              let slot = document.clips.first(where: { $0.id == slotID }) else { return nil }
        return Self.tiltBlockedReason(for: slot)
    }

    static func tiltBlockedReason(for slot: EditorTimelineSlot) -> String? {
        if ![nil, "none"].contains(slot.lookPreset) || !(slot.lookAdjustments?.isEmpty ?? true) {
            return "A clip with a colour look can’t be tilted. Remove the look first."
        }
        if slot.raw["layout"]?.stringValue == "supporting_card" { return "Supporting cards keep their own layout." }
        return nil
    }

    /// Sets a clip's tilt. nil (or an upright, blank one) straightens it and drops the row's `tilt` key, so a
    /// reset clip is byte-identical to one that was never tilted.
    func setClipTilt(_ tilt: ClipTilt?, for selection: EditorSelection) {
        let straightening = tilt?.isIdentity ?? true
        guard let section = footageSection(selection), section == .timeline,
              canEditFootage(section, keys: ["footage.tilt", "tilt"], operation: "tilt", resetting: straightening),
              straightening || tiltBlockedReason(for: selection) == nil else { return }
        let clearValue = footageClearValue(selection, key: "tilt")
        let value = tilt?.rowValue
        transactDocument(section: section) { document in
            mutateFootage(selection, in: &document) { raw in
                if let value { raw["tilt"] = value }
                else if let clearValue { raw["tilt"] = clearValue }
                else { raw.removeValue(forKey: "tilt") }
            }
        }
    }

    /// Gives every clip that can tilt the same backdrop, keeping each one's angle, in one undo step. A clip that
    /// cannot tilt (it carries a look, or is a supporting card) is left exactly as it was. Returns how many
    /// clips changed hands and how many were left alone.
    @discardableResult
    func applyTiltBackdropToAllClips(_ backdrop: ClipBackdrop) -> (applied: Int, skipped: Int) {
        guard canTiltClips, canEditFootage(.timeline, keys: ["footage.tilt", "tilt"], operation: "tilt", resetting: false) else {
            return (0, document.clips.count)
        }
        var applied = 0, skipped = 0
        transactDocument(section: .timeline) { document in
            for index in document.clips.indices {
                let slot = document.clips[index]
                guard Self.tiltBlockedReason(for: slot) == nil else { skipped += 1; continue }
                var tilt = (try? ClipTilt(row: slot.raw["tilt"])) ?? ClipTilt.upright
                tilt.backdrop = backdrop
                if let value = tilt.rowValue { document.clips[index].raw["tilt"] = value }
                else if let clear = clipSlotClearValue(slotID: slot.id ?? "", key: "tilt") { document.clips[index].raw["tilt"] = clear }
                else { document.clips[index].raw.removeValue(forKey: "tilt") }
                applied += 1
            }
        }
        return (applied, skipped)
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
