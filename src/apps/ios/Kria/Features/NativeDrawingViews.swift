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
    @State private var drawSeconds = 1.2

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
                    case .animation: animation(drawing)
                    }
                    if !session.canEditDrawings {
                        Text("This drawing can’t be edited right now.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                }
                .disabled(!session.canEditDrawings)
            }
        }
        .onChange(of: drawing == nil) { _, gone in if gone { onDone() } }
        .onAppear { drawSeconds = drawing?.drawDurationS ?? 1.2 }
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

    @ViewBuilder private func animation(_ drawing: EditorDrawing) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Entrance").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            chips([("Draw", DrawingEntrance.draw), ("Fade", .fade), ("None", .none)], selected: drawing.entrance, prefix: "entrance") {
                session.setDrawingEntrance(id: id, entrance: $0)
            }
        }
        if drawing.entrance == .draw {
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text("Draw speed").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
                    Spacer()
                    Text(String(format: "%.1f s", drawSeconds)).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc).monospacedDigit()
                }
                NativeEditorSlider(session: session, value: $drawSeconds, in: 0.2...max(0.3, min(DrawingLimits.maxDrawDuration, drawing.endS - drawing.startS)), step: 0.05) {
                    Text("Draw speed")
                }
                .onChange(of: drawSeconds) { _, value in session.setDrawingDrawDuration(id: id, seconds: value) }
                .accessibilityIdentifier("native-editor-drawing-draw-speed")
                Text("A shorter time draws faster.").font(KriaFont.body(11)).foregroundStyle(KriaColor.mutedInk)
            }
        }
        VStack(alignment: .leading, spacing: 6) {
            Text("Exit").font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            chips([("None", DrawingExit.none), ("Fade", .fade)], selected: drawing.exit, prefix: "exit") {
                session.setDrawingExit(id: id, exit: $0)
            }
        }
    }

    private func chips<Value: Equatable>(_ options: [(String, Value)], selected: Value, prefix: String, pick: @escaping (Value) -> Void) -> some View {
        HStack(spacing: 6) {
            ForEach(options.indices, id: \.self) { index in
                let option = options[index]
                Button { pick(option.1) } label: {
                    Text(option.0)
                        .font(KriaFont.body(13).weight(selected == option.1 ? .semibold : .medium))
                        .foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 16)
                        .frame(minHeight: 44)
                        .background(selected == option.1 ? KriaColor.butter : KriaColor.fill, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selected == option.1 ? .isSelected : [])
                .accessibilityIdentifier("native-editor-drawing-\(prefix)-\(option.0.lowercased())")
            }
        }
    }

    /// The colour every stroke shares, if they share one.
    private func commonColor(_ drawing: EditorDrawing) -> String? {
        let colors = Set(drawing.strokes.map { $0.color.uppercased() })
        return colors.count == 1 ? colors.first : nil
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
