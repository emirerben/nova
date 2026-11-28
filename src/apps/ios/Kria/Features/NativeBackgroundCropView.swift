import KriaMediaEngine
import SwiftUI
import UIKit

/// KRI-1656: Crop for one Talk-to-camera background bar. It replaces Aim (Left / Centre / Right and a Zoom slider): the creator
/// drags and pinches a frame over the real picture, and what is inside it is what shows. The crop is written as the wire's
/// existing `focus {x, y, zoom}` (`BackgroundCropMapping`), so no new field and no new protocol are involved.
///
/// Opening this never writes: the crop is seeded from what is stored and committed only when a gesture ends (one undo step).
struct NativeBackgroundCropView: View {
    @ObservedObject var session: NativeEditorSession
    let barID: String
    @State private var image: UIImage?
    @State private var failed = false
    @State private var crop: NormalizedSourceRect?

    private var storedFocus: SpeakerBackgroundFocus? { session.speakerBackgroundFocus(barID: barID) }
    private var outputAspect: Double { Double(session.cropOutputAspect) }
    private var imageAspect: Double? {
        guard let image, image.size.height > 0 else { return nil }
        return Double(image.size.width / image.size.height)
    }
    /// A stored zoom below 1x (chat can set it) shows the whole picture inside the frame with bands: no crop can say so.
    private var storedIsZoomedOut: Bool { (storedFocus?.zoom ?? 1) < 1 - 1e-9 }

    var body: some View {
        Group {
            if let image, let imageAspect {
                KriaCropEditor(
                    image: image, outputAspect: CGFloat(outputAspect),
                    crop: Binding(get: { crop }, set: { write($0, imageAspect: imageAspect) }),
                    onEditingChanged: { editing in editing ? session.beginTransaction() : session.endTransaction() },
                    options: KriaCropOptions(
                        lockedToFill: true,
                        minimumWidth: BackgroundCropMapping.minimumWidth(imageAspect: imageAspect, outputAspect: outputAspect, storedZoom: storedFocus?.zoom),
                        footnote: storedIsZoomedOut
                            ? String(localized: "This background is zoomed out to show the whole picture. Drag or pinch to fill the frame.")
                            : String(localized: "Drag to move, pinch to zoom. What you see is what shows.")))
                .onAppear { seed(imageAspect: imageAspect) }
                .onChange(of: storedFocus) { _, _ in seed(imageAspect: imageAspect) }
                .allowsHitTesting(session.canEditSpeakerBackgroundFocus)
                .opacity(session.canEditSpeakerBackgroundFocus ? 1 : 0.6)
                .overlay(alignment: .bottom) {
                    if !session.canEditSpeakerBackgroundFocus {
                        Label("Crop is locked for this render.", systemImage: "lock")
                            .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                            .padding(.bottom, 4)
                            .accessibilityIdentifier("native-editor-background-crop-locked")
                    }
                }
            } else if failed {
                VStack(spacing: 8) {
                    Image(systemName: "photo").font(.system(size: 28)).foregroundStyle(KriaColor.zinc)
                    Text("Couldn’t load this picture to crop.").font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityIdentifier("native-editor-background-crop-unavailable")
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .padding(16)
        .background(KriaColor.paper)
        .accessibilityIdentifier("native-editor-background-crop")
        .task(id: barID) {
            guard image == nil else { return }
            failed = false
            if let loaded = await session.cropSourceFrame(forBackgroundBar: barID) { image = loaded } else { failed = true }
        }
    }

    /// What the editor should show for what is stored: the crop it describes, the plain fill for a zoomed-out one (so Reset is
    /// offered), nothing for no crop.
    private func seed(imageAspect: Double) {
        let next: NormalizedSourceRect?
        if storedIsZoomedOut {
            next = KriaCropGeometry.fillCrop(imageAspect: imageAspect, outputAspect: outputAspect)
        } else {
            next = BackgroundCropMapping.crop(from: storedFocus, imageAspect: imageAspect, outputAspect: outputAspect)
        }
        if crop != next { crop = next }
    }

    private func write(_ value: NormalizedSourceRect?, imageAspect: Double) {
        crop = value
        let focus = value.flatMap { BackgroundCropMapping.focus(from: $0, current: storedFocus, imageAspect: imageAspect, outputAspect: outputAspect) }
        _ = session.setSpeakerBackgroundFocus(focus, barID: barID)
    }
}

/// KRI-1656: the pool photos and videos a background bar can be swapped for (no "None": removing is its own action).
struct NativeBackgroundReplaceGrid: View {
    @ObservedObject var session: NativeEditorSession
    let barID: String
    @Environment(\.dismiss) private var dismiss
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 10, alignment: .top), count: 3)

    var body: some View {
        let current = session.backgroundBarMedia(id: barID)?.mediaID
        ScrollView {
            LazyVGrid(columns: columns, spacing: 10) {
                ForEach(session.speakerBackgroundChoices) { asset in
                    let kind = VisualMediaKind(rawValue: asset.kind) ?? .image
                    let selected = asset.id == current
                    Button {
                        session.endTransaction()
                        if selected || session.replaceSpeakerBackground(barID: barID, mediaID: asset.id, kind: kind) { dismiss() }
                    } label: {
                        NativeVisualThumbnail(asset: asset)
                            .aspectRatio(1, contentMode: .fit)
                            .frame(maxWidth: .infinity)
                            .background(KriaColor.softZinc)
                            .clipShape(RoundedRectangle(cornerRadius: 12))
                            .overlay(alignment: .topTrailing) {
                                if selected {
                                    Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(.white)
                                        .frame(width: 22, height: 22).background(KriaColor.ink, in: Circle()).padding(6)
                                }
                            }
                            .contentShape(RoundedRectangle(cornerRadius: 12))
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(asset.sourceFilename ?? (kind == .video ? String(localized: "Video") : String(localized: "Photo")))
                    .accessibilityAddTraits(selected ? .isSelected : [])
                    .accessibilityIdentifier("native-editor-background-replace-" + asset.id)
                }
            }
            .padding(16)
        }
        .background(KriaColor.paper)
        .accessibilityIdentifier("native-editor-background-replace")
    }
}
