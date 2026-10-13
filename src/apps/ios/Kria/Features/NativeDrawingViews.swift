import SwiftUI
import KriaMediaEngine

// KRI-589 freehand drawing in the editor: the Draw-mode canvas and toolbar, the panel a selected drawing
// opens, and the move / scale / rotate layer on the preview. Painting goes through `NativeDrawingPainter`,
// the same code the compositor uses, so what is drawn is what is exported.

extension Color {
    /// "#RRGGBB" -> Color (the drawing wire's colour format).
    init(drawingHex hex: String) {
        let value = UInt32(hex.dropFirst(), radix: 16) ?? 0
        self.init(red: Double((value >> 16) & 255) / 255, green: Double((value >> 8) & 255) / 255, blue: Double(value & 255) / 255)
    }

    /// "#RRGGBB" for the system colour picker's result.
    var drawingHex: String {
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        UIColor(self).getRed(&red, green: &green, blue: &blue, alpha: &alpha)
        func byte(_ value: CGFloat) -> Int { Int((min(1, max(0, value)) * 255).rounded()) }
        return String(format: "#%02X%02X%02X", byte(red), byte(green), byte(blue))
    }
}

extension DrawingTexture {
    var label: String {
        switch self {
        case .pen: "Pen"
        case .marker: "Marker"
        case .highlighter: "Highlighter"
        case .neon: "Neon"
        case .chalk: "Chalk"
        case .spray: "Spray"
        }
    }
}

// MARK: - Draw mode canvas

/// Full-frame finger canvas over the preview. The live strokes are painted by the compositor's own painter.
struct NativeDrawingCanvas: View {
    @ObservedObject var sketch: NativeDrawingSketch
    @State private var dragging = false
    /// Resets to false when the system cancels a drag (alert, call), which never calls `onEnded`.
    @GestureState private var touching = false

    var body: some View {
        GeometryReader { proxy in
            Canvas { context, size in
                context.withCGContext { cg in
                    NativeDrawingPainter.paint(sketch.previewLayer, at: 0.5, canvas: size, in: cg)
                }
            }
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .local)
                    .onChanged { value in
                        let point = normalized(value.location, in: proxy.size)
                        if !dragging {
                            dragging = sketch.begin(at: normalized(value.startLocation, in: proxy.size))
                        }
                        if dragging { sketch.append(point) }
                    }
                    .onEnded { value in
                        if dragging { sketch.append(normalized(value.location, in: proxy.size)); sketch.end() }
                        dragging = false
                    }
                    .updating($touching) { _, state, _ in state = true }
            )
            .onChange(of: touching) { _, isTouching in
                // A cancelled drag: close the stroke so the next touch starts a fresh one.
                if !isTouching && dragging { sketch.end(); dragging = false }
            }
        }
        .accessibilityElement()
        .accessibilityLabel("Drawing canvas")
        .accessibilityHint("Draw with your finger")
        .accessibilityIdentifier("native-editor-draw-canvas")
    }

    private func normalized(_ point: CGPoint, in size: CGSize) -> CGPoint {
        CGPoint(x: min(1, max(0, point.x / max(size.width, 1))), y: min(1, max(0, point.y / max(size.height, 1))))
    }
}

// MARK: - Shared controls

struct NativeDrawingSwatchRow: View {
    let selected: String?
    let onPick: (String) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 2) {
                ForEach(NativeDrawingSketch.swatches, id: \.self) { hex in
                    Button { onPick(hex) } label: {
                        ZStack {
                            Circle().fill(Color(drawingHex: hex)).frame(width: 28, height: 28)
                            // A white swatch has no edge on white: the soft ring is a fill, not a stroke.
                            if hex == "#FFFFFF" { Circle().fill(KriaColor.fill).frame(width: 34, height: 34).zIndex(-1) }
                            if selected?.uppercased() == hex {
                                Circle().fill(hex == "#FFFFFF" || hex == "#FFD60A" ? KriaColor.ink : Color.white).frame(width: 9, height: 9)
                            }
                        }
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Colour \(hex)")
                    .accessibilityAddTraits(selected?.uppercased() == hex ? .isSelected : [])
                    .accessibilityIdentifier("native-editor-draw-swatch-\(hex.dropFirst())")
                }
                ColorPicker("Custom colour", selection: Binding(
                    get: { Color(drawingHex: selected ?? "#FF3B30") },
                    set: { onPick($0.drawingHex) }
                ), supportsOpacity: false)
                    .labelsHidden()
                    .frame(width: 44, height: 44)
                    .accessibilityIdentifier("native-editor-draw-color-picker")
            }
        }
    }
}

