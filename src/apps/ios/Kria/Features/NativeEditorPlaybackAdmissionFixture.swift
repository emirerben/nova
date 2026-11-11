#if DEBUG
import AVFoundation
import SwiftUI
import UIKit

/// Foreground-only integration fixture. XCUITest owns launch/activation; every frame receipt
/// still comes from the production preview and compositor.
struct NativeEditorPlaybackAdmissionFixture: View {
    @StateObject private var probe = NativeEditorPlaybackAdmissionProbe()

    var body: some View {
        VStack(spacing: 12) {
            NativeVideoPreview(session: probe.session)
                .frame(width: 220, height: 390)
                .accessibilityIdentifier("playback-admission-preview")
                .onAppear { probe.session.beginPreviewPresentation() }
                .onDisappear { probe.stop() }
            Button("Run playing title update") { probe.start() }
                .disabled(probe.report.phase != "ready")
                .accessibilityIdentifier("playback-admission-run")
            Text(probe.report.phase)
                .accessibilityIdentifier("playback-admission-phase")
            Text("Playback admission diagnostics")
                .accessibilityIdentifier("playback-admission-result")
                .accessibilityValue(probe.reportJSON)
        }
        .task { await probe.prepare() }
    }
}

private struct NativeEditorPlaybackAdmissionReport: Encodable {
    var phase = "preparing"
    var error: String?
    var sceneActive = false
    var sourceDuration = 0.0
    var initialAdmitted = false
    var beganPlaying = false
    var editTime = 0.0
    var textChangedAfterEdit = false
    var pendingAfterEdit = false
    var keptPlayingAfterEdit = false
    var settled = false
    var samePlayer = false
    var sameItem = false
    var sameCompositor = false
    var compositionChanged = false
    var ticketChanged = false
    var previewReady = false
    var previousPreviewAbsent = false
    var finalTime = 0.0
    var finalRate = 0.0
    var currentText = ""
}

@MainActor private final class NativeEditorPlaybackAdmissionProbe: ObservableObject {
    let session = NativeEditorSession(draft: NativeEditorUITestFixtures.draft(for: .sourceText),
                                      operations: LocalEditorOperations())
    @Published private(set) var report = NativeEditorPlaybackAdmissionReport()
    private var runTask: Task<Void, Never>?

    var reportJSON: String {
        guard let data = try? JSONEncoder().encode(report), let text = String(data: data, encoding: .utf8) else {
            return #"{"phase":"failed","error":"Could not encode playback diagnostics"}"#
        }
        return text
    }

    private enum Failure: Error { case unmet(String) }

    private func waitFor(_ timeout: Duration, _ condition: () -> Bool) async throws -> Bool {
        let deadline = ContinuousClock.now + timeout
        while ContinuousClock.now < deadline {
            try Task.checkCancellation()
            if condition() { return true }
            try await Task.sleep(for: .milliseconds(10))
        }
        return condition()
    }

    func prepare() async {
        do {
            report.sceneActive = try await waitFor(.seconds(5)) {
                UIApplication.shared.applicationState == .active && UIApplication.shared.connectedScenes
                    .compactMap { $0 as? UIWindowScene }.contains { $0.activationState == .foregroundActive }
            }
            guard report.sceneActive else { throw Failure.unmet("The UI fixture must own an active foreground scene") }
            guard let file = Bundle.main.url(forResource: "montage", withExtension: "mp4") else {
                throw Failure.unmet("The bundled montage is missing")
            }
            let duration = try await AVURLAsset(url: file).load(.duration).seconds
            guard duration.isFinite, duration >= 4 else { throw Failure.unmet("The source must last at least four seconds") }
            report.sourceDuration = duration
            await session.prepareFixtureSourcePreview(url: file)
            report.initialAdmitted = try await waitFor(.seconds(10)) {
                self.session.sourcePreviewState == .ready && self.session.admissiblePreviewDisplayTicket != nil
                    && !self.session.isPreparingCurrentPreview && self.session.player?.currentItem?.status == .readyToPlay
            }
            guard report.initialAdmitted else { throw Failure.unmet("The production surface never admitted the initial frame") }
            report.phase = "ready"
        } catch {
            if !Task.isCancelled { fail(error) }
        }
    }

    func start() {
        guard report.phase == "ready", runTask == nil else { return }
        report.phase = "running"
        runTask = Task { [weak self] in await self?.exercise() }
    }

    func stop() {
        runTask?.cancel()
        session.pausePlayback()
        session.endPreviewPresentation()
    }

    private func exercise() async {
        defer { session.pausePlayback() }
        do {
            guard let player = session.player, let item = player.currentItem,
                  let composition = item.videoComposition, let compositor = item.customVideoCompositor,
                  let initialTicket = session.admissiblePreviewDisplayTicket,
                  let title = session.document.textElements.first else {
                throw Failure.unmet("The admitted preview is missing its playback identities or title")
            }
            session.togglePlayback()
            report.beganPlaying = try await waitFor(.seconds(2)) {
                self.session.isPlaying && player.rate > 0 && player.currentTime().seconds >= 0.05
            }
            guard report.beganPlaying else { throw Failure.unmet("The real playback clock did not advance") }
            report.editTime = player.currentTime().seconds
            guard report.editTime < 1 else { throw Failure.unmet("The edit must start in the fixture's first second") }
            session.updateTextContent(id: title.id, content: "A clearer story")
            report.textChangedAfterEdit = session.document.textElements.first(where: { $0.id == title.id })?.text == "A clearer story"
            report.pendingAfterEdit = session.isPreparingCurrentPreview
            report.keptPlayingAfterEdit = session.isPlaying
            report.settled = try await waitFor(.seconds(2)) {
                self.session.admissiblePreviewDisplayTicket != initialTicket
                    && item.videoComposition !== composition && !self.session.isPreparingCurrentPreview
                    && self.session.isPlaying && player.rate > 0
                    && player.currentTime().seconds > self.report.editTime + 0.1
            }
            report.samePlayer = session.player === player
            report.sameItem = player.currentItem === item
            report.sameCompositor = item.customVideoCompositor.map { ($0 as AnyObject) === (compositor as AnyObject) } ?? false
            report.compositionChanged = item.videoComposition !== composition
            report.ticketChanged = session.admissiblePreviewDisplayTicket != initialTicket
            report.previewReady = session.sourcePreviewState == .ready
            report.previousPreviewAbsent = session.previousPreviewPresentation == nil
            report.finalTime = player.currentTime().seconds
            report.finalRate = Double(player.rate)
            report.currentText = session.document.textElements.first(where: { $0.id == title.id })?.text ?? ""
            guard report.settled else { throw Failure.unmet("The new revision did not settle while real playback advanced") }
            try Task.checkCancellation()
            report.phase = "complete"
        } catch {
            if !Task.isCancelled { fail(error) }
        }
    }

    private func fail(_ error: Error) {
        report.error = String(describing: error)
        report.phase = "failed"
    }
}
#endif
