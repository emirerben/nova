import SwiftUI

/// The post-wide Size sheet (KRI-606): where the post goes, what shape every slide is, and whether photos fill the
/// frame or sit whole with bars. Every change is one undoable edit on the session's draft; nothing here saves.
///
/// Instagram crops every later slide to the first slide's shape, so there is exactly one size per post. 3:4, 4:5 and
/// 1:1 display natively on both apps (TikTok shows them with bars), so those carry a "Both apps" tag.
struct SlidePostSizeSheet: View {
    @ObservedObject var session: SlidePostSession
    let onDone: () -> Void

    /// The bars a "Fit" tap uses; follows what the photos already have when they agree.
    @State private var bars: SlidePostFit = .blur
    @State private var notice: String?

    private var draft: SlidePostDraft? { session.draft }
    private var profile: String { draft?.platformProfile ?? "tiktok_photo" }
    private var hasVideo: Bool { draft?.slides.contains { $0.kind == "video" } == true }
    private var hasPhotos: Bool { draft?.slides.contains { $0.kind == "image" } == true }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header
                platformSection
                sizeSection
                if hasPhotos { photosSection }
                if let notice {
                    Text(notice).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                        .accessibilityIdentifier("slidepost-size-notice")
                }
            }
            .padding(.horizontal, 20).padding(.top, 20).padding(.bottom, 28)
        }
        .background(KriaColor.paper)
        .accessibilityIdentifier("slidepost-size-sheet")
        .onAppear { if case .uniform(let fit) = session.fitSummary, let fit { bars = fit } }
    }

    private var header: some View {
        HStack {
            Text("Size").font(KriaFont.body(20).weight(.semibold))
            Spacer()
            Button(action: onDone) {
                Text("Done").font(KriaFont.body(14)).frame(minWidth: 64, minHeight: 44)
                    .background(KriaColor.ink.opacity(0.06), in: Capsule())
            }
            .accessibilityIdentifier("slidepost-size-done")
        }
    }

    // MARK: Platform

    private var platformSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Post to")
            HStack(spacing: 8) {
                platformButton("TikTok", profile: "tiktok_photo", enabled: !hasVideo, id: "slidepost-size-platform-tiktok")
                platformButton("Instagram", profile: "instagram_carousel", enabled: true, id: "slidepost-size-platform-instagram")
            }
            if hasVideo {
                Text("TikTok photo posts can't include videos.").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            }
        }
    }

    private func platformButton(_ title: String, profile value: String, enabled: Bool, id: String) -> some View {
        let on = profile == value
        return Button {
            guard !on else { return }
            let reset = session.setPlatform(value)
            notice = reset > 0 ? "Size changed to fit \(title). Crops were reset on \(reset) slide\(reset == 1 ? "" : "s")." : nil
        } label: {
            Text(title).font(KriaFont.body(14).weight(on ? .semibold : .regular)).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(on ? KriaColor.butter : KriaColor.fill, in: Capsule())
        }
        .buttonStyle(.plain).disabled(!enabled).opacity(enabled ? 1 : 0.4)
        .accessibilityAddTraits(on ? .isSelected : [])
        .accessibilityIdentifier(id)
    }

    // MARK: Size

    private var sizeSection: some View {
        let current = draft?.size ?? SlidePostSize.defaultSize(profile: profile)
        return VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Shape")
            HStack(alignment: .top, spacing: 10) {
                ForEach(SlidePostSize.allowed(profile: profile), id: \.self) { size in chip(size, selected: size == current) }
            }
            Text(sizeHint(current)).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("slidepost-size-hint")
        }
    }

    private func sizeHint(_ size: SlidePostSize) -> String {
        if SlidePostSize.isInstagram(profile) { return "Instagram uses one shape for the whole carousel, so every slide gets this size." }
        return size == .fullScreen ? "Full screen on TikTok. Pick 4:5 or 1:1 to post the same slides on Instagram too."
            : "TikTok shows this with bars around it. It also posts cleanly to Instagram."
    }

    private func chip(_ size: SlidePostSize, selected: Bool) -> some View {
        Button {
            let reset = session.setAspect(SlidePostSize.storedAspect(size.rawValue, profile: profile))
            notice = reset > 0 ? "Crops were reset on \(reset) slide\(reset == 1 ? "" : "s") for the new shape." : nil
        } label: {
            VStack(spacing: 6) {
                shapeThumbnail(size, selected: selected).frame(height: 44)
                Text(size.rawValue).font(KriaFont.body(13).weight(.semibold))
                Text(size.name).font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc).lineLimit(1).minimumScaleFactor(0.7)
                Text(size.worksOnBoth ? "Both apps" : " ").font(KriaFont.body(10)).foregroundStyle(KriaColor.zinc).lineLimit(1)
            }
            .foregroundStyle(KriaColor.ink)
            .frame(maxWidth: .infinity, minHeight: 44).padding(.vertical, 8)
            .background(selected ? KriaColor.butter : KriaColor.fill, in: RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(size.label)\(size.worksOnBoth ? ", works on Instagram and TikTok" : "")")
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("slidepost-size-chip-\(size.rawValue.replacingOccurrences(of: ":", with: "x").replacingOccurrences(of: ".", with: "_"))")
    }

    /// A small rectangle of the right shape, scaled to fit a 44pt box.
    private func shapeThumbnail(_ size: SlidePostSize, selected: Bool) -> some View {
        let canvas = size.canvas
        let scale = 44 / max(canvas.width, canvas.height)
        return RoundedRectangle(cornerRadius: 4)
            .fill(selected ? KriaColor.ink : KriaColor.ink.opacity(0.25))
            .frame(width: canvas.width * scale, height: canvas.height * scale)
            .frame(maxWidth: .infinity, maxHeight: 44)
    }

    // MARK: Photos

    private var photosSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            sectionTitle("Photos")
            HStack(spacing: 8) {
                fitModeButton("Fill", on: summaryIs(nil), id: "slidepost-size-fit-fill") { session.setFitForAllPhotos(nil) }
                fitModeButton("Fit", on: summaryIsFit, id: "slidepost-size-fit-fit") { session.setFitForAllPhotos(bars) }
            }
            if session.fitSummary == .mixed {
                Text("Photos differ. Pick one to set them all.").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("slidepost-size-fit-mixed")
            }
            if summaryIsFit {
                HStack(spacing: 8) {
                    ForEach(SlidePostFit.allCases, id: \.self) { fit in
                        let on = session.fitSummary == .uniform(fit)
                        Button {
                            bars = fit
                            session.setFitForAllPhotos(fit)
                        } label: {
                            Text(fit.label).font(KriaFont.body(13).weight(on ? .semibold : .regular)).foregroundStyle(KriaColor.ink)
                                .frame(maxWidth: .infinity, minHeight: 44)
                                .background(on ? KriaColor.butter : KriaColor.fill, in: Capsule())
                        }
                        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
                        .accessibilityIdentifier("slidepost-size-bars-\(fit.rawValue)")
                    }
                }
            }
            Text(summaryIsFit ? "Each photo shows whole, with bars around it." : "Each photo fills the frame; its edges may be trimmed.")
                .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            if hasVideo {
                Text("Videos always fill the frame.").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
            }
        }
    }

    private func summaryIs(_ fit: SlidePostFit?) -> Bool { session.fitSummary == .uniform(fit) }
    private var summaryIsFit: Bool {
        if case .uniform(let fit) = session.fitSummary, fit != nil { return true }
        return false
    }

    private func fitModeButton(_ title: String, on: Bool, id: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(KriaFont.body(14).weight(on ? .semibold : .regular)).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(on ? KriaColor.butter : KriaColor.fill, in: Capsule())
        }
        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
        .accessibilityIdentifier(id)
    }

    private func sectionTitle(_ text: String) -> some View {
        Text(text).font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.zinc)
    }
}