// MARK: - Draw mode toolbar

struct NativeDrawingToolbar: View {
    @ObservedObject var sketch: NativeDrawingSketch
    let onCancel: () -> Void
    let onDone: () -> Void

    var body: some View {
        VStack(spacing: 10) {
            if let note = sketch.note {
                Text(note)
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("native-editor-draw-note")
            }
            NativeDrawingSwatchRow(selected: sketch.colorHex) { sketch.colorHex = $0 }
            HStack(spacing: 10) {
                Image(systemName: "circle.fill").font(.system(size: 6)).foregroundStyle(KriaColor.zinc).accessibilityHidden(true)
                Slider(value: $sketch.widthFrac, in: NativeDrawingSketch.widthRange)
                    .accessibilityLabel("Brush size")
                    .accessibilityIdentifier("native-editor-draw-size")
                Image(systemName: "circle.fill").font(.system(size: 18)).foregroundStyle(KriaColor.zinc).accessibilityHidden(true)
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(DrawingTexture.allCases, id: \.self) { texture in
                        Button { sketch.select(texture: texture) } label: {
                            Text(texture.label)
                                .font(KriaFont.body(13).weight(sketch.texture == texture ? .semibold : .medium))
                                .foregroundStyle(KriaColor.ink)
                                .padding(.horizontal, 14)
                                .frame(minHeight: 44)
                                .background(sketch.texture == texture ? KriaColor.butter : KriaColor.fill, in: Capsule())
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(sketch.texture == texture ? .isSelected : [])
                        .accessibilityIdentifier("native-editor-draw-texture-\(texture.rawValue)")
                    }
                }
            }
            HStack(spacing: 4) {
                iconButton("arrow.uturn.backward", label: "Undo stroke", id: "native-editor-draw-undo", enabled: !sketch.strokes.isEmpty) { sketch.undoStroke() }
                iconButton("trash", label: "Clear drawing", id: "native-editor-draw-clear", enabled: sketch.hasInk) { sketch.clear() }
                Spacer()
                Button("Cancel", action: onCancel)
                    .font(KriaFont.body(15).weight(.medium))
                    .foregroundStyle(KriaColor.ink)
                    .frame(minWidth: 72, minHeight: 44)
                    .accessibilityIdentifier("native-editor-draw-cancel")
                Button("Done", action: onDone)
                    .buttonStyle(KriaPrimaryButtonStyle(minHeight: 44))
                    .disabled(!sketch.hasInk)
                    .accessibilityIdentifier("native-editor-draw-done")
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(KriaColor.paper)
        .tint(KriaColor.ink)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-draw-toolbar")
    }

    private func iconButton(_ symbol: String, label: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 17, weight: .medium)).frame(width: 44, height: 44)
        }
        .buttonStyle(.plain)
        .foregroundStyle(enabled ? KriaColor.ink : KriaColor.line)
        .disabled(!enabled)
        .accessibilityLabel(label)
        .accessibilityIdentifier(id)
    }
}

// MARK: - Selected drawing panel

struct NativeDrawingPanel: View {
    @ObservedObject var session: NativeEditorSession
    let id: String
    let onDrawMore: () -> Void
    let onDone: () -> Void
    enum Tab: String, CaseIterable { case edit = "Edit", animation = "Animation" }
    @State private var tab = Tab.edit

    private var drawing: EditorDrawing? { session.drawing(id: id) }

