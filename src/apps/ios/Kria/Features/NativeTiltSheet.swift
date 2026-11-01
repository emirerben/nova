import KriaMediaEngine
import SwiftUI
import UIKit

/// KRI-1136: turns the selected clip or photo to any angle in `-180...180` and picks what shows in the
/// corners it uncovers. Every change goes through `NativeEditorSession.setClipTilt`, so the preview updates
/// live, Reset removes the row's `tilt` key, and Save sends the same row the compiler reads.
struct NativeTiltSheet: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let projectID: UUID
    let selection: EditorSelection
    @State private var applyAllNote: String?
    @State private var showsMediaPicker = false

    private var tilt: ClipTilt { session.clipTilt(for: selection) ?? .upright }
    private var blockedReason: String? { session.tiltBlockedReason(for: selection) }
    private var isBlocked: Bool { blockedReason != nil }

    static let swatches: [(name: String, hex: String)] = [
        ("Black", "#000000"), ("White", "#FFFFFF"), ("Butter", "#F4D35E"),
        ("Pink", "#E91E8C"), ("Blue", "#2D6CDF"), ("Green", "#65A30D")
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let reason = blockedReason {
                    Label(reason, systemImage: "lock")
                        .font(KriaFont.body(14))
                        .foregroundStyle(KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("native-editor-tilt-blocked")
                }
                VStack(alignment: .leading, spacing: 10) {
                    Text(Self.readout(tilt.rotationDeg))
                        .font(KriaFont.headline(34))
                        .foregroundStyle(KriaColor.ink)
                        .accessibilityIdentifier("native-editor-tilt-value")
                    TiltDial(
                        degrees: tilt.rotationDeg,
                        onEditingChanged: { editing in editing ? session.beginTransaction() : session.endTransaction() },
                        onChange: { setRotation($0) }
                    )
                    .disabled(isBlocked)
                }
                HStack(spacing: 8) {
                    pill("Nudge left", systemImage: "arrow.counterclockwise", id: "native-editor-tilt-nudge-left", iconOnly: true) {
                        setRotation(tilt.rotationDeg - 1)
                    }
                    pill("Nudge right", systemImage: "arrow.clockwise", id: "native-editor-tilt-nudge-right", iconOnly: true) {
                        setRotation(tilt.rotationDeg + 1)
                    }
                    pill("Turn 90°", systemImage: "rotate.right", id: "native-editor-tilt-turn-90") {
                        commit(tilt.turnedQuarter())
                    }
                    Spacer(minLength: 0)
                    pill("Reset", systemImage: nil, id: "native-editor-tilt-reset", enabled: !tilt.isIdentity, blockable: false) {
                        session.setClipTilt(nil, for: selection)
                        applyAllNote = nil
                    }
                }
                backgroundSection
            }
            .padding(20)
        }
        .background(KriaColor.paper)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-tilt-sheet")
        .sheet(isPresented: $showsMediaPicker) { mediaPicker }
    }

    // MARK: Background

    private var backdropKind: String {
        switch tilt.backdrop { case .blank: "blank"; case .color: "color"; case .blur: "blur"; case .media: "media" }
    }

    private var backdropOptions: [(kind: String, title: String)] {
        let options = [("blank", "Blank"), ("color", "Colour"), ("blur", "Blur")]
        return session.canTiltBackdropMedia ? options + [("media", "Photo or video")] : options
    }

    private var mediaBackdropID: String? {
        if case .media(let id) = tilt.backdrop { return id }
        return nil
    }

    private var backgroundSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            KriaSectionLabel(title: "Background")
            HStack(spacing: 4) {
                ForEach(backdropOptions, id: \.kind) { option in
                    let kind = option.kind, title = option.title
                    let selected = backdropKind == kind
                    Button {
                        switch kind {
                        case "blank": setBackdrop(.blank)
                        case "color": if backdropKind != "color" { setBackdrop(.color(Self.swatches[0].hex)) }
                        case "media": showsMediaPicker = true
                        default: setBackdrop(.blur)
                        }
                    } label: {
                        Text(title)
                            .font(KriaFont.body(backdropOptions.count > 3 ? 14 : 15).weight(selected ? .bold : .regular))
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                            .foregroundStyle(selected ? KriaColor.ink : KriaColor.zinc)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .background(selected ? KriaColor.paper : .clear, in: Capsule())
                            .shadow(color: .black.opacity(selected ? 0.10 : 0), radius: 4, y: 1)
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(isBlocked)
                    .accessibilityAddTraits(selected ? .isSelected : [])
                    .accessibilityIdentifier("native-editor-tilt-backdrop-\(kind)")
                }
            }
            .padding(4)
            .background(KriaColor.fill, in: Capsule())
            if case .color(let current) = tilt.backdrop {
                HStack(spacing: 10) {
                    ForEach(Self.swatches, id: \.hex) { swatch in
                        Button { setBackdrop(.color(swatch.hex)) } label: {
                            Circle()
                                .fill(Self.color(hex: swatch.hex))
                                .frame(width: 32, height: 32)
                                .padding(6)
                                .background(current.uppercased() == swatch.hex ? KriaColor.fill : .clear, in: Circle())
                                .frame(width: 44, height: 44)
                                .contentShape(Circle())
                        }
                        .buttonStyle(.plain)
                        .disabled(isBlocked)
                        .accessibilityLabel(swatch.name)
                        .accessibilityAddTraits(current.uppercased() == swatch.hex ? .isSelected : [])
                        .accessibilityIdentifier("native-editor-tilt-swatch-\(swatch.name.lowercased())")
                    }
                }
            }
            if let poolID = mediaBackdropID { mediaBackdropRow(poolID) }
            if session.timelineClips.count > 1 {
                pill("Use this background on every clip", systemImage: nil, id: "native-editor-tilt-apply-all", enabled: !isBlocked) {
                    let result = session.applyTiltBackdropToAllClips(tilt.backdrop)
                    applyAllNote = Self.applyAllNote(result)
                }
                if let applyAllNote {
                    Text(applyAllNote)
                        .font(KriaFont.body(13))
                        .foregroundStyle(KriaColor.mutedInk)
                        .accessibilityIdentifier("native-editor-tilt-apply-all-note")
                }
            }
        }
    }

    private func mediaBackdropRow(_ poolID: String) -> some View {
        let asset = session.tiltBackdropChoices.first { $0.id == poolID }
        let missing = session.tiltBackdropIsMissing(tilt.backdrop)
        return HStack(spacing: 12) {
            Group {
                if let asset { NativeVisualThumbnail(asset: asset) } else { Image(systemName: "photo").foregroundStyle(KriaColor.mutedInk) }
            }
            .frame(width: 56, height: 56)
            .background(KriaColor.softZinc)
            .clipShape(RoundedRectangle(cornerRadius: 9))
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(asset == nil ? "Missing photo or video" : "Chosen photo or video")
            .accessibilityIdentifier("native-editor-tilt-media-thumbnail")
            VStack(alignment: .leading, spacing: 2) {
                Text(asset?.sourceFilename ?? (asset?.kind == "video" ? "Video" : "Photo"))
                    .font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink).lineLimit(1)
                if missing {
                    Text("That photo or video isn’t available anymore. Choose another or Reset.")
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("native-editor-tilt-media-missing")
                }
            }
            Spacer(minLength: 0)
            pill("Change", systemImage: nil, id: "native-editor-tilt-media-change") { showsMediaPicker = true }
        }
    }

    private var mediaPicker: some View {
        NavigationStack {
            ScrollView {
                NativeVisualPoolPicker(
                    session: session, uploads: uploads, projectID: projectID, idPrefix: "native-editor-tilt-media",
                    choices: session.tiltBackdropChoices, selectedID: mediaBackdropID, noneTitle: "None",
                    onNone: { setBackdrop(.blank); showsMediaPicker = false },
                    onSelect: { asset in setBackdrop(.media(asset.id)); showsMediaPicker = false }
                )
                .padding(16)
            }
            .navigationTitle("Photo or video")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { showsMediaPicker = false }.accessibilityIdentifier("native-editor-tilt-media-done")
                }
            }
        }
        .tint(KriaColor.ink)
        .presentationDetents([.medium, .large])
    }

    // MARK: Edits

    private func setRotation(_ degrees: Double) {
        var next = tilt
        next.rotationDeg = (degrees * 10).rounded() / 10
        commit(next)
    }

    private func setBackdrop(_ backdrop: ClipBackdrop) {
        var next = tilt
        next.backdrop = backdrop
        commit(next)
    }

    private func commit(_ next: ClipTilt) {
        guard next != tilt else { return }
        session.setClipTilt(next, for: selection)
    }

    private func pill(_ title: String, systemImage: String?, id: String, iconOnly: Bool = false, enabled: Bool = true,
                      blockable: Bool = true, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Group {
                if let systemImage, iconOnly { Image(systemName: systemImage) }
                else if let systemImage { Label(title, systemImage: systemImage) }
                else { Text(title) }
            }
            .font(KriaFont.body(15).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .frame(minWidth: iconOnly ? 44 : 0, minHeight: 44)
            .padding(.horizontal, iconOnly ? 6 : 16)
            .background(KriaColor.fill, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .disabled(!enabled || (isBlocked && blockable))
        .accessibilityLabel(title)
        .accessibilityIdentifier(id)
    }

    // MARK: Formatting

    static func readout(_ degrees: Double) -> String {
        let rounded = (degrees * 10).rounded() / 10
        let text = rounded == rounded.rounded() ? String(Int(rounded)) : String(format: "%.1f", rounded)
        return text + "°"
    }

    static func applyAllNote(_ result: (applied: Int, skipped: Int)) -> String {
        let applied = result.applied == 1 ? "1 clip" : "\(result.applied) clips"
        guard result.skipped > 0 else { return "Applied to \(applied)." }
        let skipped = result.skipped == 1 ? "1 clip was" : "\(result.skipped) clips were"
        return "Applied to \(applied). \(skipped) left alone because it can’t be tilted."
    }

    static func color(hex: String) -> Color {
        guard hex.hasPrefix("#"), hex.count == 7, let value = UInt32(hex.dropFirst(), radix: 16) else { return .black }
        return Color(red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
    }
}

/// A track from -180 to 180 with detents at 0, ±90 and ±180. Dragging right turns the clip clockwise, the
/// same sense as the Nudge right button and as the corner-rotate handle on text and overlays.
struct TiltDial: View {
    let degrees: Double
    var onEditingChanged: (Bool) -> Void = { _ in }
    let onChange: (Double) -> Void
    @State private var dragging = false
    @State private var lastDetent: Double?
    private let haptic = UISelectionFeedbackGenerator()

    static let detents: [Double] = [-180, -90, 0, 90, 180]

    /// The angle under `x` on a track `width` wide, with the shared 90° detent applied.
    static func angle(atX x: Double, width: Double) -> Double {
        guard width > 0 else { return 0 }
        let raw = (min(max(x / width, 0), 1) * 360) - 180
        return min(180, max(-180, NativeTextRotationSnap.angle(raw)))
    }

    var body: some View {
        GeometryReader { proxy in
            let width = proxy.size.width
            let inset: CGFloat = 16
            let travel = max(1, width - inset * 2)
            ZStack(alignment: .leading) {
                Capsule().fill(KriaColor.fill).frame(height: 8)
                    .padding(.horizontal, inset)
                ForEach(Self.detents, id: \.self) { detent in
                    Capsule()
                        .fill(detent == 0 ? KriaColor.ink : KriaColor.line)
                        .frame(width: 3, height: detent == 0 ? 22 : 16)
                        .offset(x: inset + travel * CGFloat((detent + 180) / 360) - 1.5)
                }
                Circle()
                    .fill(KriaColor.ink)
                    .frame(width: 28, height: 28)
                    .offset(x: inset + travel * CGFloat((degrees + 180) / 360) - 14)
            }
            .frame(maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if !dragging { dragging = true; haptic.prepare(); onEditingChanged(true) }
                        let next = Self.angle(atX: Double(value.location.x - inset), width: Double(travel))
                        if let detent = Self.detents.first(where: { abs($0 - next) < 0.05 }), lastDetent != detent {
                            haptic.selectionChanged()
                            lastDetent = detent
                        } else if Self.detents.allSatisfy({ abs($0 - next) >= 0.05 }) {
                            lastDetent = nil
                        }
                        onChange(next)
                    }
                    .onEnded { _ in
                        dragging = false
                        lastDetent = nil
                        onEditingChanged(false)
                    }
            )
        }
        .frame(height: 44)
        .accessibilityElement()
        .accessibilityLabel("Tilt angle")
        .accessibilityValue(NativeTiltSheet.readout(degrees))
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: onChange(degrees + 1)
            case .decrement: onChange(degrees - 1)
            @unknown default: break
            }
        }
        .accessibilityIdentifier("native-editor-tilt-dial")
    }
}
