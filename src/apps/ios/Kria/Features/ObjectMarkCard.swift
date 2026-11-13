import AVFoundation
import KriaMediaEngine
import SwiftUI

// KRI-901: "Mark the plate". The server keeps text off an object only where the creator marked it, so before
// approval it asks for a box on one frame of one of their clips. The card shows the clips, the UPRIGHT frame from
// this iPhone's original (the space the server tracks in), a moment slider and a free-shape rectangle that moves
// and resizes with the same math as the crop editor (no shape presets: they mean nothing for a mark).

/// How a mark question is shown under its message.
enum ObjectMarkCardMode {
    /// The newest unanswered question: interactive. `problem` is the server's reason for refusing the last mark.
    case active(isSending: Bool, problem: String?, submit: (ProtectedRegionAnswer) -> Void)
    /// Answered by a `protected_region` message: one read-only line ("Marked the plate").
    case answered(summary: String)
    /// A newer question replaced this one before it was answered.
    case superseded
}

/// Finds a clip's original on this iPhone (phone projects keep originals in the project directory).
@MainActor enum ObjectMarkSource {
    static func localURL(projectID: UUID?, mediaID: String) -> URL? {
        #if DEBUG
        if let fixture = ObjectMarkUITestFixture.clipURL(mediaID: mediaID) { return fixture }
        #endif
        guard let projectID else { return nil }
        return CreationMediaPreview.localOriginal(mediaID: mediaID, projectID: projectID)
            ?? SongOrderPreviewSource.localURL(projectID: projectID, mediaID: mediaID)
    }
}

struct ObjectMarkCard: View {
    let question: ProtectedRegionQuestion
    var projectID: UUID? = nil
    let mode: ObjectMarkCardMode

    var body: some View {
        switch mode {
        case .answered(let summary):
            KriaAnsweredReceipt(text: summary)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(summary)
                .accessibilityIdentifier("object-mark-answered")
        case .superseded:
            KriaSupersededLine().accessibilityIdentifier("object-mark-superseded")
        case .active(let isSending, let problem, let submit):
            ObjectMarkActiveCard(question: question, projectID: projectID, isSending: isSending, problem: problem, submit: submit)
        }
    }
}

private struct ObjectMarkActiveCard: View {
    let question: ProtectedRegionQuestion
    let projectID: UUID?
    let isSending: Bool
    let problem: String?
    let submit: (ProtectedRegionAnswer) -> Void

    /// The default box: the middle of the frame, where a plate in a talking shot usually sits.
    static let initialRect = NormalizedSourceRect(x: 0.25, y: 0.4, width: 0.5, height: 0.3)

    @State private var mediaIndex = 0
    @State private var time: Double = 0
    @State private var duration: Double = 0
    @State private var frameImage: UIImage?
    @State private var loadedKey: String?
    @State private var rect = ObjectMarkActiveCard.initialRect
    @State private var dragStart: NormalizedSourceRect?
    @State private var resizeStart: NormalizedSourceRect?
    /// A system-cancelled drag never reaches `onEnded`; these let `onChange` clear the stale start rects.
    @GestureState private var moveActive = false
    @GestureState private var resizeActive = false
    /// The server's reason is only true for the box that was sent: any change hides it.
    @State private var problemSeen = false

    private var mediaID: String { question.mediaIDs[min(mediaIndex, question.mediaIDs.count - 1)] }
    private var localURL: URL? { ObjectMarkSource.localURL(projectID: projectID, mediaID: mediaID) }
    private var frameKey: String { "\(mediaID)@\(Int((time * 10).rounded()))" }
    private var answer: ProtectedRegionAnswer {
        ProtectedRegionAnswer(label: question.label, mediaID: mediaID, sourceTimeS: time, rect: rect)
    }

    private var frameIsReady: Bool { frameImage != nil && loadedKey == frameKey }
    private var isLoadingFrame: Bool { localURL != nil && !frameIsReady }