    var body: some View {
        NativeEditorLanePanel(
            title: "Drawing", tabs: Tab.allCases, tab: $tab, onDone: onDone,
            onDelete: session.canDeleteDrawings ? { _ = session.deleteSelection(EditorSelection(kind: .drawing, id: id)) } : nil,
            removeLabel: "Delete drawing", removeID: "native-editor-remove-drawing"
        ) {
            if let drawing {
                VStack(alignment: .leading, spacing: 14) {
                    switch tab {
                    case .edit: edit(drawing)
                    case .animation: NativeDrawingAnimationControls(session: session, id: id, drawing: drawing)
                    }
                    if !session.canEditDrawings {
                        Text("This drawing can’t be edited right now.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                }
                .disabled(!session.canEditDrawings)
            }
        }
        .onChange(of: drawing == nil) { _, gone in if gone { onDone() } }
    }

    @ViewBuilder private func edit(_ drawing: EditorDrawing) -> some View {
        let reason = session.drawingCapMessage(extraPoints: 2, extraStrokes: 1, editing: id)
        Button(action: onDrawMore) {
            Label("Draw more", systemImage: "pencil.tip")
                .font(KriaFont.body(15).weight(.semibold))
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(KriaColor.butter, in: Capsule())
        }
        .buttonStyle(.plain)
        .foregroundStyle(KriaColor.ink)
        .disabled(reason != nil)
        .opacity(reason == nil ? 1 : 0.45)
        .accessibilityIdentifier("native-editor-drawing-draw-more")
        if let reason { Text(reason).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk) }
        VStack(alignment: .leading, spacing: 4) {
            Text("Recolour all").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            NativeDrawingSwatchRow(selected: commonColor(drawing)) { session.recolorDrawing(id: id, hex: $0) }
        }
    }

    /// The colour every stroke shares, if they share one.
    private func commonColor(_ drawing: EditorDrawing) -> String? {
        let colors = Set(drawing.strokes.map { $0.color.uppercased() })
        return colors.count == 1 ? colors.first : nil
    }
}

// MARK: - Animation tab: speed + previews (KRI-637, device-test feedback)

/// The Animation tab's speed control. The slider position is the natural log of a speed multiplier, so sliding RIGHT is
/// FASTER (a shorter draw time) and 1x is the default 1.2 s. The stored value stays `draw_duration_s`: no wire change.
enum NativeDrawingSpeed {
    static let baseSeconds = 1.2
    static let fastestMultiplier = 6.0

    /// Slider bounds for a drawing whose window lasts `window` seconds (the draw cannot outlast its window).
    static func bounds(window: Double) -> ClosedRange<Double> {
        let longest = max(0.3, min(DrawingLimits.maxDrawDuration, window))
        return log(baseSeconds / longest)...log(fastestMultiplier)
    }
    static func position(seconds: Double, window: Double) -> Double {
        let range = bounds(window: window)
        guard seconds.isFinite, seconds > 0 else { return 0 }
        return min(range.upperBound, max(range.lowerBound, log(baseSeconds / seconds)))
    }
    static func seconds(position: Double) -> Double { baseSeconds / exp(position) }
    static func multiplier(position: Double) -> Double { exp(position) }

    /// The system slider only buzzes at its two ends, so the thumb crosses a notch every `notchSize` (a ~20% change of
    /// speed) and each crossing ticks. Notch 0 is centred on 1x: the default gets a firmer tap.
    static let notchSize = 0.2
    struct Tick: Equatable {
        var notch: Int
        var isDefault: Bool { notch == 0 }
    }
    static func tick(position: Double) -> Tick { Tick(notch: Int((position / notchSize).rounded())) }
}

/// What one preview tile shows at a moment of the shared preview clock: how much of the sample stroke is drawn and
/// how opaque it is. Pure, so the motion each tile promises is testable.
enum NativeDrawingAnimationPreviewMath {
    struct Sample: Equatable { var progress: Double; var alpha: Double }
    static let stable = Sample(progress: 1, alpha: 1)
    static let hold = 0.9, resetGap = 0.5, fadeSeconds = 0.4

    /// The Draw tile plays the drawing's real draw time (kept watchable), so sliding the speed changes the tile.
    static func edgeSeconds(_ entrance: DrawingEntrance, drawSeconds: Double) -> Double {
        entrance == .draw ? min(2.5, max(0.3, drawSeconds)) : fadeSeconds
    }

    static func entrance(_ entrance: DrawingEntrance, drawSeconds: Double, clock: Double) -> Sample {
        guard entrance != .none else { return stable }
        let edge = edgeSeconds(entrance, drawSeconds: drawSeconds)
        let t = clock.truncatingRemainder(dividingBy: edge + hold + resetGap)
        let into = t < edge ? t / edge : (t < edge + hold ? 1 : 0)
        return entrance == .draw ? Sample(progress: into, alpha: 1) : Sample(progress: 1, alpha: into)
    }

    static func exit(_ exit: DrawingExit, clock: Double) -> Sample {
        guard exit != .none else { return stable }
        let t = clock.truncatingRemainder(dividingBy: hold + fadeSeconds + resetGap)
        let alpha = t < hold ? 1 : (t < hold + fadeSeconds ? 1 - (t - hold) / fadeSeconds : 0)
        return Sample(progress: 1, alpha: alpha)
    }
}

/// The Animation tab. Entrance and Exit are tiles that PLAY what they do (like the text animation tiles), the speed
/// slider is continuous (no tick dots) and right is faster, and the layout never changes with the selection: the speed
/// row stays (dimmed unless the entrance is Draw) and the content is pinned to the leading edge, so nothing re-centres.
struct NativeDrawingAnimationControls: View {
    @ObservedObject var session: NativeEditorSession
    let id: String
    let drawing: EditorDrawing
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// A reference held by `@State`: the tiles observe it, this view does not re-render every frame.
    @State private var clock = NativeTextAnimationPreviewClock()
    @State private var speedPosition = 0.0
    /// True only while a finger is on the slider: opening the tab or an undo moves the thumb silently.
    @State private var dragging = false
    @State private var tick = NativeDrawingSpeed.Tick(notch: 0)

    private var window: Double { drawing.endS - drawing.startS }
    private var drawsOn: Bool { drawing.entrance == .draw }
    private var grid: [GridItem] { [GridItem(.adaptive(minimum: 92), spacing: 8, alignment: .top)] }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                heading("Entrance")
                LazyVGrid(columns: grid, alignment: .leading, spacing: 10) {
                    entranceTile("Draw", .draw)
                    entranceTile("Fade", .fade)
                    entranceTile("None", .none)
                }
            }
            speedSection
            VStack(alignment: .leading, spacing: 6) {
                heading("Exit")
                LazyVGrid(columns: grid, alignment: .leading, spacing: 10) {
                    exitTile("None", .none)
                    exitTile("Fade", .fade)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onAppear {
            speedPosition = NativeDrawingSpeed.position(seconds: drawing.drawDurationS, window: window)
            startPreviewIfAllowed()
        }
        .onDisappear { clock.pauseForLifecycle() }
        .onChange(of: scenePhase) { _, next in if next == .active { startPreviewIfAllowed() } else { clock.pauseForLifecycle() } }
        .onChange(of: reduceMotion) { _, reduced in if reduced { clock.pauseForLifecycle() } else { startPreviewIfAllowed() } }
        // An undo or a redo changes the stored time under us: follow it.
        .onChange(of: drawing.drawDurationS) { _, seconds in
            let next = NativeDrawingSpeed.position(seconds: seconds, window: window)
            if abs(next - speedPosition) > 0.01 { speedPosition = next }
        }
    }

    private var speedSection: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                heading("Draw speed")
                Spacer()
                // One line for every entrance, so the section never changes height when the entrance does.
                Text(drawsOn ? String(format: "%.1f×", NativeDrawingSpeed.multiplier(position: speedPosition)) : "Draw only")
                    .font(KriaFont.body(13)).foregroundStyle(drawsOn ? KriaColor.zinc : KriaColor.mutedInk).monospacedDigit()
            }
            // Continuous on purpose: a `step` makes iOS draw a tick dot per step.
            NativeEditorSlider(session: session, value: $speedPosition, in: NativeDrawingSpeed.bounds(window: window),
                               onEditingChanged: { dragging = $0 }) {
                Text("Draw speed")
            }
            .onChange(of: speedPosition) { _, position in
                let seconds = NativeDrawingSpeed.seconds(position: position)
                if abs(seconds - drawing.drawDurationS) > 0.02 { session.setDrawingDrawDuration(id: id, seconds: seconds) }
                if dragging { tick = NativeDrawingSpeed.tick(position: position) }
            }
            // A tick per notch under the finger (the system slider gives one only at the ends), firmer on 1x.
            .sensoryFeedback(trigger: tick) { old, new in
                guard old != new else { return nil }
                return new.isDefault ? .impact(weight: .medium) : .selection
            }
            .accessibilityValue(String(format: "%.1f times", NativeDrawingSpeed.multiplier(position: speedPosition)))
            .accessibilityIdentifier("native-editor-drawing-draw-speed")
            HStack {
                Text("Slower"); Spacer(); Text("Faster")
            }
            .font(KriaFont.body(11)).foregroundStyle(KriaColor.mutedInk)
        }
        .opacity(drawsOn ? 1 : 0.4)
        .disabled(!drawsOn)
    }

    private func heading(_ title: String) -> some View {
        Text(title).font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
    }

    private func entranceTile(_ title: String, _ value: DrawingEntrance) -> some View {
        NativeDrawingAnimationTile(title: title, selected: drawing.entrance == value, clock: clock,
                                   identifier: "native-editor-drawing-entrance-\(title.lowercased())") { time in
            NativeDrawingAnimationPreviewMath.entrance(value, drawSeconds: NativeDrawingSpeed.seconds(position: speedPosition), clock: time)
        } pick: { session.setDrawingEntrance(id: id, entrance: value) }
    }

    private func exitTile(_ title: String, _ value: DrawingExit) -> some View {
        NativeDrawingAnimationTile(title: title, selected: drawing.exit == value, clock: clock,
                                   identifier: "native-editor-drawing-exit-\(title.lowercased())") { time in
            NativeDrawingAnimationPreviewMath.exit(value, clock: time)
        } pick: { session.setDrawingExit(id: id, exit: value) }
    }

    private func startPreviewIfAllowed() {
        guard NativeTextAnimationPreviewAutoplay.allows(reduceMotion: reduceMotion, environment: ProcessInfo.processInfo.environment) else { return }
        clock.startAutoplay()
    }
}

