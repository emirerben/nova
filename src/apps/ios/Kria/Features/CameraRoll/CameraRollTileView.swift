import SwiftUI

/// Short wording that fits on a 112 pt tile. The full sentence (`CameraRollItemReason.message`) goes to VoiceOver.
extension CameraRollItemReason {
    var shortLabel: String {
        switch self {
        case .providerError: "Couldn't analyze"
        case .budgetPaused: "Paused for now"
        case .unsupportedType: "Not supported"
        case .tooLarge: "Too large"
        case .tooLong: "Too long"
        case .icloudUnavailable: "iCloud unavailable"
        case .noUsableMoment: "No usable moment"
        case .accessRevoked: "No access"
        case .uploadFailed: "Upload failed"
        }
    }
}

/// How a tile behaves while the creator is moving items between events.
enum CameraRollTileSelection: Equatable {
    case off
    case available(isSelected: Bool)
}

/// One tile of the contact sheet. It reads only its own `CameraRollTile`, so a progress tick redraws this tile and
/// nothing else. Motion (Paper "Tile states and motion"): a light sweep while Kria looks, a check that pops once when
/// the item becomes ready, one 6 pt shake when it fails, a ring that fills with real upload progress. Reduce Motion
/// keeps every state readable and removes all movement.
struct CameraRollTileView: View {
    let entry: CameraRollCollection.Entry
    let tile: CameraRollTile
    let api: any KriaAPIClient
    var thumbnailsDisabled = false
    /// Width over height. The contact sheet is square; a collapsed event shows the 112 x 72 strip.
    var aspect: CGFloat = 1
    var selection: CameraRollTileSelection = .off
    var onRetry: () -> Void = {}
    var onToggle: () -> Void = {}

    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @State private var checkScale: CGFloat = 1
    @State private var shakes = 0
    private var reduceMotion: Bool { KriaMotion.isReduced(systemReduceMotion) }

    var body: some View {
        let state = tile.state
        Color.clear.aspectRatio(aspect, contentMode: .fit)
            .overlay {
                GeometryReader { proxy in
                    ZStack {
                        CameraRollCollectionThumbnail(entry: entry, api: api, metadataOnly: thumbnailsDisabled, size: proxy.size)
                        stateLayers(state, size: proxy.size)
                    }
                }
            }
            .overlay(alignment: .topLeading) { durationBadge(state) }
            .overlay(alignment: .topTrailing) { selectionMark }
            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .keyframeAnimator(initialValue: CGFloat(0), trigger: shakes) { content, offset in
                content.offset(x: offset)
            } keyframes: { _ in
                KeyframeTrack {
                    // 6 pt each way, settling, 280 ms in all.
                    CubicKeyframe(-6, duration: 0.05)
                    CubicKeyframe(6, duration: 0.07)
                    CubicKeyframe(-4, duration: 0.06)
                    CubicKeyframe(4, duration: 0.06)
                    CubicKeyframe(0, duration: 0.04)
                }
            }
            .contentShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
            .onTapGesture { if selection != .off { onToggle() } }
            .onChange(of: state.phase) { old, new in
                if new == .ready, old != .ready, !reduceMotion {
                    checkScale = 0.6
                    withAnimation(.spring(duration: 0.22, bounce: 0.35)) { checkScale = 1 }
                }
                if new == .needsRetry, old != .needsRetry, !reduceMotion { shakes += 1 }
            }
            .accessibilityElement(children: .contain)
            .accessibilityLabel(accessibilityLabel(state))
            .accessibilityValue(state.phase.rawValue)
            .accessibilityIdentifier("camera-roll-tile-\(entry.id)")
            .accessibilityAddTraits(selection == .available(isSelected: true) ? .isSelected : [])
    }

    // MARK: Layers

    @ViewBuilder
    private func stateLayers(_ state: CameraRollItemState, size: CGSize) -> some View {
        switch state {
        case .picking:
            KriaColor.fill.opacity(0.55)
            statusPill("Waiting", dot: true)
        case .exporting:
            KriaColor.fill.opacity(0.55)
            statusPill("Preparing", spinner: true)
        case let .uploading(progress):
            uploading(progress: progress, size: size)
        case .queued:
            KriaColor.fill.opacity(0.55)
            statusPill("Waiting", dot: true)
        case .analyzing:
            CameraRollShimmer()
            statusPill("Looking", spinner: true, butter: true)
        case .ready:
            readyMark
        case .needsRetry:
            needsRetry(size: size)
        case let .failed(reason), let .excluded(reason):
            cannotUse(reason, failed: state.phase == .failed)
        }
    }

    private func uploading(progress: Double, size: CGSize) -> some View {
        let ring = min(44, size.width * 0.4, size.height * 0.4)
        return ZStack {
            KriaColor.ink.opacity(0.38)
            if !reduceMotion {
                ZStack {
                    Circle().stroke(Color.white.opacity(0.35), lineWidth: 4)
                    Circle().trim(from: 0, to: max(0.04, progress))
                        .stroke(KriaColor.butter, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                        .animation(.easeOut(duration: 0.2), value: progress)
                }.frame(width: ring, height: ring)
            }
            VStack { Spacer(); Text("Uploading \(Int((progress * 100).rounded()))%")
                .font(KriaFont.body(11).weight(.semibold)).foregroundStyle(.white).padding(.bottom, 8) }
        }
    }

    private var readyMark: some View {
        VStack { Spacer(); HStack { Spacer()
            ZStack {
                Circle().fill(KriaColor.success)
                KriaCheckShape().stroke(.white, style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round)).frame(width: 12, height: 12)
            }.frame(width: 22, height: 22).scaleEffect(checkScale).padding(6)
        } }
    }

