import KriaMediaEngine
import SwiftUI
import UIKit

/// Tilt for the selected slide's photo or video (KRI-1138), in the same connected bottom shell as the Look panel. Every
/// change stages on the session's draft, so the stage above shows it live and nothing here saves. Only the media turns:
/// text, drawing and pictures stay upright.
///
/// Written as a self-contained view over (`tilt`, `onChange`) so it can move onto a shared Tilt sheet later without
/// touching the session: the panel never reads or writes the draft itself.
struct SlidePostTiltPanel: View {
    @ObservedObject var session: SlidePostSession
    let slideID: String
    let onDone: () -> Void

    private var tilt: SlidePostTilt? { session.draft?.slides.first { $0.id == slideID }?.edits?.tilt }

    var body: some View {
        SlidePostTiltControls(
            tilt: tilt,
            onChange: { next, coalescing in session.setTilt(slideID: slideID, next, coalescing: coalescing ? "slidepost-tilt-\(slideID)" : nil) },
            onDone: onDone
        )
        .nativeEditorIslandSurface(cornerRadius: 32)
    }
}

/// The controls: a dial from -180 to 180 with detents at 0, +-90 and 180, a 1 degree nudge, Turn 90 and Reset, and the
/// Background row (Blank, Colour, Blur). `onChange(next, coalescing)` is called with `coalescing == true` for a drag (many
/// calls, one undo step) and `false` for a tap.
struct SlidePostTiltControls: View {
    let tilt: SlidePostTilt?
    let onChange: (SlidePostTilt?, Bool) -> Void
    let onDone: () -> Void

    /// The angle a drag snaps to when it comes within this many degrees of a detent.
    static let detents: [Double] = [-180, -90, 0, 90, 180]
    static let snapWindow = 3.0

    @State private var colour: Color = .white
    @State private var lastDetent: Double?
    /// The background chosen while the picture is still upright: a background with no turn draws nothing, so it is held
    /// here and goes in with the first turn.
    @State private var pending: ClipBackdrop = .blank

