import SwiftUI

/// Ephemeral authoring input shared while the connected editor swaps panels.
/// These values intentionally remain device-local and are never part of the edit recipe.
@MainActor
final class NativeEditorPanelDrafts: ObservableObject {
    @Published var musicTrackID = ""

    @Published var visualTab: NativeVisualPanel.Tab = .edit
    @Published var visualCategory: NativeVisualPanel.Category = .media
    @Published var cardPreset: String?
    @Published var cardText = ""
    @Published var motionPreset: String?
    @Published var motionAssetIDs: [String] = []
    @Published var cameraIntensity: Double?
}