/// One choice with a live sample of its animation. Selection is a fill (no border), like the rest of Kria's controls.
private struct NativeDrawingAnimationTile: View {
    let title: String
    let selected: Bool
    @ObservedObject var clock: NativeTextAnimationPreviewClock
    let identifier: String
    let sample: (Double) -> NativeDrawingAnimationPreviewMath.Sample
    let pick: () -> Void

    init(title: String, selected: Bool, clock: NativeTextAnimationPreviewClock, identifier: String,
         sample: @escaping (Double) -> NativeDrawingAnimationPreviewMath.Sample, pick: @escaping () -> Void) {
        self.title = title; self.selected = selected; self.clock = clock; self.identifier = identifier
        self.sample = sample; self.pick = pick
    }

    var body: some View {
        // Until the clock has run (and with Reduce Motion) the tile shows the finished state, like the text tiles.
        let shown = clock.hasEverPlayed ? sample(clock.elapsed) : NativeDrawingAnimationPreviewMath.stable
        Button(action: pick) {
            VStack(spacing: 6) {
                NativeDrawingPreviewSquiggle().trim(from: 0, to: shown.progress)
                    .stroke(KriaColor.ink, style: StrokeStyle(lineWidth: 3.5, lineCap: .round, lineJoin: .round))
                    .opacity(shown.alpha)
                    .padding(.horizontal, 14).padding(.vertical, 12)
                    .frame(maxWidth: .infinity, minHeight: 52)
                    .background(selected ? KriaColor.butter : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                    .accessibilityHidden(true)
                Text(title).font(KriaFont.body(11).weight(selected ? .semibold : .medium)).lineLimit(1).minimumScaleFactor(0.8)
                    .foregroundStyle(KriaColor.ink)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier(identifier)
    }
}

/// The little stroke every tile animates: a loose wave, drawn left to right.
private struct NativeDrawingPreviewSquiggle: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let steps = 40
        for step in 0...steps {
            let t = Double(step) / Double(steps)
            let point = CGPoint(x: rect.minX + rect.width * t, y: rect.midY + sin(t * .pi * 3) * rect.height * 0.32)
            if step == 0 { path.move(to: point) } else { path.addLine(to: point) }
        }
        return path
    }
}

// MARK: - Move / scale / rotate on the preview

/// Frame, move, pinch and two-finger rotate for the selected drawing. The preview shows the gesture live
/// through the compositor (only the portable layers are swapped); the document is written once, when the
/// fingers lift, so one gesture is one undo step.
struct NativeDrawingTransformLayer: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject private var clock: NativeEditorPlaybackClock
    let id: String
    @State private var base: EditorDrawing?
    @State private var live: EditorDrawing?
    @State private var activeGestures = Set<String>()

