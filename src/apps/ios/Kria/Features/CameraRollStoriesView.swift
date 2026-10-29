import SwiftUI
import AVKit
import PhotosUI

struct CameraRollStoriesView: View {
    let api: any KriaAPIClient
    let collection: CameraRollCollection
    let result: CameraRollStories
    var cacheOnly = false
    /// One idea on the camera roll's detail page (Paper A4): no navigation of its own, and a bar of actions at
    /// the bottom. The standalone form (several ideas, its own navigation) stays for the offline review host.
    var embedded = false
    /// Offline fixtures have no pictures to fetch; tiles show their colour instead.
    var thumbnailsDisabled = false
    /// "Use this idea" handed to an owner that outlives this view (the Camera roll's model). Without one, the
    /// view runs the work itself.
    var onUse: (@MainActor (CameraRollStory) -> Void)? = nil
    var onAddBackground: @MainActor (String) -> Void = { _ in }
    /// Opens the chat; false when the workspace will not open it.
    let onSelected: @MainActor (CreationThread) async -> Bool
    var onReviewed: @MainActor (CameraRollStories) -> Void = { _ in }
    /// "Not this one" in the embedded form: the idea is deleted (and not offered again) rather than just hidden.
    var onDelete: @MainActor () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    @State private var stories: [CameraRollStory] = []
    @State private var busy = false
    @State private var error: String?
    @State private var inspection: String?
    @State private var question = "What is clearly visible in this interval?"
    @State private var player: AVPlayer?
    @State private var showsPreview = false
    @State private var editingWording: CameraRollStory?
    @State private var use: CameraRollIdeaUse?
    @State private var showsUse = false
    @State private var taste: EditorialTasteProfile?
    @State private var showsTaste = false
    @State private var ratingStory: CameraRollStory?
    @State private var feedbackSaved = Set<String>()

    var body: some View {
        Group {
            if embedded { embeddedContent } else { standaloneContent }
        }
        .onAppear { stories = result.stories }
        .task {
            taste = try? await api.editorialTaste()
            if result.coverage != nil {
                for story in result.stories {
                    try? await api.recordCameraRollStoryExposure(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0)
                }
            }
        }
        .onDisappear {
            onReviewed(CameraRollStories(stories: stories, coverage: result.coverage, suggestedResearchTopic: result.suggestedResearchTopic))
            player?.pause()
        }
        .interactiveDismissDisabled(busy)
        .sheet(item: $ratingStory) { story in
            IdeaFeedbackView(api: api, collectionID: collection.id, story: story) {
                feedbackSaved.insert("\(story.id)-\(story.revision)")
            }
        }
        .sheet(isPresented: $showsPreview, onDismiss: { player?.pause(); player = nil }) {
            if let player { VideoPlayer(player: player).ignoresSafeArea(edges: .bottom) }
        }
        .sheet(item: $editingWording) { story in
            if let plan = story.contentPlan {
                CameraRollWordingEditor(plan: plan) { revised in
                    busy = true; error = nil; defer { busy = false }
                    let changed = try await api.updateCameraRollContentPlan(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, plan: revised)
                    stories = stories.map { $0.id == changed.id ? changed : $0 }
                }
            }
        }
        .sheet(isPresented: $showsUse) { if let use { CameraRollUseSheet(use: use) } }
    }

