import SwiftUI
import UIKit

/// KRI-450: the rich body of a decided plan card (clip filmstrip, music tile, caption lines, effect chips...),
/// rendered from the block's structured `payload`. Every branch falls back to the block's `summary`/`detail`
/// when the payload is missing or malformed (contract decode-with-fallback), so an old server or a bad payload
/// still reads as a plain text card. Shared by the live feed and the Review view.
enum PlanTimeFormat {
    /// 0:07, 1:05. Negative or non-finite values read as 0:00.
    static func clock(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds > 0 else { return "0:00" }
        let whole = Int(seconds.rounded(.down))
        return "\(whole / 60):" + String(format: "%02d", whole % 60)
    }
}

extension PlanSectionID {
    /// The longer name the Paper -B cards use.
    var cardTitle: String {
        switch self {
        case .clips: "Clips and order"
        case .look: "Look and motion"
        default: label
        }
    }

    /// The verb on a deciding card's pill.
    var workingVerb: String {
        switch self {
        case .title, .captions, .postCaption: "Writing"
        case .music: "Finding a track"
        case .clips, .sfx, .overlays: "Choosing"
        case .look: "Styling"
        }
    }
}

/// What sits under the header of an expanded, decided card.
struct PlanBlockContent: View {
    let block: PlanBlock
    var maxCaptionLines = 3

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            switch block.payload {
            case .clips(let payload) where !payload.clips.isEmpty:
                PlanClipFilmstrip(clips: payload.clips)
            case .music(let payload):
                PlanMusicRow(payload: payload)
            case .captions(let payload) where !payload.lines.isEmpty:
                PlanCaptionLines(payload: payload, limit: maxCaptionLines, sectionID: block.section)
            case .sfx(let payload) where !payload.items.isEmpty:
                PlanChipWrap(chips: payload.items.prefix(6).map { item in
                    item.label.map { "\($0) · \(PlanTimeFormat.clock(item.atS))" } ?? PlanTimeFormat.clock(item.atS)
                })
            case .overlays(let payload) where !payload.items.isEmpty:
                PlanOverlayRows(items: payload.items)
            case .look(let payload) where !payload.chips.isEmpty:
                PlanChipWrap(chips: payload.chips)
            case .postCaption(let payload):
                PlanPostCaptionBody(payload: payload)
            case .title, .clips, .captions, .sfx, .overlays, .look, .none:
                fallback
            }
        }
    }

    /// The text-only card: the title's own text is already in the header, so only detail shows for it.
    @ViewBuilder private var fallback: some View {
        if block.section != .title, block.skipped || !block.displaySummary.isEmpty, !headerShowsSummary {
            Text(block.displaySummary)
                .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("plan-feed.summary.\(block.section.rawValue)")
        }
        if let detail = block.detail, !detail.isEmpty {
            Text(detail)
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("plan-feed.detail.\(block.section.rawValue)")
        }
    }

    /// Whether the expanded header already printed the summary as its subtitle.
    private var headerShowsSummary: Bool { block.payload != nil && block.section != .title }
}

// MARK: - Clips

private struct PlanClipFilmstrip: View {
    let clips: [PlanClipItem]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            // Lazy: a 30-clip montage must not build (and read thumbnails for) every tile up front.
            LazyHStack(spacing: 6) {
                ForEach(clips) { clip in
                    PlanClipTile(clip: clip)
                }
            }
        }
        .scrollClipDisabled()
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(clips.count) clips")
    }
}

/// One clip: its thumbnail (the local media ledger first, then the signed URL from `GET /plan`), else a quiet
/// gradient. The order badge sits bottom-left.
struct PlanClipTile: View {
    let clip: PlanClipItem
    var width: CGFloat = 50
    var height: CGFloat = 68
    @State private var image: UIImage?

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(LinearGradient(colors: [Color(red: 0.92, green: 0.93, blue: 0.95), Color(red: 0.82, green: 0.84, blue: 0.88)], startPoint: .top, endPoint: .bottom))
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else if let url = clip.thumbnailURL {
                AsyncImage(url: url) { phase in
                    if let loaded = phase.image { loaded.resizable().scaledToFill() } else { Color.clear }
                }
            }
            Text("\(clip.index + 1)")
                .font(KriaFont.body(10).weight(.semibold))
                .foregroundStyle(Color.white)
                .frame(width: 16, height: 16)
                .background(KriaColor.ink, in: Circle())
                .padding(4)
        }
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .task(id: clip.mediaID) {
            guard image == nil, let id = clip.mediaID else { return }
            image = await MainActor.run { UIImage(contentsOfFile: CreationMediaPreview.url(mediaID: id).path) }
        }
        .accessibilityHidden(true)
    }
}

// MARK: - Music

private struct PlanMusicRow: View {
    let payload: PlanMusicPayload

    private var title: String {
        if let title = payload.title, !title.isEmpty { return title }
        switch payload.source {
        case .userSong: return "Your song"
        case .voiceover: return "Your voiceover"
        case .catalog: return "Music"
        }
    }