    private func needsRetry(size: CGSize) -> some View {
        ZStack {
            KriaColor.failureSoft.opacity(0.72)
            VStack(spacing: 0) {
                Text("Didn't finish").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.failureText)
                    .padding(.top, size.height * 0.27)
                Spacer(minLength: 0)
                Button(action: onRetry) {
                    HStack(spacing: 5) {
                        Image(systemName: "arrow.clockwise").font(.system(size: 10, weight: .bold))
                        Text("Try again").font(KriaFont.body(12).weight(.semibold))
                    }
                    .foregroundStyle(.white).frame(maxWidth: .infinity, minHeight: 30)
                    .background(KriaColor.failureText, in: Capsule()).contentShape(Capsule())
                }
                .buttonStyle(.plain).padding(6).disabled(selection != .off)
                .accessibilityLabel("Try again").accessibilityIdentifier("camera-roll-retry-\(entry.id)")
            }
        }
    }

    private func cannotUse(_ reason: CameraRollItemReason, failed: Bool) -> some View {
        VStack(spacing: 4) {
            Image(systemName: failed ? "exclamationmark.circle" : "info.circle")
                .font(.system(size: 18, weight: .medium)).foregroundStyle(KriaColor.zinc)
            Text("Can't use").font(KriaFont.body(11).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            Text(reason.shortLabel).font(KriaFont.body(10).weight(.medium)).foregroundStyle(KriaColor.zinc)
                .multilineTextAlignment(.center).lineLimit(2)
        }
        .padding(.horizontal, 6).frame(maxWidth: .infinity, maxHeight: .infinity).background(KriaColor.fill)
    }

    private func statusPill(_ text: String, dot: Bool = false, spinner: Bool = false, butter: Bool = false) -> some View {
        VStack { Spacer(); HStack {
            HStack(spacing: 5) {
                if dot { Circle().fill(KriaColor.zinc).frame(width: 6, height: 6) }
                if spinner { KriaSpinner(size: 11) }
                Text(text).font(KriaFont.body(11).weight(.semibold))
                    .foregroundStyle(butter ? KriaColor.ink : KriaColor.mutedInk)
            }
            .padding(.vertical, 4).padding(.leading, spinner ? 6 : 8).padding(.trailing, 8)
            .background(butter ? KriaColor.butter : Color.white, in: Capsule())
            Spacer()
        }.padding(6) }
    }

    @ViewBuilder
    private func durationBadge(_ state: CameraRollItemState) -> some View {
        if let seconds = duration, state.phase != .failed, state.phase != .excluded {
            Text(GalleryTileChrome.duration(seconds))
                .font(KriaFont.body(11).weight(.semibold)).foregroundStyle(.white)
                .padding(.horizontal, 7).padding(.vertical, 3)
                .background(KriaColor.ink.opacity(0.62), in: Capsule()).padding(6)
        }
    }

    @ViewBuilder
    private var selectionMark: some View {
        if case let .available(isSelected) = selection {
            ZStack {
                Circle().fill(isSelected ? KriaColor.ink : Color.white.opacity(0.7))
                if isSelected { KriaCheckShape().stroke(.white, style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round)).frame(width: 12, height: 12) }
            }.frame(width: 24, height: 24).padding(6)
        }
    }

    private var duration: Double? {
        if let metadata = entry.metadata, metadata.kind == "video", metadata.duration > 0 { return metadata.duration }
        if entry.savedAsset?.kind == "video", let raw = entry.savedAsset?.metadata["duration_s"], case .number(let value) = raw, value.isFinite, value > 0 { return value }
        return nil
    }

    private func accessibilityLabel(_ state: CameraRollItemState) -> String {
        let kind = (entry.metadata?.kind ?? entry.savedAsset?.kind) == "image" ? "Photo" : "Video"
        let status: String
        switch state.phase {
        case .picking, .queued: status = "waiting"
        case .exporting: status = "preparing"
        case .uploading: status = "uploading"
        case .analyzing: status = "being looked at"
        case .ready: status = "ready"
        case .needsRetry: status = "didn't finish. \(state.message ?? "")"
        case .failed, .excluded: status = "can't be used. \(state.message ?? "")"
        }
        return "\(kind), \(status)"
    }
}

/// A light band that crosses the picture for 0.58 s every 2.25 s: the same timing as the chat's thinking row. It
/// is driven the same way too (an animation per sweep, not a per-frame clock), so the app can still go idle.
struct CameraRollShimmer: View {
    @Environment(\.accessibilityReduceMotion) private var systemReduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var offset: CGFloat = -1

    private static let interval = 2.25
    private static let duration = 0.58
    private var animates: Bool { !KriaMotion.isReduced(systemReduceMotion) && scenePhase == .active }

    var body: some View {
        GeometryReader { proxy in
            if animates {
                LinearGradient(colors: [Color.white.opacity(0), Color.white.opacity(0.55), Color.white.opacity(0)],
                               startPoint: UnitPoint(x: 0, y: 0.2), endPoint: UnitPoint(x: 1, y: 0.8))
                    .frame(width: proxy.size.width * 0.7)
                    .offset(x: offset * proxy.size.width)
            }
        }
        .allowsHitTesting(false).accessibilityHidden(true)
        .task(id: animates) { await sweep() }
    }

    private func sweep() async {
        guard animates else { return reset() }
        while !Task.isCancelled {
            reset()
            withAnimation(.linear(duration: Self.duration)) { offset = 1.1 }
            do { try await Task.sleep(for: .seconds(Self.interval)) } catch { return }
        }
    }

    private func reset() {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { offset = -1 }
    }
}
