import AVFoundation
import SwiftUI
import UIKit

/// A compact, independently pausable preview for the final creation format.
struct SlidePostFormatCover: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var index = 0
    @Binding var paused: Bool
    @State private var mostlyVisible = true
    private let size = CGSize(width: 156, height: 156)
    private var advances: Bool { !reduceMotion && !paused && mostlyVisible && scenePhase == .active }
    private var displayedIndex: Int { index % 3 }

    var body: some View {
        GeometryReader { proxy in
            // The strip is wider than its viewport. Leading alignment is
            // essential: a centered ZStack would start on the second frame.
            ZStack(alignment: .bottomLeading) {
                HStack(spacing: 0) {
                    image("trulli-street")
                    SlideCoverVideo(playing: index == 1 && advances).frame(width: size.width, height: size.height)
                    image("lisbon")
                    // Duplicate the first image so the final swipe is a real
                    // third-to-first transition. We reset to index zero only
                    // after this identical frame has held.
                    image("trulli-street")
                }
                .frame(width: size.width * 4, height: size.height, alignment: .leading)
                .offset(x: -CGFloat(index) * size.width)
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.6), value: index)
                HStack(spacing: 6) {
                    Text("\(displayedIndex + 1)/3").font(KriaFont.body(11).weight(.semibold)).monospacedDigit()
                    ForEach(0..<3, id: \.self) { item in
                        Capsule().fill(item == displayedIndex ? Color.white : Color.white.opacity(0.5)).frame(width: item == displayedIndex ? 12 : 5, height: 5)
                    }
                    Spacer(minLength: 0)
                }
                .foregroundStyle(.white).padding(.leading, 9).padding(.bottom, 5)
            }
            .frame(width: proxy.size.width, height: proxy.size.height).clipped()
            .onAppear { visibility(proxy.frame(in: .global)) }
            .onChange(of: proxy.frame(in: .global)) { _, frame in visibility(frame) }
        }
        .allowsHitTesting(false)
        .task(id: advances) {
            guard advances else { return }
            // An app/background pause can cancel the task during the duplicate
            // frame's 600ms travel. Resume from the real first frame, never an
            // out-of-range fourth index.
            if index == 3 {
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { index = 0 }
            }
            while !Task.isCancelled {
                // 2.7 seconds fully still, then a 0.6-second swipe. The
                // transition wait makes the next hold start after the travel,
                // so each logical frame occupies 3.3 seconds.
                do { try await Task.sleep(for: .milliseconds(2700)) } catch { return }
                guard advances else { return }
                let wraps = index == 2
                withAnimation(.easeInOut(duration: 0.6)) { index += 1 }
                do { try await Task.sleep(for: .milliseconds(600)) } catch { return }
                if wraps {
                    var transaction = Transaction()
                    transaction.disablesAnimations = true
                    withTransaction(transaction) { index = 0 }
                }
            }
        }
        .onChange(of: reduceMotion) { _, enabled in if enabled { index = 0 } }
    }

    private func image(_ name: String) -> some View {
        Group {
            if let url = Bundle.main.url(forResource: name, withExtension: "jpg"), let uiImage = UIImage(contentsOfFile: url.path) { Image(uiImage: uiImage).resizable().scaledToFill() }
            else { KriaColor.softZinc.overlay { Image(systemName: "photo") } }
        }.frame(width: size.width, height: size.height).clipped()
    }

    private func visibility(_ frame: CGRect) {
        let visible = frame.intersection(UIScreen.main.bounds)
        mostlyVisible = visible.width * visible.height / max(1, frame.width * frame.height) >= 0.65
    }
}

private struct SlideCoverVideo: UIViewRepresentable {
    let playing: Bool
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> PlayerSurface {
        let view = PlayerSurface()
        guard let url = Bundle.main.url(forResource: "istanbul", withExtension: "mp4") else { return view }
        let player = AVPlayer(url: url); player.isMuted = true; player.actionAtItemEnd = .none
        view.playerLayer.player = player; context.coordinator.player = player
        context.coordinator.loop = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: player.currentItem, queue: .main) { _ in
            player.seek(to: .zero); if context.coordinator.playing { player.play() }
        }
        return view
    }
    func updateUIView(_ view: PlayerSurface, context: Context) {
        context.coordinator.playing = playing
        if playing { context.coordinator.player?.play() } else { context.coordinator.player?.pause() }
    }
    static func dismantleUIView(_ view: PlayerSurface, coordinator: Coordinator) { coordinator.player?.pause(); if let loop = coordinator.loop { NotificationCenter.default.removeObserver(loop) } }
    final class Coordinator { var player: AVPlayer?; var loop: NSObjectProtocol?; var playing = false }
    final class PlayerSurface: UIView {
        override class var layerClass: AnyClass { AVPlayerLayer.self }
        var playerLayer: AVPlayerLayer { super.layer as! AVPlayerLayer }
        override init(frame: CGRect) { super.init(frame: frame); playerLayer.videoGravity = .resizeAspectFill; backgroundColor = .black }
        required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    }
}
