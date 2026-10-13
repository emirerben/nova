import SwiftUI
import UIKit

// MARK: Sticker layer (KRI-460)

/// The pure sticker layer of a slide: every picture centred, sized by its width fraction, turned about its centre, bottom to
/// top in list order. It draws from `SlidePostStickerGeometry`, the same function the on-device export and the server's
/// fixture are held to, so what the editor shows is where the export puts it.
struct SlidePostStickerLayerView: View {
    let stickers: [SlidePostSticker]
    let images: [String: UIImage]
    let size: CGSize
    /// Present only in the editor: VoiceOver labels and actions per picture. Export renders none.
    var accessibility: Accessibility? = nil

    struct Accessibility {
        var selectedID: String?
        var onSelect: (String) -> Void
        var onRemove: (String) -> Void
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            ForEach(stickers) { sticker in
                if let image = images[sticker.assetID] { stickerView(sticker, image: image) }
            }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
    }

    @ViewBuilder private func stickerView(_ sticker: SlidePostSticker, image: UIImage) -> some View {
        let placement = SlidePostStickerGeometry.placement(for: sticker, imageSize: image.size, canvas: size)
        let view = Image(uiImage: image).resizable()
            .frame(width: placement.size.width, height: placement.size.height)
            .rotationEffect(.degrees(placement.rotationDegrees))
            .offset(x: placement.rect.minX, y: placement.rect.minY)
        if let accessibility {
            let selected = accessibility.selectedID == sticker.id
            view
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Picture")
                .accessibilityValue(value(sticker, selected: selected))
                .accessibilityHint(selected ? "Drag to move, pinch to resize, twist to rotate" : "Double tap to select")
                .accessibilityAddTraits(selected ? .isSelected : [])
                .accessibilityAction(named: "Select") { accessibility.onSelect(sticker.id) }
                .accessibilityAction(named: "Remove") { accessibility.onRemove(sticker.id) }
                .accessibilityIdentifier("slidepost-canvas-sticker-\(sticker.id)")
        } else { view }
    }

    private func value(_ sticker: SlidePostSticker, selected: Bool) -> String {
        var parts = ["width \(Int((sticker.widthFrac * 100).rounded()))%", "rotation \(Int(sticker.rotationDeg))",
                     "position \(Int((sticker.xFrac * 100).rounded()))%, \(Int((sticker.yFrac * 100).rounded()))%"]
        if selected { parts.append("selected") }
        return parts.joined(separator: ", ")
    }
}

/// The editor's sticker layer: loads each picture (original bytes, so PNG alpha survives), draws them, and in browse mode lets
/// the creator touch one: a touch selects it, dragging moves it, the corner handle (or a pinch / twist) resizes and turns it,
/// the X removes it. Every sample goes through `onTransform` with one key per gesture, so undo steps back a whole gesture.
struct SlidePostStickerCanvas: View {
    let stickers: [SlidePostSticker]
    let assets: [SlidePostAsset]
    let size: CGSize
    let selectedID: String?
    /// Browse mode: pictures take touches. Everywhere else they are just drawn.
    let interactive: Bool
    /// Called once when a signed URL had expired, so the session can fetch fresh ones.
    var onExpired: (() async -> Void)? = nil
    var store: SlidePostStickerStore = .shared
    let onSelect: (String?) -> Void
    let onTransform: (String, String, (inout SlidePostSticker) -> Void) -> Void
    let onDelete: (String) -> Void

    private enum Mode {
        case idle
        /// A touch that started on empty canvas: not ours.
        case ignored
        case move(SlidePostSticker)
        case handle(SlidePostSticker, startVector: CGVector)
        case pinch(SlidePostSticker)
        var isPinch: Bool { if case .pinch = self { true } else { false } }
        var isIdle: Bool { if case .idle = self { true } else { false } }
    }

    @State private var loaded: [String: UIImage] = [:]
    @State private var mode = Mode.idle
    @State private var gestureKey = UUID().uuidString
    @State private var movedFar = false
    @State private var wasSelectedAtTouch = false
    @State private var twistLatched = false

    private static let handleRadius: CGFloat = 11
    private static let handleInset: CGFloat = 13
    private static let selectionPadding: CGFloat = 4
    private static let tapSlop: CGFloat = 6
    private static let twistThreshold = 8.0

    private var images: [String: UIImage] {
        var result = loaded
        for sticker in stickers where result[sticker.assetID] == nil {
            if let hit = store.cachedImage(assetID: sticker.assetID) { result[sticker.assetID] = hit }
        }
        return result
    }
    private var loadKey: String {
        stickers.map { sticker in
            "\(sticker.assetID)|\(assets.first { $0.id == sticker.assetID }.flatMap { $0.sourceURL ?? $0.displayURL } != nil)"
        }.joined(separator: ",")
    }