    private var standaloneContent: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                if let coverage = result.coverage, !coverage.unindexed.isEmpty {
                    Label("\(coverage.unindexed.count) \(coverage.unindexed.count == 1 ? "video needs" : "videos need") source or analysis review before inclusion.", systemImage: "info.circle")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                } else if let coverage = result.coverage, !coverage.incompleteSpeech.isEmpty {
                    Label("Some audio needs checking", systemImage: "info.circle")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                }
                if let topic = result.suggestedResearchTopic, !stories.contains(where: { $0.research != nil }) {
                    VStack(alignment: .leading, spacing: 6) {
                        Button { dismiss(); onAddBackground(topic) } label: {
                            Label("Add background facts", systemImage: "text.book.closed")
                        }.buttonStyle(KriaSecondaryButtonStyle(minHeight: 44)).accessibilityIdentifier("story-add-background")
                        Text("You confirm the topic first. Only “\(topic)” is searched, never your footage.")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                }
                if stories.allSatisfy(\.rejected) { ContentUnavailableView("No story to suggest", systemImage: "photo.stack", description: Text("Try a smaller collection or a more specific story. You can also create a video from your chosen footage in chat.")) }
                ForEach(stories.filter { !$0.rejected }) { story in
                    VStack(alignment: .leading, spacing: 16) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(story.title).font(KriaFont.headline(26))
                            Spacer()
                            Text("\(story.duration, specifier: "%.1f") sec").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        }
                        if let plan = story.contentPlan {
                            if let label = plan.formLabel {
                                Text(label).font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.mutedInk)
                                    .accessibilityIdentifier("story-form-\(story.id)")
                            }
                            Text(plan.angle).font(KriaFont.body(16).weight(.semibold))
                        }
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(story.moments) { moment in
                                    if let entry = collection.entries.first(where: { $0.savedAsset?.id == moment.assetID }) {
                                        Button { Task { await preview(moment) } } label: {
                                            CameraRollCollectionThumbnail(entry: entry, api: api, metadataOnly: thumbnailsDisabled, size: CGSize(width: 104, height: 138))
                                                .clipShape(RoundedRectangle(cornerRadius: 12))
                                        }.buttonStyle(.plain).accessibilityLabel("Preview \(moment.text)")
                                    }
                                }
                            }
                        }
                        if let plan = story.contentPlan {
                            VStack(alignment: .leading, spacing: 10) {
                                HStack {
                                    Text("Opening text").font(KriaFont.body(13).weight(.semibold))
                                    Spacer()
                                    Button("Edit") { editingWording = story }.font(KriaFont.body(13)).accessibilityIdentifier("story-edit-wording")
                                }
                                Text("“\(plan.openingText)”").font(KriaFont.body(16).weight(.medium))
                                if let closing = plan.closingText { Text("Then: “\(closing)”").font(KriaFont.body(14)) }
                            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                                .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 16))
                        }
                        if let plan = story.contentPlan, !(plan.openQuestions ?? []).isEmpty {
                            CameraRollOpenQuestionsView(story: story, plan: plan) { revised in
                                await resolve(story, plan: revised)
                            }
                        }
                        if taste?.enabled == true && taste?.memoryPaused == false {
                            Button(feedbackSaved.contains("\(story.id)-\(story.revision)") ? "Feedback saved" : "Would you post this?") { ratingStory = story }
                                .font(KriaFont.body(13)).frame(minHeight: 44)
                                .accessibilityIdentifier("idea-feedback")
                        }
                        if let research = story.research { CameraRollIdeaResearchView(research: research) }
                        Button { select(story) } label: {
                            Text("Use this idea").frame(maxWidth: .infinity)
                        }.buttonStyle(KriaPrimaryButtonStyle()).disabled(!(story.contentPlan?.unresolvedQuestions.isEmpty ?? true))
                            .accessibilityIdentifier("story-choose-\(story.id)")
                        DisclosureGroup("See clips and details") {
                            storyDetails(story)
                        }.font(KriaFont.body(14))
                        Divider().padding(.top, 12)
                    }.padding(.vertical, 12).disabled(busy)
                }
                DisclosureGroup("Ask about a clip") {
                    TextField("Question about an interval", text: $question)
                    Text("AI checks can be wrong. Review the footage before choosing.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    if let inspection { Text(inspection).textSelection(.enabled) }
                }
                if busy { ProgressView("Preparing…") }
                if let error { Text(error).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("story-error") }
                }.padding(24)
            }
            .background(KriaColor.paper).kriaPage()
            .navigationTitle("Story ideas").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if taste != nil { Button("Idea preferences") { showsTaste = true }.accessibilityIdentifier("editorial-preferences") }
                }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.disabled(busy) }
            }
            .navigationDestination(isPresented: $showsTaste) {
                CameraRollTasteView(api: api) { value in taste = value }
            }
        }
    }

    // MARK: Embedded (Paper A4)

    @ViewBuilder private var embeddedContent: some View {
        if let story = stories.first(where: { !$0.rejected }) ?? result.stories.first {
            ZStack(alignment: .bottom) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(embeddedKicker(story)).cameraRollKicker()
                            Text(story.title).cameraRollTitle().fixedSize(horizontal: false, vertical: true)
                                .accessibilityAddTraits(.isHeader)
                        }
                        clipStrip(story)
                        if let plan = story.contentPlan {
                            VStack(alignment: .leading, spacing: 6) {
                                HStack {
                                    Text("Opening text").cameraRollKicker()
                                    Spacer()
                                    Button("Edit") { editingWording = story }.font(KriaFont.body(13).weight(.semibold)).frame(minHeight: 44)
                                        .accessibilityIdentifier("story-edit-wording")
                                }
                                Text(plan.openingText).font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
                                if let closing = plan.closingText { Text("Then: \(closing)").font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk) }
                            }
                            .padding(.horizontal, 16).padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .leading)
                            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                            if !(plan.openQuestions ?? []).isEmpty {
                                CameraRollOpenQuestionsView(story: story, plan: plan) { revised in await resolve(story, plan: revised) }
                            }
                        }
                        if let research = story.research { CameraRollIdeaResearchView(research: research) }
                        DisclosureGroup("See clips and details") { storyDetails(story) }.font(KriaFont.body(14))
                        if busy { ProgressView("Preparing…").accessibilityIdentifier("story-busy") }
                        if let error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("story-error") }
                    }.padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 190)
                }.kriaScrollEdgeFade()
                actionBar(story)
            }
            .background(KriaColor.paper).kriaPage()
        } else {
            Text("This idea was removed.").font(KriaFont.body(15)).foregroundStyle(KriaColor.mutedInk).frame(maxHeight: .infinity)
        }
    }

    private func embeddedKicker(_ story: CameraRollStory) -> String {
        let seconds = Int(story.duration.rounded())
        return "\(story.contentPlan?.formLabel ?? "Idea") · \(story.moments.count) \(story.moments.count == 1 ? "clip" : "clips") · \(seconds / 60):\(String(format: "%02d", seconds % 60))"
    }

    private func clipStrip(_ story: CameraRollStory) -> some View {
        let shown = Array(story.moments.prefix(3))
        return HStack(spacing: 8) {
            ForEach(shown) { moment in
                if let entry = collection.entries.first(where: { $0.savedAsset?.id == moment.assetID }) {
                    Button { Task { await preview(moment) } } label: {
                        CameraRollCollectionThumbnail(entry: entry, api: api, metadataOnly: thumbnailsDisabled, size: CGSize(width: 84, height: 112))
                            .overlay(alignment: .topLeading) {
                                Text(GalleryTileChrome.duration(moment.end - moment.start)).font(KriaFont.body(11).weight(.semibold)).foregroundStyle(.white)
                                    .padding(.horizontal, 7).padding(.vertical, 3).background(KriaColor.ink.opacity(0.62), in: Capsule()).padding(6)
                            }
                            .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    }.buttonStyle(.plain).accessibilityLabel("Preview \(moment.text)")
                }
            }
            if story.moments.count > shown.count {
                Text("+\(story.moments.count - shown.count)").font(KriaFont.body(14).weight(.bold)).foregroundStyle(KriaColor.mutedInk)
                    .frame(width: 84, height: 112).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
        }
    }

    private func actionBar(_ story: CameraRollStory) -> some View {
        VStack(spacing: 8) {
            Button { select(story) } label: {
                Text("Use this idea").font(KriaFont.body(16).weight(.bold)).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
            }
            .buttonStyle(.plain).disabled(busy || !(story.contentPlan?.unresolvedQuestions.isEmpty ?? true))
            .opacity(busy || !(story.contentPlan?.unresolvedQuestions.isEmpty ?? true) ? 0.45 : 1)
            .accessibilityIdentifier("story-choose-\(story.id)")
            HStack(spacing: 8) {
                if taste?.enabled == true && taste?.memoryPaused == false {
                    Button { ratingStory = story } label: { secondaryLabel(feedbackSaved.contains("\(story.id)-\(story.revision)") ? "Feedback saved" : "Would you post this?") }
                        .buttonStyle(.plain).accessibilityIdentifier("idea-feedback")
                }
                Button { onDelete() } label: { secondaryLabel("Not this one") }
                    .buttonStyle(.plain).disabled(busy).accessibilityIdentifier("story-not-this-one")
            }
        }
        .padding(12).background(Color.white, in: RoundedRectangle(cornerRadius: 30, style: .continuous))
        .shadow(color: KriaColor.ink.opacity(0.18), radius: 28, y: 6).padding(.horizontal, 12).padding(.bottom, 28)
    }

    private func secondaryLabel(_ title: String) -> some View {
        Text(title).font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.ink)
            .frame(maxWidth: .infinity, minHeight: 40).background(KriaColor.fill, in: Capsule()).frame(minHeight: 44).contentShape(Capsule())
    }

    private func storyDetails(_ story: CameraRollStory) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            if let plan = story.contentPlan {
                Text(plan.whyItWorks).font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                Text("For \(plan.audience)").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            }
            DisclosureGroup("Story arc") {
                Text(story.rationale)
                Text(story.hook)
                Text(story.payoff)
            }.font(KriaFont.body(14))
            Toggle("Original sound", isOn: Binding(
                get: { story.audioPolicy != "muted" },
                set: { keep in Task { await change(story, order: nil, audioPolicy: keep ? "preserve_source_audio" : "muted", action: keep ? "sound-original" : "sound-muted") } }
            )).accessibilityIdentifier("story-sound")
            if story.audioPolicy == "muted" { Text("Muted footage").font(.footnote).foregroundStyle(.secondary) }
            Text("From your footage").font(KriaFont.body(13).weight(.semibold))
                .accessibilityIdentifier("story-provenance-footage-\(story.id)")
            ForEach(Array(story.moments.enumerated()), id: \.element.id) { index, moment in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(alignment: .top, spacing: 12) {
                        if let entry = collection.entries.first(where: { $0.savedAsset?.id == moment.assetID }) {
                            CameraRollCollectionThumbnail(entry: entry, api: api, metadataOnly: thumbnailsDisabled, size: CGSize(width: 72, height: 88))
                                .clipShape(RoundedRectangle(cornerRadius: 10))
                        }
                        VStack(alignment: .leading, spacing: 6) {
                            Text(moment.text).font(KriaFont.body(14).weight(.medium)).lineLimit(3)
                            if moment.evidenceScope == "whole_source_context" {
                                Text("Whole clip · no timed highlight detected").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                            }
                            Text("\(sourceLabel(moment)) · \(moment.start, specifier: "%.1f")–\(moment.end, specifier: "%.1f")s")
                                .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                            if let reasons = story.contentPlan?.beatReasons, reasons.indices.contains(index) {
                                Text(reasons[index]).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                            }
                        }
                    }
                    HStack {
                        Button("Preview") { Task { await preview(moment) } }
                        Menu("Replace") {
                            ForEach((result.coverage?.moments ?? []).filter { alternative in
                                !story.moments.contains { $0.id == alternative.id } && (1...20).contains(alternative.end - alternative.start)
                            }) { alternative in
                                Button("\(sourceLabel(alternative)) · \(Int(alternative.start))s · \(alternative.text.prefix(60))") {
                                    Task { await replace(story, index: index, moment: alternative) }
                                }.accessibilityIdentifier("story-alternative-\(alternative.id)")
                            }
                            Divider()
                            if index > 0 {
                                Button("Move earlier") {
                                    var order = story.moments.map(\.id); order.swapAt(index, index - 1)
                                    Task { await change(story, order: order, audioPolicy: nil, action: "earlier-\(index)") }
                                }
                            }
                            if index + 1 < story.moments.count {
                                Button("Move later") {
                                    var order = story.moments.map(\.id); order.swapAt(index, index + 1)
                                    Task { await change(story, order: order, audioPolicy: nil, action: "later-\(index)") }
                                }
                            }
                            if story.moments.count > 1 {
                                Button("Remove from story", role: .destructive) {
                                    Task { await change(story, order: story.moments.filter { $0.id != moment.id }.map(\.id), audioPolicy: nil, action: "remove-\(index)") }
                                }
                            }
                        }.accessibilityIdentifier("story-replace-\(index)")
                    }.buttonStyle(KriaSecondaryButtonStyle(minHeight: 44))
                    Button("Check this clip") { Task { await inspect(moment) } }
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk).frame(minHeight: 44)
                }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                    .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16))
            }
            Button("Not this story", role: .destructive) { Task { await reject(story) } }
        }.padding(.top, 12)
    }

    private func sourceLabel(_ moment: CameraRollMoment) -> String {
        let index = collection.entries.firstIndex { $0.savedAsset?.id == moment.assetID }
        return index.map { "Video \($0 + 1)" } ?? "Source video"
    }
    @MainActor private func change(_ story: CameraRollStory, order: [UUID]?, audioPolicy: String?, action: String) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let changed = try await api.changeCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, order: order, audioPolicy: audioPolicy, eventID: "\(action)-\(story.id)-\(story.revision)")
            stories = stories.map { $0.id == changed.id ? changed : $0 }
        } catch { self.error = CameraRollCollectionFailure.message(error) }
    }
    @MainActor private func resolve(_ story: CameraRollStory, plan: CameraRollContentPlan) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let changed = try await api.updateCameraRollContentPlan(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, plan: plan)
            stories = stories.map { $0.id == changed.id ? changed : $0 }
        } catch { self.error = CameraRollCollectionFailure.message(error) }
    }
    @MainActor private func replace(_ story: CameraRollStory, index: Int, moment: CameraRollMoment) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let changed = try await api.correctCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, index: index, momentID: moment.id, eventID: "replace-\(story.id)-\(story.revision)-\(index)-\(moment.id)")
            stories = stories.map { $0.id == changed.id ? changed : $0 }
        } catch { self.error = CameraRollCollectionFailure.message(error) }
    }
    @MainActor private func reject(_ story: CameraRollStory) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let changed = try await api.rejectCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, eventID: "reject-\(story.id)-\(story.revision)")
            stories = stories.map { $0.id == changed.id ? changed : $0 }
        } catch { self.error = CameraRollCollectionFailure.message(error) }
    }
    @MainActor private func inspect(_ moment: CameraRollMoment) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let result = try await api.inspectCameraRollMoment(collectionID: collection.id, revision: collection.serverRevision ?? 0, momentID: moment.id, question: question.isEmpty ? "What is visible?" : question, cacheOnly: cacheOnly)
            inspection = result.answer.isEmpty ? "The check could not answer confidently." : "\(result.answer) — \(result.evidence) (AI check; review the footage.)"
        } catch { self.error = CameraRollCollectionFailure.message(error) }
    }
    @MainActor private func preview(_ moment: CameraRollMoment) async {
        busy = true; error = nil; defer { busy = false }
        do {
            guard let owner = api.mediaLibraryOwner, let asset = collection.entries.compactMap(\.savedAsset).first(where: { $0.id == moment.assetID }), let source = asset.sources.first(where: { $0.id == moment.sourceID && $0.generation == moment.generation }) else { throw APIError.invalidResponse }
            let url: URL
            if let fingerprint = asset.fingerprint, let local = MediaLibraryOriginals(owner: owner).localURL(assetID: asset.id, fingerprint: fingerprint) { url = local }
            else {
                let access = try await api.savedMediaAccess(assetID: asset.id, sourceID: source.id)
                guard access.generation == source.generation else { throw APIError.invalidResponse }
                url = access.url
            }
            let item = AVPlayerItem(url: url)
            item.forwardPlaybackEndTime = CMTime(seconds: moment.end, preferredTimescale: 600)
            let created = AVPlayer(playerItem: item)
            await created.seek(to: CMTime(seconds: moment.start, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
            player = created; showsPreview = true
        } catch { self.error = CameraRollCollectionFailure.message(error) }
    }
    /// Opens the idea's progress: on the owner that keeps it running, or here for the standalone review.
    @MainActor private func select(_ story: CameraRollStory) {
        if let onUse { onUse(story); return }
        showsUse = true
        if let use, use.story.id == story.id { return }
        let collection = collection
        let started = CameraRollIdeaUse(story: story, api: api, collection: { collection }, open: onSelected)
        use = started
        started.start()
    }
}

struct CameraRollIdeaResearchView: View {
    let research: CameraRollStoryResearch
    var body: some View {
        DisclosureGroup("Background facts") {
            VStack(alignment: .leading, spacing: 12) {
                Text("Background on \(research.topic)").font(KriaFont.body(13).weight(.semibold))
                Text("Public sources, not something your footage shows.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                if !research.reviewCurrent {
                    Text("Wording or footage changed. Recheck these sources.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                }
                ForEach(research.evidence.facts) { fact in
                    Text(fact.text).font(KriaFont.body(14))
                    ForEach(fact.sources, id: \.url) { source in
                        if let url = ResearchCitation.publicURL(source.url) {
                            Link(source.title, destination: url).font(KriaFont.body(13)).frame(minHeight: 44)
                        }
                    }
                }
                if !research.evidence.searchEntryHTML.isEmpty {
                    ResearchSearchAttribution(html: research.evidence.searchEntryHTML).frame(height: 100)
                }
            }.padding(.top, 8)
        }.font(KriaFont.body(13)).accessibilityIdentifier("story-research-sources")
    }
}

struct CameraRollOpenQuestionsView: View {
    let story: CameraRollStory
    let plan: CameraRollContentPlan
    let save: @MainActor (CameraRollContentPlan) async -> Void
    @State private var drafts: [String: String] = [:]
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(plan.unresolvedQuestions.isEmpty ? "Your answers" : "Before you build this").font(KriaFont.body(13).weight(.semibold))
            ForEach(plan.openQuestions ?? []) { item in
                VStack(alignment: .leading, spacing: 8) {
                    Text(item.question).font(KriaFont.body(15).weight(.medium))
                    if item.isResolved {
                        Text(item.answer ?? "Skipped").font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                            .accessibilityIdentifier("story-question-resolved-\(item.id)")
                    } else {
                        TextField("Your answer", text: Binding(get: { drafts[item.id] ?? "" }, set: { drafts[item.id] = String($0.prefix(200)) }), axis: .vertical)
                            .accessibilityIdentifier("story-question-answer-\(item.id)")
                        let answerIsEmpty = (drafts[item.id] ?? "").trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                        HStack(spacing: 8) {
                            Button { Task { await save(plan.resolving(item.id, answer: drafts[item.id] ?? "")) } } label: { questionChip("Save answer") }
                                .disabled(answerIsEmpty).opacity(answerIsEmpty ? 0.45 : 1)
                                .accessibilityIdentifier("story-question-save-\(item.id)")
                            Button { Task { await save(plan.resolving(item.id, answer: nil)) } } label: { questionChip("Skip") }
                                .accessibilityIdentifier("story-question-skip-\(item.id)")
                        }.buttonStyle(.plain)
                    }
                }
            }
        }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
            .background(KriaColor.menu, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
    }

    private func questionChip(_ title: String) -> some View {
        Text(title).font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.ink)
            .padding(.horizontal, 14).frame(height: 36).background(Color.white, in: Capsule()).frame(minHeight: 44).contentShape(Capsule())
    }
}

private struct CameraRollWordingEditor: View {
    @State var plan: CameraRollContentPlan
    let save: @MainActor (CameraRollContentPlan) async throws -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var busy = false
    @State private var error: String?
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Text("Opening text").font(KriaFont.body(13).weight(.semibold))
                    TextField("Opening text", text: $plan.openingText, axis: .vertical).accessibilityIdentifier("story-opening-text")
                    Text("Ending text · optional").font(KriaFont.body(13).weight(.semibold))
                    TextField("Ending text", text: Binding(get: { plan.closingText ?? "" }, set: { plan.closingText = $0.isEmpty ? nil : $0 }), axis: .vertical)
                    Text("Post caption").font(KriaFont.body(13).weight(.semibold))
                    TextField("Caption", text: $plan.caption, axis: .vertical)
                    if let error { Text(error).foregroundStyle(KriaColor.failureText) }
                }.font(KriaFont.body(16)).padding(24)
            }.background(KriaColor.paper).navigationTitle("Edit wording").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(busy) }
                    ToolbarItem(placement: .confirmationAction) { Button("Save") {
                        Task { busy = true; defer { busy = false }
                            do { try await save(plan); dismiss() } catch { self.error = CameraRollCollectionFailure.message(error) }
                        }
                    }.disabled(busy || plan.openingText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || plan.openingText.count > 100 || (plan.closingText?.count ?? 0) > 100 || plan.caption.count > 300).accessibilityIdentifier("story-save-wording") }
                }.kriaPage()
        }.interactiveDismissDisabled(busy)
    }
}

