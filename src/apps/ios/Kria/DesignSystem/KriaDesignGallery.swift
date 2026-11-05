#if DEBUG
import SwiftUI

/// Every shared primitive in its states, as one scrolling page per group. Open the previews in Xcode to
/// compare against the Paper board "Kria iOS — Component Redesign" (page 4, S4.01 to S4.11).
struct KriaDesignGallery: View {
    enum Page: Int, CaseIterable { case buttons, text, notices, rows, answers, sources, sheet }
    let page: Page

    @State private var text = ""
    @State private var filled = "A flat lay of fresh ingredients on a wooden table, soft morning light"
    @State private var segment = "portrait"

    var body: some View {
        switch page {
        case .sheet:
            KriaSheetScaffold(headerTitle: "Add media", heading: "Add footage", message: "Record yourself, or upload a video you already have.",
                              onBack: {}, onClose: {}) {
                KriaSourceRow(options: [.record(action: {}), .upload(action: {})], leadsWithFirst: true)
                KriaMediaRow(name: "IMG_2041.mov", status: .ready("Video · Ready"), trailing: .remove {}) { KriaThumbnailPlaceholder(systemImage: "video") }
            } pinnedBar: {
                Button("Done") {}.buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
            }
        default:
            ScrollView { VStack(alignment: .leading, spacing: 20) { content }.padding(24) }
                .background(KriaColor.softZinc)
        }
    }

    @ViewBuilder private var content: some View {
        switch page {
        case .buttons: buttons
        case .text: textPage
        case .notices: notices
        case .rows: rows
        case .answers: answers
        case .sources: sources
        case .sheet: EmptyView()
        }
    }

    private func caption(_ title: String) -> some View {
        Text(title).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
    }

