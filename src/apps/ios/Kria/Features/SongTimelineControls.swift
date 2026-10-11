import SwiftUI

// The song timeline's controls around the lanes (KRI-575): the transport (the play button), the action bar for
// the selected clip or empty spot, and the tray of clips left off the song.

/// Filled capsule for the timeline's actions: fills not outlines, 44 pt tall.
struct SongTimelineChipStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var prominent = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(14).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 14)
            .frame(minWidth: 44, minHeight: 44)
            .background(prominent ? KriaColor.butter : KriaColor.paper, in: Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.7 : 1) : 0.4)
    }
}

/// Lays children out left to right and wraps them onto new rows, so the action chips fit any width.
struct SongTimelineWrap: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let rows = arrange(subviews, width: proposal.width ?? .infinity)
        return CGSize(width: rows.map(\.width).max() ?? 0, height: rows.reduce(0) { $0 + $1.height } + CGFloat(max(0, rows.count - 1)) * spacing)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for row in arrange(subviews, width: bounds.width) {
            var x = bounds.minX
            for index in row.indices {
                let size = subviews[index].sizeThatFits(.unspecified)
                subviews[index].place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
                x += size.width + spacing
            }
            y += row.height + spacing
        }
    }

    private struct Row { var indices: [Int] = []; var width: CGFloat = 0; var height: CGFloat = 0 }

    private func arrange(_ subviews: Subviews, width: CGFloat) -> [Row] {
        var rows: [Row] = [Row()]
        for index in subviews.indices {
            let size = subviews[index].sizeThatFits(.unspecified)
            let used = rows[rows.count - 1].width
            if !rows[rows.count - 1].indices.isEmpty, used + spacing + size.width > width { rows.append(Row()) }
            var row = rows[rows.count - 1]
            row.width += (row.indices.isEmpty ? 0 : spacing) + size.width
            row.height = max(row.height, size.height)
            row.indices.append(index)
            rows[rows.count - 1] = row
        }
        return rows
    }
}

// MARK: - Transport

/// Play/pause, previous/next clip, the time readout and zoom. Observes the clock; nothing else re-renders per frame.
struct SongTimelineTransport: View {
    @ObservedObject var clock: SongTimelineClock
    let stops: [SongTimelineMath.Stop]
    let canPlay: Bool
    let onPlayPause: () -> Void
    let onPrevious: () -> Void
    let onNext: () -> Void

    var body: some View {
        let hasPrevious = SongTimelineMath.previousStop(before: clock.currentTime, in: stops) != nil
        let hasNext = SongTimelineMath.nextStop(after: clock.currentTime, in: stops) != nil
        HStack(spacing: 6) {
            stepButton("backward.end.fill", label: "Previous clip", id: "song-timeline-prev", enabled: hasPrevious, action: onPrevious)
            playButton
            stepButton("forward.end.fill", label: "Next clip", id: "song-timeline-next", enabled: hasNext, action: onNext)
            timeReadout
            Spacer(minLength: 0)
            stepButton("minus.magnifyingglass", label: "Zoom out", id: "song-timeline-zoom-out", enabled: true) { clock.zoom(by: 1 / 1.4) }
            stepButton("plus.magnifyingglass", label: "Zoom in", id: "song-timeline-zoom-in", enabled: true) { clock.zoom(by: 1.4) }
        }
    }

    private var playButton: some View {
        Button(action: onPlayPause) {
            Image(systemName: clock.isPlaying ? "pause.fill" : "play.fill")
                .font(.system(size: 18, weight: .bold))
                .foregroundStyle(KriaColor.ink)
                .frame(width: 48, height: 48)
                .background(KriaColor.butter, in: Circle())
                .opacity(canPlay ? 1 : 0.4)
        }
        .buttonStyle(.plain)
        .disabled(!canPlay)
        .accessibilityLabel(clock.isPlaying ? "Pause song" : "Play song")
        .accessibilityIdentifier("song-timeline-play")
    }

    private var timeReadout: some View {
        let now = SongTimelineMath.timecode(clock.currentTime)
        let total = DurationFormatter.clock(clock.duration)
        return Text("\(now) / \(total)")
            .font(.system(size: 12, weight: .medium, design: .monospaced)).monospacedDigit()
            .foregroundStyle(KriaColor.mutedInk)
            .lineLimit(1).minimumScaleFactor(0.6)
            .padding(.leading, 2)
            .accessibilityElement()
            .accessibilityLabel("Current time")
            .accessibilityValue("\(now) of \(total)")
            .accessibilityIdentifier("song-timeline-current-time")
    }

    private func stepButton(_ symbol: String, label: String, id: String, enabled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(KriaColor.ink)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
                .opacity(enabled ? 1 : 0.35)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .accessibilityLabel(label)
        .accessibilityIdentifier(id)
    }
}

// MARK: - Action bar

/// What you can do with the selected clip or empty spot. Tapping a clip never moves it: the buttons do, and a
/// press-and-hold on the clip lifts it.
struct SongTimelineActionBar: View {
    struct Actions {
        var moveToTray: () -> Void
        var nudgeEarlier: () -> Void
        var nudgeLater: () -> Void
        var here: () -> Void
        var preview: () -> Void
        var fill: () -> Void
    }

    let selection: SongTimelineSelection?
    let clipNumber: Int
    let isSending: Bool
    let actions: Actions

    var body: some View {
        Group {
            switch selection {
            case .block:
                SongTimelineWrap(spacing: 8) {
                    chip("Move to tray", id: "to-tray", action: actions.moveToTray)
                    chip("−0.5 s", id: "earlier", label: "Move half a second earlier", action: actions.nudgeEarlier)
                    chip("+0.5 s", id: "later", label: "Move half a second later", action: actions.nudgeLater)
                    chip("Here", id: "here", label: "Start at the playhead", action: actions.here)
                    chip("Preview clip", id: "preview", action: actions.preview)
                }
            case .gap:
                SongTimelineWrap(spacing: 8) {
                    chip("Fill with a clip", id: "fill", prominent: true, action: actions.fill)
                }
            case nil:
                Text("Slide the timeline to move the playhead. Tap a clip or an empty spot to edit it.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(minHeight: 44, alignment: .leading)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("song-timeline-actions")
    }

    private func chip(_ title: String, id: String, label: String? = nil, prominent: Bool = false, action: @escaping () -> Void) -> some View {
        Button(title, action: action)
            .buttonStyle(SongTimelineChipStyle(prominent: prominent))
            .disabled(isSending)
            .accessibilityLabel(label ?? title)
            .accessibilityIdentifier("song-timeline-action-\(id)")
    }
}

// MARK: - Tray

struct SongTimelineTray: View {
    let clips: [(id: String, media: CreationAttachedMedia, number: Int)]
    let isSending: Bool
    let fillsSelectedSpot: Bool
    let onTap: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Tray").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(clips, id: \.id) { clip in
                        Button { if !isSending { onTap(clip.id) } } label: {
                            VStack(spacing: 4) {
                                CreationAttachmentThumbnail(media: clip.media)
                                    .background(RoundedRectangle(cornerRadius: 8).fill(KriaColor.zinc.opacity(0.12)))
                                Text("Clip \(clip.number)").font(KriaFont.body(11).weight(.semibold)).foregroundStyle(KriaColor.ink)
                            }
                            .frame(minWidth: 52, minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(fillsSelectedSpot
                            ? "Clip \(clip.number), in the tray, double tap to put it in the selected empty spot"
                            : "Clip \(clip.number), in the tray, double tap for options")
                        .accessibilityIdentifier("song-timeline-tray-\(clip.id)")
                    }
                }
            }
        }
    }
}
