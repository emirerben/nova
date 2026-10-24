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
            HStack(spacing: 8) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(KriaColor.success)
                    .accessibilityHidden(true)
                Text(summary)
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(minHeight: 44, alignment: .leading)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(summary)
            .accessibilityIdentifier("object-mark-answered")
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
    /// The server's reason is only true for the box that was sent: any change hides it.
    @State private var problemSeen = false

    private var mediaID: String { question.mediaIDs[min(mediaIndex, question.mediaIDs.count - 1)] }
    private var localURL: URL? { ObjectMarkSource.localURL(projectID: projectID, mediaID: mediaID) }
    private var frameKey: String { "\(mediaID)@\(Int((time * 10).rounded()))" }
    private var answer: ProtectedRegionAnswer {
        ProtectedRegionAnswer(label: question.label, mediaID: mediaID, sourceTimeS: time, rect: rect)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Draw a box around the \(question.label) on the clip where it's in view while you talk.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
            if question.mediaIDs.count > 1 { clipPicker }
            frameArea
            if duration > 0.2, localURL != nil {
                Slider(value: $time, in: 0...duration) { Text("Moment") }
                    .accessibilityLabel("Moment in the clip")
                    .accessibilityValue(String(format: "%.1f seconds", time))
                    .accessibilityIdentifier("object-mark-time")
                    .disabled(isSending)
            }
            if let problem, !problemSeen {
                Text(problem)
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("object-mark-error")
            }
            Button(question.actionTitle) { submit(answer) }
                .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
                .frame(maxWidth: .infinity)
                // The box is sent with `time`: only once that moment's frame is the one on screen.
                .disabled(isSending || frameImage == nil || loadedKey != frameKey)
                .accessibilityIdentifier("object-mark-send")
        }
        .padding(14)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .excludesDrawerGesture()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("object-mark-card")
        .onChange(of: problem) { _, _ in problemSeen = false }
        .onChange(of: rect) { _, _ in problemSeen = true }
        .task(id: mediaID) { await loadDuration() }
        .task(id: frameKey) { await loadFrame() }
    }

    private var clipPicker: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(Array(question.mediaIDs.enumerated()), id: \.offset) { index, _ in
                    Button("Clip \(index + 1)") {
                        guard index != mediaIndex else { return }
                        mediaIndex = index
                        time = 0
                        frameImage = nil
                    }
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(index == mediaIndex ? Color.white : KriaColor.ink)
                    .padding(.horizontal, 14)
                    .frame(minHeight: 44)
                    .background(index == mediaIndex ? KriaColor.ink : KriaColor.fill, in: Capsule())
                    .accessibilityAddTraits(index == mediaIndex ? .isSelected : [])
                    .accessibilityIdentifier("object-mark-clip-\(index + 1)")
                    .disabled(isSending)
                }
            }
        }
    }

    @ViewBuilder private var frameArea: some View {
        if localURL == nil {
            Text(question.mediaIDs.count > 1
                 ? "This clip isn't on this iPhone. Pick another clip."
                 : "This clip isn't on this iPhone, so it can't be marked here.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, minHeight: 120)
                .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
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
                    } else {
                        ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }
            }
            .frame(height: 300)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("object-mark-frame")
        }
    }

    @ViewBuilder private func markOverlay(in drawn: CGRect) -> some View {
        let box = KriaCropGeometry.viewRect(rect, in: drawn)
        RoundedRectangle(cornerRadius: 6, style: .continuous)
            .fill(Color.white.opacity(0.12))
            .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).stroke(Color.white, lineWidth: 2))
            .shadow(color: .black.opacity(0.35), radius: 2)
            .frame(width: box.width, height: box.height)
            .offset(x: box.minX, y: box.minY)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 2)
                    .onChanged { value in
                        let start = dragStart ?? rect
                        dragStart = start
                        rect = KriaCropGeometry.moved(start, dx: value.translation.width / max(drawn.width, 1),
                                                      dy: value.translation.height / max(drawn.height, 1))
                    }
                    .onEnded { _ in dragStart = nil }
            )
            .accessibilityElement()
            .accessibilityLabel("The \(question.label)")
            .accessibilityValue(answer.box.map { String(format: "%.4f", $0) }.joined(separator: ","))
            .accessibilityIdentifier("object-mark-rect")
        Circle()
            .fill(Color.white)
            .overlay(Circle().stroke(KriaColor.ink, lineWidth: 2))
            .frame(width: 22, height: 22)
            .frame(width: 44, height: 44)
            .contentShape(Rectangle())
            .offset(x: box.maxX - 22, y: box.maxY - 22)
            .gesture(
                DragGesture(minimumDistance: 1)
                    .onChanged { value in
                        let start = resizeStart ?? rect
                        resizeStart = start
                        rect = KriaCropGeometry.resized(start, corner: .bottomTrailing,
                                                        dx: value.translation.width / max(drawn.width, 1),
                                                        dy: value.translation.height / max(drawn.height, 1))
                    }
                    .onEnded { _ in resizeStart = nil }
            )
            .accessibilityLabel("Resize the box")
            .accessibilityIdentifier("object-mark-resize")
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