    var body: some View {
        KriaQuestionCard(title: "Draw a box around the \(question.label)",
                         helper: "Pick a moment where it’s in view while you talk.") {
            if question.mediaIDs.count > 1 { clipPicker }
            frameArea
            if duration > 0.2, localURL != nil {
                HStack(spacing: 12) {
                    Text(Self.readout(time))
                        .kriaType(.label).monospacedDigit().foregroundStyle(KriaColor.ink)
                        .frame(minWidth: 44, alignment: .leading)
                        .accessibilityHidden(true)
                    Slider(value: $time, in: 0...duration) { Text("Moment") }
                        .tint(KriaColor.ink)
                        .accessibilityLabel("Moment in the clip")
                        .accessibilityValue(String(format: "%.1f seconds", time))
                        .accessibilityIdentifier("object-mark-time")
                        .disabled(isSending)
                }
            }
            if let problem, !problemSeen {
                KriaInlineFailure(text: problem).accessibilityIdentifier("object-mark-error")
            }
            Button(question.actionTitle) { submit(answer) }
                .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, onFill: true, isLoading: isSending))
                // The box is sent with `time`: only once that moment's frame is the one on screen.
                .disabled(isSending || !frameIsReady)
                .kriaDisabledReason(isLoadingFrame ? "Wait for the frame to load." : nil)
                .accessibilityIdentifier("object-mark-send")
        }
        .excludesDrawerGesture()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("object-mark-card")
        .onChange(of: problem) { _, _ in problemSeen = false }
        .onChange(of: rect) { _, _ in problemSeen = true }
        .task(id: mediaID) { await loadDuration() }
        .task(id: frameKey) { await loadFrame() }
    }

    /// 0:04.2, on the same clock as the slider.
    private static func readout(_ seconds: Double) -> String {
        let tenths = Int((max(0, seconds) * 10).rounded())
        return String(format: "%d:%02d.%d", tenths / 600, (tenths / 10) % 60, tenths % 10)
    }

    private var clipPicker: some View {
        HStack(spacing: 8) {
            ForEach(Array(question.mediaIDs.enumerated()), id: \.offset) { index, _ in
                let selected = index == mediaIndex
                Button {
                    guard !selected else { return }
                    mediaIndex = index
                    time = 0
                    frameImage = nil
                } label: {
                    Text("Clip \(index + 1)").kriaType(.button)
                        .foregroundStyle(selected ? KriaColor.paper : KriaColor.ink)
                        .padding(.horizontal, 20).padding(.vertical, 8)
                        .frame(minHeight: 44)
                        .background(selected ? KriaColor.ink : KriaColor.paper, in: Capsule())
                        .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selected ? .isSelected : [])
                .accessibilityIdentifier("object-mark-clip-\(index + 1)")
                .disabled(isSending)
            }
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder private var frameArea: some View {
        if localURL == nil {
            Text(question.mediaIDs.count > 1
                 ? "This clip isn’t on this iPhone. Pick another clip."
                 : "This clip isn’t on this iPhone, so it can’t be marked here.")
                .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
                .frame(maxWidth: .infinity, minHeight: 80)
                .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                .accessibilityIdentifier("object-mark-missing")
        } else {
            GeometryReader { proxy in
                let image = frameImage
                let aspect = image.map { $0.size.height > 0 ? $0.size.width / $0.size.height : 9.0 / 16 } ?? 9.0 / 16
                let drawn = KriaCropGeometry.fitted(aspect: aspect, in: proxy.size)
                ZStack(alignment: .topLeading) {
                    if let image {
                        Image(uiImage: image).resizable().scaledToFit()
                            .frame(width: drawn.width, height: drawn.height)
                            .offset(x: drawn.minX, y: drawn.minY)
                            .accessibilityHidden(true)
                        markOverlay(in: drawn)
                    }
                    if !frameIsReady && image == nil {
                        HStack(spacing: 8) {
                            KriaSpinner(size: 20)
                            Text("Loading the frame…").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    }
                }
            }
            .frame(height: 300)
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("object-mark-frame")
        }
    }

    @ViewBuilder private func markOverlay(in drawn: CGRect) -> some View {
        let box = KriaCropGeometry.viewRect(rect, in: drawn)
        Rectangle()
            .fill(Color.white.opacity(0.001))
            .overlay(Rectangle().stroke(Color.white, lineWidth: 2))
            .shadow(color: .black.opacity(0.35), radius: 2)
            .frame(width: box.width, height: box.height)
            .offset(x: box.minX, y: box.minY)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 2)
                    .updating($moveActive) { _, active, _ in active = true }
                    .onChanged { value in
                        let start = dragStart ?? rect
                        dragStart = start
                        rect = KriaCropGeometry.moved(start, dx: value.translation.width / max(drawn.width, 1),
                                                      dy: value.translation.height / max(drawn.height, 1))
                    }
                    .onEnded { _ in dragStart = nil }
            )
            .onChange(of: moveActive) { _, active in if !active { dragStart = nil } }
            .accessibilityElement()
            .accessibilityLabel("The \(question.label)")
            .accessibilityValue(answer.box.map { String(format: "%.4f", $0) }.joined(separator: ","))
            .accessibilityIdentifier("object-mark-rect")
        ForEach(KriaCropGeometry.Corner.allCases, id: \.self) { corner in handle(corner, box: box, drawn: drawn) }
    }

    /// A 22pt white disc with a soft ring, in a 44pt hit area centred on the box corner.
    private func handle(_ corner: KriaCropGeometry.Corner, box: CGRect, drawn: CGRect) -> some View {
        let leading = corner == .topLeading || corner == .bottomLeading
        let top = corner == .topLeading || corner == .topTrailing
        return Circle().fill(Color.white)
            .frame(width: 22, height: 22)
            .shadow(color: .black.opacity(0.25), radius: 2, y: 1)
            .frame(width: 44, height: 44)
            .background(Circle().fill(Color.white.opacity(0.3)).frame(width: 44, height: 44))
            .contentShape(Rectangle())
            .offset(x: (leading ? box.minX : box.maxX) - 22, y: (top ? box.minY : box.maxY) - 22)
            .gesture(
                DragGesture(minimumDistance: 1)
                    .updating($resizeActive) { _, active, _ in active = true }
                    .onChanged { value in
                        let start = resizeStart ?? rect
                        resizeStart = start
                        rect = KriaCropGeometry.resized(start, corner: corner,
                                                        dx: value.translation.width / max(drawn.width, 1),
                                                        dy: value.translation.height / max(drawn.height, 1))
                    }
                    .onEnded { _ in resizeStart = nil }
            )
            .onChange(of: resizeActive) { _, active in if !active { resizeStart = nil } }
            .accessibilityLabel("Resize the box")
            .accessibilityIdentifier(corner == .bottomTrailing ? "object-mark-resize" : "object-mark-resize-\(corner)")
    }

    private func loadDuration() async {
        guard let url = localURL, !NativeDownloadedMedia.isStillImage(url) else { duration = 0; return }
        let seconds = (try? await AVURLAsset(url: url).load(.duration)).map(CMTimeGetSeconds) ?? 0
        duration = seconds.isFinite && seconds > 0 ? seconds : 0
    }

    private func loadFrame() async {
        guard let url = localURL else { frameImage = nil; return }
        // Let a slider drag settle before decoding.
        if loadedKey != nil { try? await Task.sleep(for: .milliseconds(120)) }
        guard !Task.isCancelled else { return }
        let image = await KriaCropFrameLoader.image(at: url, time: time, maxPixel: 1080)
        guard !Task.isCancelled else { return }
        frameImage = image
        loadedKey = frameKey
    }
}

#if DEBUG
/// `KRIA_CHAT_MARK_OBJECT`: the first fixture clip has a local original (a generated clip), the second does not.
enum ObjectMarkUITestFixture {
    @MainActor static func clipURL(mediaID: String) -> URL? {
        guard ProcessInfo.processInfo.environment["KRIA_CHAT_MARK_OBJECT"] != nil, mediaID == "fixture-clip" else { return nil }
        return SongTimelineFixture.clipURL(mediaID: mediaID, force: true)
    }
}
#endif