    init(session: NativeEditorSession, id: String) {
        self.session = session
        self.id = id
        _clock = ObservedObject(wrappedValue: session.playbackClock)
    }

    var body: some View {
        GeometryReader { proxy in
            if let drawing = live ?? session.drawing(id: id), clock.currentTime >= drawing.startS - 0.001, clock.currentTime < drawing.endS {
                let box = frame(of: drawing, in: proxy.size)
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(KriaColor.sky.opacity(0.18))
                    .overlay(alignment: .topLeading) { knob }
                    .overlay(alignment: .topTrailing) { knob }
                    .overlay(alignment: .bottomLeading) { knob }
                    .overlay(alignment: .bottomTrailing) { knob }
                    .frame(width: box.width, height: box.height)
                    .rotationEffect(.degrees(drawing.rotationDeg))
                    .position(x: drawing.centerX * proxy.size.width, y: drawing.centerY * proxy.size.height)
                    .contentShape(Rectangle())
                    .gesture(moveGesture(proxy.size))
                    .simultaneousGesture(scaleGesture)
                    .simultaneousGesture(rotateGesture)
                    .accessibilityElement()
                    .accessibilityLabel("Selected drawing")
                    .accessibilityHint("Drag to move, pinch to resize, twist to rotate")
                    .accessibilityIdentifier("native-editor-drawing-transform")
            }
        }
    }