    @ViewBuilder private var buttons: some View {
        caption("Primary / secondary / tertiary / destructive / ink")
        Button("Use image") {}.buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true))
        Button("Generate image") {}.buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true)).disabled(true)
            .kriaDisabledReason("Describe your image to start.")
        Button("Saving…") {}.buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: true))
        Button("Try again") {}.buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
        VStack { Button("Whole song") {}.buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true, onFill: true)) }
            .padding(12).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20))
        HStack {
            Button("Let Kria decide") {}.buttonStyle(.kriaTertiary)
            Button("Discard take") {}.buttonStyle(KriaButtonStyle(role: .tertiaryDestructive, size: .medium))
            Button("Remove clip") {}.buttonStyle(KriaButtonStyle(role: .destructive, size: .medium))
        }
        HStack {
            Button("Try again") {}.buttonStyle(KriaButtonStyle(role: .ink, size: .medium))
            Button {} label: { Image(systemName: "chevron.left") }.buttonStyle(.kriaIcon).accessibilityLabel("Back")
            Button {} label: { Image(systemName: "xmark") }.buttonStyle(KriaButtonStyle(role: .iconGlass, size: .medium))
                .padding(8).background(Color.brown, in: Capsule()).accessibilityLabel("Close")
        }
        caption("Segmented control, ink selected")
        KriaSegmentedControl(options: [(label: "Portrait", value: "portrait"), (label: "Square", value: "square"), (label: "Landscape", value: "landscape")],
                             selection: $segment, fillsWidth: true)
        KriaSegmentedControl(options: [(label: "Off", value: "off"), (label: "On", value: "on")], selection: $segment)
    }

    @ViewBuilder private var textPage: some View {
        ForEach(KriaType.allCases, id: \.self) { type in
            Text("\(String(describing: type)) \(Int(type.size))").kriaType(type).foregroundStyle(KriaColor.ink)
        }
        KriaSectionLabel(title: "Group heading in sentence case")
        KriaField(label: "Name", text: $text, placeholder: "Name (optional)", helper: "Helps Kria know where the image fits.")
        KriaField(label: "Prompt", text: $filled, maxCount: 1500, isMultiline: true, minHeight: 104)
        KriaField(label: "Prompt", text: $text, placeholder: "Describe the image you want", errorMessage: "Add at least one word.")
        KriaField(label: "Locked", text: $filled).disabled(true)
        KriaEmptyState(title: "Start your first video", message: "Tell Kria what you want, then add your footage.", action: "New video", onAction: {})
        HStack {
            ForEach([ProjectStatus.draft, .rendering, .ready, .failed], id: \.self) { KriaStatusPill(status: $0) }
        }
    }

    @ViewBuilder private var notices: some View {
        KriaNoticeCard(tone: .info, title: "We’re not sure it finished", message: "Kria won’t start another one on its own. Check again in a minute.",
                       action: KriaNoticeAction(title: "Check again") {}, announces: false)
        KriaNoticeCard(tone: .warning, title: "This image changed after you started", message: "Discard this result and start again from the newest version.",
                       action: KriaNoticeAction(title: "Start again") {}, announces: false)
        KriaNoticeCard(tone: .failure, title: "Kria couldn’t make this image", message: "Your prompt is still here. Try again or change it.",
                       action: KriaNoticeAction(title: "Try again") {}, link: KriaNoticeAction(title: "Edit prompt") {}, announces: false)
        KriaNoticeCard(tone: .success, title: "Clips saved", message: "4 clips are ready to use.", announces: false)
        KriaNoticeCard(tone: .failure, title: "1 clip couldn’t upload", action: KriaNoticeAction(title: "Retry") {}, style: .banner, announces: false)
        HStack(spacing: 12) {
            KriaSpinner(size: 13); KriaSpinner(size: 16); KriaSpinner(size: 20)
            KriaCheckDisc(size: 16); KriaCheckDisc(size: 20); KriaCheckDisc(size: 24, dash: true)
        }
    }

    @ViewBuilder private var rows: some View {
        KriaStepRail(steps: ["Footage", "Song", "Overlays"], current: 1)
        KriaStepRail(steps: ["Footage", "Voiceover"], current: 0)
        KriaMediaRow(name: "menu.jpg", status: .ready("Photo · Ready"), trailing: .remove {}) { KriaThumbnailPlaceholder() }
        KriaMediaRow(name: "Coffee flat lay", status: .ready("AI image · Ready"),
                     trailing: .menu([KriaMenuAction(title: "Edit with AI", systemImage: "sparkles") {}, KriaMenuAction(title: "Remove", isDestructive: true) {}])) { KriaThumbnailPlaceholder() }
        KriaMediaRow(name: "IMG_2041.mov", status: .preparing) { KriaThumbnailPlaceholder(systemImage: "video") }
        KriaMediaRow(name: "Clip 3.mov", status: .uploading(0.42)) { KriaThumbnailPlaceholder(systemImage: "video") }
        KriaMediaRow(name: "Clip 4.mov", status: .failed("Couldn’t add"), onRetry: {}) { KriaThumbnailPlaceholder(failed: true) }
    }

    @ViewBuilder private var answers: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                KriaAnswerChip(title: "After I say hello", action: {})
                KriaAnswerChip(title: "Start right away", isRecommended: true, action: {})
            }
            HStack {
                KriaAnswerChip(title: "Start right away", state: .selected, action: {})
                KriaAnswerChip(title: "Start right away", state: .sending, action: {})
            }
            HStack {
                KriaAnswerChip(title: "Type my own", typeMyOwn: true, action: {})
                KriaAnswerChip(title: "Disabled", action: {}).disabled(true)
            }
            KriaAnswerChip(title: "Start with the sunset", caption: "Your best shot is at 0:12", action: {})
        }
        KriaChoiceRow(title: "Keep it under 30 seconds", detail: "I’ll drop the two slowest clips.", isRecommended: true, action: {})
        KriaChoiceRow(title: "Keep every clip you picked, even the two slow ones near the end of the trip",
                      detail: "Long titles and details wrap and the block grows in height.", action: {})
        KriaChoiceRow(title: "Sending", state: .sending, action: {})
        KriaChoiceRow(title: "Held", detail: "Detail in paper at 80%", state: .selected, action: {})
        KriaChoiceRow(title: "Disabled", action: {}).disabled(true)
        KriaChoiceRow(title: "Type my own", typeMyOwn: true, action: {})
        Button("Let Kria decide") {}.buttonStyle(.kriaTertiary).frame(maxWidth: .infinity)
    }

    @ViewBuilder private var sources: some View {
        caption("Video 2-up, Talk to camera lead")
        KriaSourceRow(options: [.record(action: {}), .upload(action: {})], leadsWithFirst: true)
        caption("Video 2-up, equal")
        KriaSourceRow(options: [.record(action: {}), .upload(action: {})])
        caption("Audio 3-up")
        KriaSourceRow(options: [
            .record(symbol: "mic.fill", caption: "Use your mic", action: {}),
            KriaSourceOption(id: "file", symbol: "arrow.up.doc", title: "Upload", caption: "Use an audio file", action: {}),
            KriaSourceOption(id: "ai", symbol: "waveform", title: "AI voice", caption: "Read by AI", action: {}),
        ])
        caption("Visual 3-up")
        KriaSourceRow(options: [
            KriaSourceOption(id: "camera", symbol: "camera.fill", title: "Camera", caption: "Film now", action: {}),
            .upload(caption: "Use existing media", action: {}),
            KriaSourceOption(id: "gen", symbol: "sparkles", title: "Generate", caption: "Make with AI", action: {}),
        ])
        caption("Compact, disabled, preparing")
        KriaSourceRow(options: [.record(action: {}), .upload(action: {})], layout: .compact)
        KriaSourceRow(options: [.record(isEnabled: false, action: {}), .upload(isEnabled: false, action: {})],
                      disabledReason: "You’ve added 10 clips, the most for this format.")
        KriaSourceRow(options: [.upload(isPreparing: true, action: {})])
        caption("On a fill card")
        KriaSourceRow(options: [.record(action: {}), .upload(action: {})], onFill: true)
            .padding(12).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20))
        caption("Camera")
        HStack { KriaCameraUploadButton {}; Spacer() }.padding(16).background(Color.black)
    }
}


#Preview("Buttons and segments") { KriaDesignGallery(page: .buttons) }
#Preview("Type, fields, empty, status") { KriaDesignGallery(page: .text) }
#Preview("Notices and progress") { KriaDesignGallery(page: .notices) }
#Preview("Step rail and media rows") { KriaDesignGallery(page: .rows) }
#Preview("Answer chips and blocks") { KriaDesignGallery(page: .answers) }
#Preview("Source rows") { KriaDesignGallery(page: .sources) }
#Preview("Sheet scaffold") { KriaDesignGallery(page: .sheet) }
#endif
