import SwiftUI

/// The look shared by every selectable media grid (the Photos gallery and the saved-uploads grid): pick-order
/// number or empty ring top-trailing, video length bottom-trailing, and a heavy ink frame once chosen.
struct GalleryTileChrome: ViewModifier {
    /// 1-based pick number; nil when not chosen.
    let number: Int?
    /// Seconds, shown as a length badge for videos.
    let duration: TimeInterval?
    /// A tile that is already part of the destination: a checkmark instead of a number, never framed as a pick.
    var isLocked = false

    func body(content: Content) -> some View {
        content
            .overlay(alignment: .bottomTrailing) {
                if let duration {
                    Text(Self.duration(duration))
                        .font(KriaFont.body(11).weight(.semibold)).foregroundStyle(Color.white)
                        .padding(.horizontal, 5).padding(.vertical, 2)
                        .background(KriaColor.ink.opacity(0.7), in: Capsule())
                        .padding(4)
                }
            }
            .overlay(alignment: .topTrailing) {
                Group {
                    if isLocked {
                        Image(systemName: "checkmark")
                            .font(.system(size: 11, weight: .bold)).foregroundStyle(Color.white)
                            .frame(width: 22, height: 22)
                            .background(KriaColor.mutedInk, in: Circle())
                    } else if let number {
                        Text("\(number)")
                            .font(KriaFont.body(12).weight(.bold)).foregroundStyle(Color.white)
                            .frame(minWidth: 22, minHeight: 22).padding(.horizontal, 2)
                            .background(KriaColor.ink, in: Capsule())
                    } else {
                        Circle().stroke(Color.white.opacity(0.9), lineWidth: 1.5).frame(width: 22, height: 22)
                            .background(Circle().fill(Color.black.opacity(0.15)))
                    }
                }
                .padding(5)
            }
            .overlay { if number != nil, !isLocked { Rectangle().stroke(KriaColor.ink, lineWidth: 3) } }
            .contentShape(Rectangle())
    }

    static func duration(_ seconds: TimeInterval) -> String {
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

/// The count / hint bar under a selectable grid.
struct GalleryFooter: View {
    let text: String
    let identifier: String
    var body: some View {
        HStack {
            Text(text)
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .accessibilityIdentifier(identifier)
            Spacer()
        }
        .padding(.horizontal, 16).frame(minHeight: 44)
        .background(.regularMaterial)
    }
}