    private var detail: String {
        var parts: [String] = []
        if let artist = payload.artist, !artist.isEmpty { parts.append(artist) }
        if let bpm = payload.bpm, bpm > 0 { parts.append("\(Int(bpm.rounded())) BPM") }
        if let start = payload.startS, start > 0 { parts.append("starts at \(PlanTimeFormat.clock(start))") }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: 13, style: .continuous)
                    .fill(LinearGradient(colors: [KriaColor.butterPale, KriaColor.butterDeep], startPoint: .topLeading, endPoint: .bottomTrailing))
                if let url = payload.artURL {
                    AsyncImage(url: url) { phase in
                        if let loaded = phase.image { loaded.resizable().scaledToFill() } else { Color.clear }
                    }
                } else {
                    Image(systemName: "music.note").font(.system(size: 16, weight: .semibold)).foregroundStyle(KriaColor.ink.opacity(0.7))
                }
            }
            .frame(width: 44, height: 44)
            .clipShape(RoundedRectangle(cornerRadius: 13, style: .continuous))
            VStack(alignment: .leading, spacing: 0) {
                Text(title).font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink).lineLimit(1)
                if !detail.isEmpty {
                    Text(detail).font(KriaFont.body(13)).foregroundStyle(KriaColor.ink.opacity(0.7)).lineLimit(1)
                }
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Captions

struct PlanCaptionLines: View {
    let payload: PlanCaptionsPayload
    var limit = 3
    var sectionID: PlanSectionID = .captions

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(payload.lines.prefix(limit)) { line in
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(PlanTimeFormat.clock(line.startS))
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink.opacity(0.65))
                        .frame(width: 34, alignment: .leading)
                    Text(line.text)
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            let more = max(payload.count, payload.lines.count) - min(limit, payload.lines.count)
            if more > 0 {
                Text("+\(more) more")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .padding(.leading, 46)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("plan-feed.captions.\(sectionID.rawValue)")
    }
}

// MARK: - Chips, overlays, post caption

struct PlanChipWrap: View {
    let chips: [String]

    var body: some View {
        PlanChipFlow(spacing: 8) {
            ForEach(Array(chips.enumerated()), id: \.offset) { _, chip in
                Text(chip)
                    .font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.ink)
                    .padding(.horizontal, 12).frame(height: 28)
                    .background(KriaColor.ink.opacity(0.07), in: Capsule())
            }
        }
    }
}

private struct PlanOverlayRows: View {
    let items: [PlanOverlayItem]

    private func icon(_ kind: PlanOverlayItem.Kind) -> String {
        switch kind {
        case .image: "photo"
        case .video: "video"
        case .textCard: "textformat"
        case .motion: "wand.and.stars"
        case .visual: "photo.on.rectangle"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(items.prefix(4)) { item in
                HStack(spacing: 10) {
                    Image(systemName: icon(item.kind)).font(.system(size: 12, weight: .semibold)).foregroundStyle(KriaColor.ink.opacity(0.7))
                        .frame(width: 18)
                    Text(item.label ?? item.kind.rawValue.replacingOccurrences(of: "_", with: " ").capitalized)
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink).lineLimit(1)
                    Spacer(minLength: 4)
                    Text(PlanTimeFormat.clock(item.startS)).font(KriaFont.body(13)).foregroundStyle(KriaColor.ink.opacity(0.65))
                }
            }
            if items.count > 4 {
                Text("+\(items.count - 4) more").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

private struct PlanPostCaptionBody: View {
    let payload: PlanPostCaptionPayload

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(payload.text)
                .font(KriaFont.body(14)).foregroundStyle(KriaColor.ink)
                .fixedSize(horizontal: false, vertical: true)
            if !payload.hashtags.isEmpty {
                Text(payload.hashtags.map { "#\($0)" }.joined(separator: " "))
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Skeleton (deciding)

/// The grey placeholder shapes of a card that is still being decided, shaped like the content it will hold.
struct PlanSkeletonContent: View {
    let section: PlanSectionID

    var body: some View {
        Group {
            switch section {
            case .clips:
                HStack(spacing: 6) {
                    ForEach(0..<5, id: \.self) { _ in
                        RoundedRectangle(cornerRadius: 12, style: .continuous).fill(KriaColor.ink.opacity(0.10)).frame(width: 50, height: 68)
                    }
                }
            case .music:
                HStack(spacing: 12) {
                    RoundedRectangle(cornerRadius: 13, style: .continuous).fill(KriaColor.ink.opacity(0.14)).frame(width: 44, height: 44)
                    VStack(alignment: .leading, spacing: 8) {
                        KriaSkeletonBar(width: 150)
                        KriaSkeletonBar(width: 110, strong: false)
                    }
                }
            case .sfx, .look:
                HStack(spacing: 8) {
                    KriaSkeletonBar(width: 90, height: 26)
                    KriaSkeletonBar(width: 110, height: 26, strong: false)
                }
            default:
                VStack(alignment: .leading, spacing: 8) {
                    KriaSkeletonBar(width: 200)
                    KriaSkeletonBar(width: 140, strong: false)
                }
            }
        }
        .accessibilityHidden(true)
    }
}