    private var degrees: Double { tilt?.rotationDeg ?? 0 }
    private var backdrop: ClipBackdrop { tilt?.backdrop ?? pending }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Tilt").font(KriaFont.body(18).weight(.semibold))
                Spacer()
                Text(Self.readout(degrees)).font(KriaFont.body(14).weight(.medium)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("slidepost-tilt-readout")
                Button(action: onDone) {
                    Text("Done").font(KriaFont.body(14)).frame(minWidth: 64, minHeight: 44)
                        .background(KriaColor.ink.opacity(0.06), in: Capsule())
                }
                .accessibilityIdentifier("slidepost-done")
            }
            dial
            HStack(spacing: 8) {
                pill("\u{2212}1\u{00B0}", id: "slidepost-tilt-minus") { set(degrees - 1) }
                    .accessibilityLabel("Turn one degree left")
                pill("+1\u{00B0}", id: "slidepost-tilt-plus") { set(degrees + 1) }
                    .accessibilityLabel("Turn one degree right")
                pill("Turn 90\u{00B0}", id: "slidepost-tilt-quarter") { set(Self.quarterTurn(from: degrees)) }
                pill("Reset", id: "slidepost-tilt-reset", enabled: tilt != nil) { onChange(nil, false) }
            }
            .excludesDrawerGesture()
            backgroundRow
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 24).padding(.top, 10).padding(.bottom, 8).frame(maxWidth: .infinity, alignment: .leading)
        .font(KriaFont.body(14)).tint(KriaColor.ink)
        .onAppear { if case .color(let hex) = backdrop { colour = Color(slideHex: hex) } }
    }

    // MARK: Dial

    private var dial: some View {
        VStack(spacing: 2) {
            Slider(
                value: Binding(get: { degrees }, set: { dragged in set(Self.snapped(dragged), coalescing: true) }),
                in: -SlidePostTilt.maxDegrees...SlidePostTilt.maxDegrees, step: 1
            )
            .frame(minHeight: 44)
            .accessibilityLabel("Tilt")
            .accessibilityValue(Self.readout(degrees))
            .accessibilityIdentifier("slidepost-tilt-dial")
            .excludesDrawerGesture()
            HStack {
                ForEach(Self.detents, id: \.self) { detent in
                    Text(detent == 0 ? "0\u{00B0}" : "\(Int(detent))\u{00B0}")
                        .font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc)
                        .frame(maxWidth: .infinity)
                }
            }
            .accessibilityHidden(true)
        }
    }

    // MARK: Background

    private var backgroundRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Background").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.zinc)
            HStack(spacing: 8) {
                kind("Blank", id: "slidepost-tilt-bg-blank", on: backdrop == .blank) { setBackdrop(.blank) }
                kind("Colour", id: "slidepost-tilt-bg-colour", on: isColour) { setBackdrop(.color(colour.slideHexString)) }
                kind("Blur", id: "slidepost-tilt-bg-blur", on: backdrop == .blur) { setBackdrop(.blur) }
                if isColour {
                    ColorPicker("Background colour", selection: Binding(get: { colour }, set: { picked in
                        colour = picked
                        setBackdrop(.color(picked.slideHexString), coalescing: true)
                    }), supportsOpacity: false)
                    .labelsHidden().frame(minWidth: 44, minHeight: 44)
                    .accessibilityIdentifier("slidepost-tilt-colour")
                }
            }
            .excludesDrawerGesture()
            if tilt == nil {
                Text("Shows behind the picture once you turn it.").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            }
        }
    }

    private var isColour: Bool { if case .color = backdrop { return true } else { return false } }

    private func kind(_ title: String, id: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(KriaFont.body(14).weight(on ? .semibold : .regular)).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(on ? KriaColor.butter : KriaColor.fill, in: Capsule())
        }
        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : []).accessibilityIdentifier(id)
    }

    private func pill(_ title: String, id: String, enabled: Bool = true, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(KriaColor.fill, in: Capsule())
        }
        .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.4).accessibilityIdentifier(id)
    }

    // MARK: Writes

    /// A turn keeps the backdrop; upright with the default backdrop is no tilt, so it is stored as nil.
    private func set(_ next: Double, coalescing: Bool = false) {
        let wrapped = SlidePostTilt.wrapped(next)
        if Self.detents.contains(wrapped), lastDetent != wrapped { UISelectionFeedbackGenerator().selectionChanged() }
        lastDetent = Self.detents.contains(wrapped) ? wrapped : nil
        onChange(SlidePostTilt(rotationDeg: wrapped, backdrop: backdrop), coalescing)
    }

    private func setBackdrop(_ next: ClipBackdrop, coalescing: Bool = false) {
        guard let tilt else { pending = next; return }
        onChange(SlidePostTilt(rotationDeg: tilt.rotationDeg, backdrop: next), coalescing)
    }

    // MARK: Pure helpers (unit tested)

    /// Within `snapWindow` degrees of a detent the dial lands exactly on it.
    static func snapped(_ degrees: Double) -> Double {
        detents.first { abs($0 - degrees) <= snapWindow } ?? degrees
    }

    /// The next multiple of 90 clockwise (so a repeated tap walks 0, 90, 180, -90, 0).
    static func quarterTurn(from degrees: Double) -> Double {
        let next = (degrees / 90).rounded(.down) * 90 + 90
        return SlidePostTilt.wrapped(next)
    }

    /// "12\u{00B0} right", "30\u{00B0} left" or "Upright".
    static func readout(_ degrees: Double) -> String {
        let whole = Int(degrees.rounded())
        if whole == 0 { return "Upright" }
        return "\(abs(whole))\u{00B0} \(whole > 0 ? "right" : "left")"
    }
}
