import SwiftUI
import AVFoundation
import AVKit

/// Translucent camera chrome (Paper S3.08): the same recipe as `KriaCameraUploadButton`, for any shape.
private struct CameraGlassModifier<S: InsettableShape>: ViewModifier {
    let shape: S
    var strength = 0.2
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        let reduced = KriaTransparency.isReduced(reduceTransparency)
        content.background {
            ZStack {
                if !reduced { shape.fill(.ultraThinMaterial) }
                shape.fill(Color.white.opacity(reduced ? 0.28 : strength))
            }
        }
    }
}

extension View {
    func kriaCameraGlass<S: InsettableShape>(_ shape: S, strength: Double = 0.2) -> some View {
        modifier(CameraGlassModifier(shape: shape, strength: strength))
    }
}

/// The platform red every camera reads as "recording" (Paper S3.08, decision 3.5).
enum KriaCaptureColor {
    static let red = Color(red: 1, green: 0.231, blue: 0.188)
}

/// A solid 60pt red disc in a 4pt white ring; recording turns the disc into a 28pt rounded square.
struct KriaCaptureButton: View {
    let isRecording: Bool
    var isEnabled = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle().strokeBorder(Color.white, lineWidth: 4)
                if isRecording {
                    RoundedRectangle(cornerRadius: 8, style: .continuous).fill(KriaCaptureColor.red).frame(width: 28, height: 28)
                } else {
                    Circle().fill(KriaCaptureColor.red).frame(width: 60, height: 60)
                }
            }
            .frame(width: 76, height: 76)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(!isEnabled)
        .opacity(isEnabled ? 1 : 0.4)
        .accessibilityLabel(isRecording ? "Stop recording" : "Record")
        .accessibilityIdentifier(isRecording ? "teleprompter-stop-recording" : "teleprompter-record")
    }
}

/// 44pt white-20% disc with a white close mark, 16pt in from the edge (Paper S3.08). The paper glass close button
/// is ink on white, which reads grey over a camera preview.
struct KriaCameraCloseButton: View {
    var identifier = "teleprompter-close"
    var label = "Close"
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            KriaCloseMark()
                .stroke(Color.white, style: StrokeStyle(lineWidth: 1.9, lineCap: .round))
                .frame(width: 10, height: 10)
                .frame(width: 44, height: 44)
                .kriaCameraGlass(Circle())
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier)
    }
}

/// 44pt glass icon (flip camera).
struct KriaCameraIconButton: View {
    let symbol: String
    let label: String
    let identifier: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 18, weight: .regular)).foregroundStyle(Color.white)
                .frame(width: 44, height: 44)
                .kriaCameraGlass(Circle())
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier)
    }
}

/// A glass capsule with an optional leading symbol, for the camera's top row and status chips.
struct KriaCameraCapsule: View {
    let title: String
    var symbol: String?
    var identifier: String?
    /// The control is switched on: ink on white, the camera's version of the selected state.
    var isOn = false
    var action: (() -> Void)?

    var body: some View {
        if let action {
            Button(action: action) { label }
                .buttonStyle(.plain)
                .accessibilityLabel(title)
                .accessibilityIdentifier(identifier ?? "camera-capsule")
        } else {
            label.accessibilityElement(children: .combine)
        }
    }

    private var label: some View {
        HStack(spacing: 8) {
            if let symbol { Image(systemName: symbol).font(.system(size: 15, weight: .regular)) }
            Text(title).kriaType(.button).multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
        }
        .foregroundStyle(isOn ? KriaColor.ink : Color.white)
        .padding(.horizontal, 16).padding(.vertical, 6).frame(minHeight: 44)
        .background { if isOn { Capsule().fill(Color.white) } }
        .kriaCameraGlass(Capsule())
        .contentShape(Capsule())
        .accessibilityAddTraits(isOn ? .isSelected : [])
    }
}

/// The 120pt countdown disc (Paper S3.09a) with a white tertiary Cancel underneath.
struct KriaCountdownDisc: View {
    let value: Int
    let cancel: () -> Void

    var body: some View {
        VStack(spacing: 12) {
            Text("\(value)").kriaType(.display).foregroundStyle(Color.white)
                .frame(width: 120, height: 120)
                .kriaCameraGlass(Circle())
                .accessibilityLabel("Recording in \(value)")
            Button("Cancel", action: cancel)
                .kriaType(.button).foregroundStyle(Color.white).frame(minWidth: 88, minHeight: 44)
                .accessibilityIdentifier("teleprompter-cancel-countdown")
        }
    }
}