    private var knob: some View {
        Circle().fill(KriaColor.sky).frame(width: 12, height: 12).padding(-6).allowsHitTesting(false)
    }

    /// The unrotated frame, padded so a thin stroke still has something to grab.
    private func frame(of drawing: EditorDrawing, in size: CGSize) -> CGSize {
        let points = drawing.strokes.flatMap(\.points)
        let xs = points.map { $0[0] }, ys = points.map { $0[1] }
        let width = ((xs.max() ?? 0.5) - (xs.min() ?? 0.5)) * drawing.scale * size.width
        let height = ((ys.max() ?? 0.5) - (ys.min() ?? 0.5)) * drawing.scale * size.height
        let widest = (drawing.strokes.map(\.widthFrac).max() ?? 0.01) * min(size.width, size.height) * drawing.scale
        return CGSize(width: max(44, width + widest + 16), height: max(44, height + widest + 16))
    }

    private func moveGesture(_ size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 3, coordinateSpace: .local)
            .onChanged { value in
                guard begin("move"), let base else { return }
                // Clamped to the frame: a centre outside 0...1 is an invalid drawing the preview compile would drop.
                update(base) {
                    $0.centerX = min(1, max(0, base.centerX + value.translation.width / max(size.width, 1)))
                    $0.centerY = min(1, max(0, base.centerY + value.translation.height / max(size.height, 1)))
                }
            }
            .onEnded { _ in end("move") }
    }

    private var scaleGesture: some Gesture {
        MagnifyGesture()
            .onChanged { value in
                guard begin("scale"), let base else { return }
                update(base) { $0.scale = min(3, max(0.2, base.scale * Double(value.magnification))) }
            }
            .onEnded { _ in end("scale") }
    }

    private var rotateGesture: some Gesture {
        RotateGesture()
            .onChanged { value in
                guard begin("rotate"), let base else { return }
                update(base) { $0.rotationDeg = EditorDrawing.wrappedRotation(base.rotationDeg + value.rotation.degrees) }
            }
            .onEnded { _ in end("rotate") }
    }

    /// Records the gesture's starting drawing once; returns false when the drawing can't be edited.
    private func begin(_ name: String) -> Bool {
        guard session.canEditDrawings else { return false }
        if base == nil, let current = session.drawing(id: id) {
            base = current
            session.beginDirectManipulation()
        }
        activeGestures.insert(name)
        return base != nil
    }

    private func update(_ start: EditorDrawing, _ change: (inout EditorDrawing) -> Void) {
        var next = live ?? start
        change(&next)
        live = next
        session.previewDrawingInPlace(next)
    }

    private func end(_ name: String) {
        activeGestures.remove(name)
        guard activeGestures.isEmpty, base != nil else { return }
        if let live {
            session.setDrawingTransform(id: id, centerX: live.centerX, centerY: live.centerY, scale: live.scale, rotationDeg: live.rotationDeg)
        }
        base = nil; live = nil
        session.endDirectManipulation()
    }
}
