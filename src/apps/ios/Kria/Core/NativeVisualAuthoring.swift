import Foundation

/// Construction never changes the base timeline: visual layers occupy existing time.
enum NativeVisualAuthoring {
    static var defaultStyle: [String: JSONValue] {
        ["version": .number(1), "rotation_deg": .number(0), "fit_mode": .string("cover"), "zoom": .number(1),
         "animation": .object(["version": .number(1), "entrance": .string("none"), "exit": .string("none"), "loop": .string("none"), "speed": .number(1)])]
    }

    static func style(for raw: [String: JSONValue]) -> [String: JSONValue] {
        if let existing = raw["editor_style"]?.objectValue { return existing }
        var style = defaultStyle
        if let transform = raw["transform"]?.objectValue {
            style["fit_mode"] = transform["fit_mode"] ?? .string("contain")
            style["zoom"] = transform["zoom"] ?? .number(1)
        } else if raw["kind"] == .string("media") { style["fit_mode"] = .string("contain") }
        return style
    }

    static func window(at playhead: Double, duration: Double, projection: NativeEditorTimelineProjection,
                       preferred: Double = 3, minimum: Double = 0.1) -> (start: Double, end: Double)? {
        guard playhead.isFinite, duration.isFinite, preferred.isFinite, duration >= minimum else { return nil }
        let outputStart = max(0, min(playhead, duration - minimum))
        let start = projection.unprojectOutputTime(outputStart)
        let end = projection.unprojectOutputTime(min(duration, outputStart + preferred))
        guard end - start >= minimum - 0.000_001 else { return nil }
        return (start, end)
    }

    static func media(asset: CreationVisual, start: Double, end: Double, z: Int) -> EditorVisualBlock? {
        guard let path = asset.gcsPath, !path.isEmpty, asset.status == "ready", ["image", "video"].contains(asset.kind), end > start else { return nil }
        if asset.kind == "video", (asset.durationS ?? 0) < end - start { return nil }
        let id = UUID().uuidString
        var raw: [String: JSONValue] = [
            "version": .number(1), "origin": .string("user"), "timing_mode": .string("manual"),
            "asset_id": .string(asset.id), "src_gcs_path": .string(path), "media_kind": .string(asset.kind),
            "display_mode": .string("overlay"), "scale": .number(0.35), "x_frac": .number(0.5), "y_frac": .number(0.5),
            "z": .number(Double(z)), "transform": .object(["fit_mode": .string("cover"), "focal_x": .number(0.5), "focal_y": .number(0.5), "zoom": .number(1)]),
            "editor_style": .object(defaultStyle)
        ]
        if let duration = asset.durationS, asset.kind == "video" {
            raw["source_duration_s"] = .number(duration)
            raw["trim_start_s"] = .number(0); raw["trim_end_s"] = .number(duration)
        }
        return .init(id: id, kind: "media", startS: start, endS: end, raw: raw)
    }

    static func card(text: String, bold: Bool, start: Double, end: Double) -> (EditorVisualBlock, EditorTextElement)? {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, text.count <= 500, end > start else { return nil }
        let id = UUID().uuidString
        let block = EditorVisualBlock(id: id, kind: "text_card", startS: start, endS: end,
            raw: ["version": .number(1), "origin": .string("user"), "timing_mode": .string("manual"),
                  "style_preset_id": .string(bold ? "bold" : "simple"),
                  "background": .object(["type": .string("solid"), "color": .string(bold ? "#30352C" : "#FFFFFF")])])
        let element = EditorTextElement(id: UUID().uuidString, text: text, startS: start, endS: end,
            raw: ["visual_block_id": .string(id), "font_family": .string(bold ? "Inter" : "Inter Regular"),
                  "size_px": .number(bold ? 96 : 72), "color": .string(bold ? "#FFFFFF" : "#30352C"),
                  "x_frac": .number(0.5), "y_frac": .number(0.5), "position": .string("custom"), "effect": .string("none")])
        return (block, element)
    }

    static func motion(preset: String, assets: [CreationVisual], start: Double, end: Double) -> EditorMotionScene? {
        let minimum = preset == "card_stack" ? 2 : 3
        let maximum = preset == "card_stack" ? 6 : 8
        guard ["card_stack", "film_strip"].contains(preset), (minimum...maximum).contains(assets.count),
              assets.allSatisfy({ $0.status == "ready" && $0.kind == "image" && $0.gcsPath != nil }), end > start else { return nil }
        let startFrame = Int(ceil(start * 30)), endFrame = Int(floor(end * 30))
        guard endFrame > startFrame, endFrame - startFrame <= 240 else { return nil }
        let id = UUID().uuidString
        let raw: [String: JSONValue] = ["id": .string(id), "preset_id": .string(preset), "preset_version": .number(2),
            "start_frame": .number(Double(startFrame)), "end_frame_exclusive": .number(Double(endFrame)),
            "palette": .object(["primary": .string("#30352C"), "accent": .string("#9BCAFF")]), "intensity": .number(0.7),
            "params": .object(["assets": .array(assets.map { .object(["asset_id": .string($0.id), "gcs_path": .string($0.gcsPath!)]) })]),
            "motion": .object(["version": .number(2), "speed": .number(1), "easing": .string("ease-in-out-cubic"), "hold_frames": .number(0)])]
        return .init(id: id, startS: Double(startFrame) / 30, endS: Double(endFrame) / 30, preset: preset, raw: raw)
    }
}