/// Camera or microphone refused or unavailable (Paper S3.10). Always two ways forward: fix it, or upload a video.
struct CameraAccessCard: View {
    let kind: TeleprompterCamera.ErrorKind?
    let message: String
    let openSettings: () -> Void
    let retry: () -> Void
    let upload: (() -> Void)?

    private var title: String {
        switch kind {
        case .cameraAccess: "Camera access is off"
        case .microphoneAccess: "Microphone access is off"
        case .restricted: "Camera access is restricted"
        default: "The camera isn’t available"
        }
    }
    private var symbol: String { kind == .microphoneAccess ? "mic.slash" : "video.slash" }
    private var needsSettings: Bool { kind == .cameraAccess || kind == .microphoneAccess }

    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: symbol).font(.system(size: 20)).foregroundStyle(Color.white)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).kriaType(.button).foregroundStyle(Color.white)
                    .fixedSize(horizontal: false, vertical: true)
                Text(message).kriaType(.label).foregroundStyle(Color.white.opacity(0.8))
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("teleprompter-camera-error")
            }
            // Two short actions sit side by side; at accessibility sizes they stack and each keeps its whole label.
            let actions = Group {
                if needsSettings {
                    Button("Open Settings", action: openSettings)
                        .buttonStyle(KriaButtonStyle(role: .primary, size: .medium, fullWidth: true))
                        .accessibilityIdentifier("teleprompter-open-settings")
                } else if kind != .restricted {
                    Button("Try again", action: retry)
                        .buttonStyle(KriaButtonStyle(role: .primary, size: .medium, fullWidth: true))
                        .accessibilityIdentifier("teleprompter-retry-camera")
                }
                if let upload {
                    Button(action: upload) {
                        HStack(spacing: 8) {
                            Image(systemName: KriaSourceOption.uploadSymbol).font(.system(size: 15))
                            Text("Upload").kriaType(.button).multilineTextAlignment(.center)
                        }
                        .foregroundStyle(Color.white).padding(.vertical, 8).frame(maxWidth: .infinity, minHeight: 44)
                        .background(Color.white.opacity(0.2), in: Capsule())
                        .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Upload")
                    .accessibilityIdentifier("camera-upload")
                }
            }
            if dynamicTypeSize.isAccessibilitySize {
                VStack(spacing: 8) { actions }
            } else {
                HStack(spacing: 8) { actions }
            }
            if needsSettings {
                Text("Come back and tap Record. It works as soon as access is on.")
                    .kriaType(.caption).foregroundStyle(Color.white.opacity(0.8))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .padding(.horizontal, 24)
    }
}

