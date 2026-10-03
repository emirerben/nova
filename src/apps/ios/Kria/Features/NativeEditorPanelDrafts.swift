import SwiftUI

/// Ephemeral authoring input shared while the connected editor swaps panels.
/// These values intentionally remain device-local and are never part of the edit recipe.
enum NativeSoundsTab: String { case music = "Music", effects = "Effects" }
/// Sound-effects panel screens besides "edit sound", which is derived from the selected SFX.
enum NativeSfxScreen: Equatable { case home, library, trim }

@MainActor
final class NativeEditorPanelDrafts: ObservableObject {
    @Published var musicTrackID = ""
    @Published var sfxQuery = ""
    @Published var soundsTab: NativeSoundsTab = .music
    @Published var sfxScreen: NativeSfxScreen = .home

    @Published var visualTab: NativeVisualPanel.Tab = .edit
    @Published var visualCategory: NativeVisualPanel.Category = .media
    @Published var cardPreset: String?
    @Published var cardText = ""
    @Published var motionPreset: String?
    @Published var motionAssetIDs: [String] = []
    @Published var cameraIntensity: Double?
    @Published var wholeVideoTransition = "cut"
    @Published var wholeVideoTransitionDuration = 0.2
}