struct CollectionOriginalsReview: View {
    let assets: [SavedMediaAsset]
    let owner: String
    let onReady: @MainActor () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var selection: [PhotosPickerItem] = []
    @State private var linked = Set<UUID>()
    @State private var busy = false
    @State private var error: String?
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    Text("Use the full-quality footage").font(KriaFont.headline(26))
                    Text("Choose the matching originals from Photos. They stay on your iPhone.").font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                    ForEach(assets) { asset in
                        HStack { Text(asset.displayName).font(KriaFont.body(14)); Spacer(); Image(systemName: linked.contains(asset.id) ? "checkmark.circle.fill" : "video").foregroundStyle(KriaColor.mutedInk) }
                    }
                    if busy { ProgressView("Checking originals…") }
                    if let error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText) }
                    PhotosPicker(selection: $selection, maxSelectionCount: max(1, assets.count), matching: .videos, preferredItemEncoding: .current) {
                        Text("Choose originals").frame(maxWidth: .infinity)
                    }.buttonStyle(KriaSecondaryButtonStyle()).disabled(busy)
                    Button("Continue") { onReady() }.buttonStyle(KriaPrimaryButtonStyle()).disabled(busy || linked.count != assets.count)
                }.padding(24)
            }.background(KriaColor.paper).navigationTitle("Original footage").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(busy) } }
                .onChange(of: selection) { _, chosen in
                    Task {
                        busy = true; error = nil; defer { busy = false; selection = [] }
                        for item in chosen {
                            do {
                                let file = try await PhotoItemFileLoader().load(item: item, identifier: item.itemIdentifier ?? "picker")
                                defer { try? FileManager.default.removeItem(at: file) }
                                linked.formUnion(try await CollectionOriginalRelinker.link(file: file, assets: assets, owner: owner))
                            } catch { self.error = "Choose the original, unmodified video. This selection didn't match the saved footage." }
                        }
                    }
                }.kriaPage()
        }.interactiveDismissDisabled(busy)
    }
}