/// The optional script (Paper S3.11). Scrolls inside a faded field, and Expand gives it the full height. At
/// accessibility sizes the sheet opens full height and scrolls as a whole, so no line of it can be cut off.
struct TeleprompterScriptSheet: View {
    @ObservedObject var script: TeleprompterScript
    let save: () -> Void
    let useScript: () -> Void
    let skip: () -> Void
    @State private var detent: PresentationDetent = .medium
    @FocusState private var focused: Bool
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var trimmed: String { script.text.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var expanded: Bool { detent == .large }
    private var largeText: Bool { dynamicTypeSize.isAccessibilitySize }

    private static func spoken(_ seconds: Int) -> String {
        seconds < 60 ? "\(seconds) \(seconds == 1 ? "second" : "seconds")"
            : "\(seconds / 60) minute\(seconds / 60 == 1 ? "" : "s")" + (seconds % 60 == 0 ? "" : " \(seconds % 60) seconds")
    }

    private var caption: String {
        // An empty field shows the pace on a typical 90 word script, so the speed has a meaning before anything is typed.
        guard !trimmed.isEmpty else {
            return "About \(Self.spoken(Int((90 * 60 / Double(script.wordsPerMinute)).rounded()))) at \(script.wordsPerMinute) words per minute"
        }
        let words = script.readingUnitCount
        let base = "\(words) \(words == 1 ? "word" : "words"), about \(Self.spoken(Int(ceil(script.estimatedReadTime)))) at \(script.wordsPerMinute) words per minute."
        return words > 90 ? base + " Scroll to read the rest." : base
    }

    var body: some View {
        Group {
            if largeText {
                ScrollView { content.padding(.bottom, 12) }.scrollBounceBehavior(.basedOnSize)
            } else {
                content
            }
        }
        .padding(.horizontal, 24).padding(.top, 24).padding(.bottom, 12)
        .background(KriaColor.paper)
        .presentationDetents(largeText ? [.large] : [.medium, .large], selection: $detent)
        .presentationDragIndicator(largeText ? .visible : .hidden)
        .presentationCornerRadius(24)
        .onAppear { if largeText { detent = .large } }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 12) {
            let heading = Text("Script").kriaType(.heading).foregroundStyle(KriaColor.ink).accessibilityAddTraits(.isHeader)
            let expand = Button {
                detent = expanded ? .medium : .large
            } label: {
                Label(expanded ? "Collapse" : "Expand",
                      systemImage: expanded ? "arrow.down.right.and.arrow.up.left" : "arrow.up.left.and.arrow.down.right")
                    .labelStyle(.titleAndIcon)
            }
            .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
            .accessibilityIdentifier("teleprompter-script-expand")
            if largeText {
                heading
            } else {
                HStack { heading; Spacer(); expand }
            }
            TextEditor(text: Binding(get: { script.text }, set: { script.updateText($0); save() }))
                .scrollContentBackground(.hidden)
                .kriaType(.body).foregroundStyle(KriaColor.ink).tint(KriaColor.sky)
                .focused($focused)
                .padding(.horizontal, 12).padding(.vertical, 8)
                .frame(maxWidth: .infinity, minHeight: 200, maxHeight: largeText ? 280 : .infinity)
                .background(focused || !trimmed.isEmpty ? KriaColor.selectionSoft : KriaColor.fill,
                            in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .overlay(alignment: .topLeading) {
                    if script.text.isEmpty {
                        Text("Paste or write what you’ll say").kriaType(.body).foregroundStyle(KriaColor.mutedInk)
                            .padding(.horizontal, 17).padding(.vertical, 16).allowsHitTesting(false)
                    }
                }
                .mask(LinearGradient(stops: [.init(color: .black.opacity(0.35), location: 0), .init(color: .black, location: 0.06),
                                             .init(color: .black, location: 0.94), .init(color: .black.opacity(0.35), location: 1)],
                                     startPoint: .top, endPoint: .bottom).clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous)))
                .accessibilityLabel("Script").accessibilityIdentifier("teleprompter-script-editor")
            Text(caption).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 6) {
                Button("Use script", action: useScript)
                    .buttonStyle(KriaButtonStyle(role: trimmed.isEmpty ? .secondary : .primary, fullWidth: true))
                    .disabled(trimmed.isEmpty)
                    .accessibilityIdentifier("teleprompter-use-script")
                Button("Record without a script", action: skip)
                    .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true))
                    .accessibilityIdentifier("teleprompter-no-script")
            }
        }
    }
}

/// A take (or any clip) shown at its own 9:16 size, filling the frame with no black bars, with a play control and a
/// scrubber (Paper S3.12).
struct ReviewTakePlayer: View {
    let url: URL
    @State private var player = AVPlayer()
    @State private var isPlaying = false
    @State private var elapsed: Double = 0
    @State private var duration: Double = 0
    @State private var scrubbing = false
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    private var elapsedLabel: some View { Text(DurationFormatter.clock(elapsed)).kriaType(.label).monospacedDigit().fixedSize() }
    private var durationLabel: some View { Text(DurationFormatter.clock(duration)).kriaType(.label).monospacedDigit().fixedSize() }