    var body: some View {
        let current = images
        ZStack(alignment: .topLeading) {
            SlidePostStickerLayerView(
                stickers: stickers, images: current, size: size,
                accessibility: .init(selectedID: interactive ? selectedID : nil, onSelect: { onSelect($0) }, onRemove: { onDelete($0) }))
                .allowsHitTesting(false)
            if interactive, !stickers.isEmpty {
                gestureSurface(current)
            }
            if interactive, let selected = stickers.first(where: { $0.id == selectedID }), let image = current[selected.assetID] {
                selectionOverlay(for: selected, placement: SlidePostStickerGeometry.placement(for: selected, imageSize: image.size, canvas: size))
            }
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
        .coordinateSpace(name: "slidepost-sticker-canvas")
        .task(id: loadKey) { await load() }
        .onDisappear { mode = .idle; twistLatched = false }
    }

    // MARK: Loading

    private func load() async {
        var expiredOnce = false
        for sticker in stickers {
            guard store.cachedImage(assetID: sticker.assetID) == nil, loaded[sticker.assetID] == nil,
                  let asset = assets.first(where: { $0.id == sticker.assetID }), let url = asset.sourceURL ?? asset.displayURL else { continue }
            do {
                loaded[sticker.assetID] = try await store.image(assetID: asset.id, url: url)
            } catch SlidePostImageCache.LoadError.expired where !expiredOnce {
                expiredOnce = true
                await onExpired?()
            } catch {}
        }
    }

    // MARK: Geometry

    private func placement(of sticker: SlidePostSticker, _ images: [String: UIImage]) -> SlidePostStickerGeometry.Placement? {
        images[sticker.assetID].map { SlidePostStickerGeometry.placement(for: sticker, imageSize: $0.size, canvas: size) }
    }
    private func hit(at point: CGPoint, _ images: [String: UIImage]) -> SlidePostSticker? {
        stickers.reversed().first { sticker in
            placement(of: sticker, images).map { SlidePostStickerGeometry.contains(point, in: $0, padding: Self.selectionPadding) } ?? false
        }
    }
    private func handleCenter(_ placement: SlidePostStickerGeometry.Placement) -> CGPoint {
        let corner = SlidePostStickerGeometry.handleCenter(of: placement, padding: Self.selectionPadding)
        return CGPoint(x: min(max(Self.handleInset, corner.x), size.width - Self.handleInset),
                       y: min(max(Self.handleInset, corner.y), size.height - Self.handleInset))
    }
    private func grabsHandle(at point: CGPoint, _ images: [String: UIImage]) -> SlidePostSticker? {
        guard let selected = stickers.first(where: { $0.id == selectedID }), let placed = placement(of: selected, images) else { return nil }
        let corner = handleCenter(placed)
        return hypot(point.x - corner.x, point.y - corner.y) <= NativeTextTransformMath.cornerGrabRadius ? selected : nil
    }

    // MARK: Gestures

    private func gestureSurface(_ images: [String: UIImage]) -> some View {
        var rects: [(CGRect, Double)] = stickers.compactMap { sticker in
            placement(of: sticker, images).map { ($0.rect.insetBy(dx: -Self.selectionPadding, dy: -Self.selectionPadding), $0.rotationDegrees) }
        }
        if let selected = stickers.first(where: { $0.id == selectedID }), let placed = placement(of: selected, images) {
            let corner = handleCenter(placed)
            rects.append((CGRect(x: corner.x - 22, y: corner.y - 22, width: 44, height: 44), 0))
        }
        return Color.clear
            .frame(width: size.width, height: size.height)
            // Only over the pictures (and the handle): the text under them, and the media's own controls, keep every other touch.
            .contentShape(SlidePostTextHitShape(rects: rects))
            .highPriorityGesture(touchGesture(images))
            .simultaneousGesture(pinchGesture)
    }

    private func touchGesture(_ images: [String: UIImage]) -> some Gesture {
        DragGesture(minimumDistance: 0, coordinateSpace: .named("slidepost-sticker-canvas"))
            .onChanged { value in
                if mode.isIdle { begin(at: value.startLocation, images) }
                dragChanged(value)
            }
            .onEnded { _ in
                // Touching an already-selected picture without moving it lets go of the selection.
                if case .move = mode, !movedFar, wasSelectedAtTouch { onSelect(nil) }
                if !mode.isPinch { mode = .idle }
            }
    }

    private func begin(at start: CGPoint, _ images: [String: UIImage]) {
        movedFar = false
        gestureKey = UUID().uuidString
        if let selected = grabsHandle(at: start, images), let placed = placement(of: selected, images) {
            wasSelectedAtTouch = true
            mode = .handle(selected, startVector: CGVector(dx: start.x - placed.center.x, dy: start.y - placed.center.y))
            return
        }
        guard let target = hit(at: start, images) else { mode = .ignored; return }
        wasSelectedAtTouch = target.id == selectedID
        if !wasSelectedAtTouch { onSelect(target.id) }
        mode = .move(target)
    }

    private func dragChanged(_ value: DragGesture.Value) {
        guard size.width > 0, size.height > 0 else { return }
        switch mode {
        case .idle, .ignored, .pinch:
            return
        case let .move(base):
            let distance = hypot(value.translation.width, value.translation.height)
            if distance > Self.tapSlop { movedFar = true }
            guard movedFar else { return }
            let x = Double(CGFloat(base.xFrac) + value.translation.width / size.width)
            let y = Double(CGFloat(base.yFrac) + value.translation.height / size.height)
            onTransform(base.id, gestureKey) { $0.xFrac = x; $0.yFrac = y }
        case let .handle(base, startVector):
            guard let centre = stickerCentre(of: base) else { return }
            let next = CGVector(dx: value.location.x - centre.x, dy: value.location.y - centre.y)
            guard let delta = NativeTextTransformMath.cornerDelta(start: startVector, next: next) else { return }
            movedFar = true
            onTransform(base.id, gestureKey) {
                $0.widthFrac = base.widthFrac * delta.scale
                $0.rotationDeg = SlidePostSticker.normalized(base.rotationDeg + delta.rotationDegrees)
            }
        }
    }

    private func stickerCentre(of sticker: SlidePostSticker) -> CGPoint? {
        CGPoint(x: CGFloat(sticker.xFrac) * size.width, y: CGFloat(sticker.yFrac) * size.height)
    }

    private var pinchGesture: some Gesture {
        MagnificationGesture().simultaneously(with: RotationGesture())
            .onChanged { value in
                guard let selected = stickers.first(where: { $0.id == selectedID }) else { return }
                let base: SlidePostSticker
                if case let .pinch(existing) = mode { base = existing } else {
                    gestureKey = UUID().uuidString
                    twistLatched = false
                    base = selected
                    mode = .pinch(base)
                }
                let scale = Double(value.first ?? 1), degrees = value.second?.degrees ?? 0
                if abs(degrees) >= Self.twistThreshold { twistLatched = true }
                let key = gestureKey, twist = twistLatched ? degrees : 0
                onTransform(base.id, key) {
                    $0.widthFrac = base.widthFrac * scale
                    $0.rotationDeg = SlidePostSticker.normalized(base.rotationDeg + twist)
                }
            }
            .onEnded { _ in mode = .idle; twistLatched = false }
    }

    // MARK: Selection chrome

    @ViewBuilder private func selectionOverlay(for sticker: SlidePostSticker, placement: SlidePostStickerGeometry.Placement) -> some View {
        let rect = placement.rect.insetBy(dx: -Self.selectionPadding, dy: -Self.selectionPadding)
        let handle = handleCenter(placement)
        let remove = removeCenter(of: placement)
        ZStack(alignment: .topLeading) {
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .strokeBorder(KriaColor.sky, lineWidth: 2)
                .frame(width: rect.width, height: rect.height)
                .rotationEffect(.degrees(placement.rotationDegrees))
                .position(x: rect.midX, y: rect.midY)
                .allowsHitTesting(false)
                .accessibilityElement(children: .ignore)
                .accessibilityIdentifier("slidepost-sticker-selection")
                .accessibilityValue("selected")
            Image(systemName: "arrow.up.left.and.arrow.down.right")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(.white)
                .frame(width: Self.handleRadius * 2, height: Self.handleRadius * 2)
                .background(KriaColor.sky, in: Circle())
                .position(x: handle.x, y: handle.y)
                .allowsHitTesting(false)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Resize and rotate")
                .accessibilityIdentifier("slidepost-sticker-handle")
            Button { onDelete(sticker.id) } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.white)
                    .frame(width: Self.handleRadius * 2, height: Self.handleRadius * 2)
                    .background(KriaColor.ink, in: Circle())
                    .frame(width: 44, height: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .position(x: remove.x, y: remove.y)
            .accessibilityLabel("Remove picture")
            .accessibilityIdentifier("slidepost-sticker-delete")
        }
        .frame(width: size.width, height: size.height, alignment: .topLeading)
    }

    /// The top-left corner of the rotated selection box, pulled inside the canvas so it is always reachable.
    private func removeCenter(of placement: SlidePostStickerGeometry.Placement) -> CGPoint {
        let radians = CGFloat(placement.rotationDegrees) * .pi / 180
        let dx = -(placement.size.width / 2 + Self.selectionPadding), dy = -(placement.size.height / 2 + Self.selectionPadding)
        let corner = CGPoint(x: placement.center.x + dx * cos(radians) - dy * sin(radians),
                             y: placement.center.y + dx * sin(radians) + dy * cos(radians))
        return CGPoint(x: min(max(Self.handleInset, corner.x), size.width - Self.handleInset),
                       y: min(max(Self.handleInset, corner.y), size.height - Self.handleInset))
    }
}
