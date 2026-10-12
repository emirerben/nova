import CoreGraphics
import Foundation

/// The post-wide size every slide exports at (KRI-606). Raw values are the server's ids
/// (`ASPECT_CANVASES` in `app/pipeline/slide_post/profiles.py`); the table is pinned against both by
/// the `sizes` block of `tests/fixtures/slide-framing-parity/v1.json`.
///
/// Instagram crops every later slide to the FIRST slide's shape, so a carousel has exactly one size.
/// TikTok shows each photo on its own and letterboxes anything that is not 9:16, but one size keeps
/// the preview, the export and a cross-post to Instagram identical.
enum SlidePostSize: String, CaseIterable, Sendable {
    case fullScreen = "9:16"
    case portrait = "4:5"
    case tall = "3:4"
    case square = "1:1"
    case wide = "1.91:1"

    /// The canvas (at scale 1) every slide of the post is drawn on. All dimensions are even.
    var canvas: CGSize {
        switch self {
        case .fullScreen: CGSize(width: 1080, height: 1920)
        case .portrait: CGSize(width: 1080, height: 1350)
        case .tall: CGSize(width: 1080, height: 1440)
        case .square: CGSize(width: 1080, height: 1080)
        case .wide: CGSize(width: 1080, height: 566)
        }
    }

    /// "4:5 Portrait", as shown in the transport row and the Size sheet.
    var label: String { "\(rawValue) \(name)" }
    var name: String {
        switch self {
        case .fullScreen: "Full screen"
        case .portrait: "Portrait"
        case .tall: "Tall"
        case .square: "Square"
        case .wide: "Wide"
        }
    }

    /// Displays natively on both apps (TikTok letterboxes it), so one export posts cleanly to either.
    var worksOnBoth: Bool { self == .portrait || self == .tall || self == .square }

    static func isInstagram(_ profile: String) -> Bool { profile == "instagram_carousel" }

    static func allowed(profile: String) -> [SlidePostSize] {
        isInstagram(profile) ? [.portrait, .tall, .square, .wide] : [.fullScreen, .tall, .portrait, .square]
    }

    static func defaultSize(profile: String) -> SlidePostSize { isInstagram(profile) ? .portrait : .fullScreen }

    /// The size a post actually uses: the stored `aspect`, or the profile's default when it is absent or
    /// not allowed on this platform (the server coerces the same way).
    static func resolved(_ aspect: String?, profile: String) -> SlidePostSize {
        if let aspect, let size = SlidePostSize(rawValue: aspect), allowed(profile: profile).contains(size) { return size }
        return defaultSize(profile: profile)
    }

    static func canvas(profile: String, aspect: String?) -> CGSize { resolved(aspect, profile: profile).canvas }

    /// The value to store in the draft: nil for the profile's own default (so a post that never chose a
    /// size serializes exactly as before sizes existed), nil for a size this platform cannot use.
    static func storedAspect(_ aspect: String?, profile: String) -> String? {
        let size = resolved(aspect, profile: profile)
        return size == defaultSize(profile: profile) ? nil : size.rawValue
    }
}