    var body: some View {
        ZStack(alignment: .bottom) {
            ReviewPlayerSurface(player: player).background(Color.black)
            Button(action: toggle) {
                ZStack {
                    Color.clear
                    if !isPlaying {
                        Image(systemName: "play.fill").font(.system(size: 22)).foregroundStyle(KriaColor.ink)
                            .frame(width: 56, height: 56).background(KriaColor.paper.opacity(0.95), in: Circle())
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain).frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityLabel(isPlaying ? "Pause take" : "Play take")
            .accessibilityIdentifier(isPlaying ? "review-pause" : "review-play")
            // The player can be narrow beside tall controls at accessibility sizes, so the times drop under the scrubber.
            Group {
                if dynamicTypeSize.isAccessibilitySize {
                    VStack(spacing: 4) {
                        scrubber
                        HStack { elapsedLabel; Spacer(minLength: 8); durationLabel }
                    }
                } else {
                    HStack(spacing: 10) { elapsedLabel; scrubber; durationLabel }
                }
            }
            .foregroundStyle(Color.white)
            .padding(.horizontal, 16).padding(.top, 28).padding(.bottom, 12)
            .background(LinearGradient(colors: [.clear, .black.opacity(0.55)], startPoint: .top, endPoint: .bottom))
        }
        .aspectRatio(9.0 / 16.0, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .task(id: url) {
            let item = AVPlayerItem(url: url)
            player.replaceCurrentItem(with: item)
            duration = (try? await item.asset.load(.duration).seconds) ?? 0
            if !duration.isFinite { duration = 0 }
            let interval = CMTime(seconds: 0.1, preferredTimescale: 600)
            for await _ in Self.ticks(player: player, interval: interval) {
                if !scrubbing { elapsed = player.currentTime().seconds.isFinite ? player.currentTime().seconds : 0 }
                isPlaying = player.timeControlStatus != .paused
                if duration > 0, elapsed >= duration - 0.05, isPlaying { player.pause(); await player.seek(to: .zero); elapsed = 0 }
            }
        }
        .onDisappear { player.pause() }
    }

    private var scrubber: some View {
        GeometryReader { proxy in
            let fraction = duration > 0 ? min(1, max(0, elapsed / duration)) : 0
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.35)).frame(height: 3)
                Capsule().fill(Color.white).frame(width: proxy.size.width * fraction, height: 3)
                Circle().fill(Color.white).frame(width: 10, height: 10).offset(x: max(0, proxy.size.width * fraction - 5))
            }
            .frame(maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                scrubbing = true
                elapsed = duration * min(1, max(0, value.location.x / max(1, proxy.size.width)))
            }.onEnded { _ in
                scrubbing = false
                player.seek(to: CMTime(seconds: elapsed, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
            })
        }
        .frame(height: 28)
        // VoiceOver gets a real slider, so the position can be swiped to seek instead of dragged.
        .accessibilityRepresentation {
            Slider(value: Binding(get: { elapsed }, set: { seek(to: $0) }), in: 0...max(duration, 1))
                .accessibilityLabel("Playback position")
                .accessibilityValue("\(DurationFormatter.clock(elapsed)) of \(DurationFormatter.clock(duration))")
                .accessibilityIdentifier("review-scrubber")
        }
    }

    private func seek(to seconds: Double) {
        elapsed = min(duration, max(0, seconds))
        player.seek(to: CMTime(seconds: elapsed, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }

    private func toggle() {
        if player.timeControlStatus == .paused { player.play() } else { player.pause() }
        isPlaying = player.timeControlStatus != .paused
    }

    private static func ticks(player: AVPlayer, interval: CMTime) -> AsyncStream<Void> {
        AsyncStream { continuation in
            let token = player.addPeriodicTimeObserver(forInterval: interval, queue: .main) { _ in continuation.yield() }
            continuation.onTermination = { _ in player.removeTimeObserver(token) }
        }
    }
}

private struct ReviewPlayerSurface: UIViewRepresentable {
    let player: AVPlayer
    final class View: UIView {
        override class var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
    }
    func makeUIView(context: Context) -> View {
        let view = View()
        view.playerLayer.player = player
        view.playerLayer.videoGravity = .resizeAspectFill
        return view
    }
    func updateUIView(_ uiView: View, context: Context) { uiView.playerLayer.player = player }
}


/// The script sheet shown before the camera opens (from the Add media Script row or the composer's Add script). It
/// edits the same saved draft the camera reads, so the words are waiting when the camera opens.
struct TeleprompterScriptEntrySheet: View {
    let accountID: String
    let projectID: UUID
    /// `true` = "Use script", `false` = "Record without a script".
    let finish: (Bool) -> Void
    @StateObject private var script = TeleprompterScript()
    @State private var store: TeleprompterStore?

    var body: some View {
        TeleprompterScriptSheet(script: script, save: { try? store?.save(draft: script.draft) },
                                useScript: { try? store?.save(draft: script.draft); finish(true) },
                                skip: { finish(false) })
            .onAppear {
                let store = TeleprompterStore(accountID: accountID, projectID: projectID)
                self.store = store
                if let draft = try? store.loadDraft() { script.draft = draft }
            }
    }
}
