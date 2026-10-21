import SwiftUI
import AVKit
import PhotosUI

struct CameraRollStoriesView: View {
    let api: any KriaAPIClient
    let collection: CameraRollCollection
    let result: CameraRollStories
    var cacheOnly = false
    let onSelected: @MainActor (CreationThread) async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var stories: [CameraRollStory] = []
    @State private var busy = false
    @State private var error: String?
    @State private var inspection: String?
    @State private var question = "What is clearly visible in this interval?"
    @State private var player: AVPlayer?
    @State private var showsPreview = false
    @State private var selectionTask: Task<Void, Never>?
    @State private var editingWording: CameraRollStory?
    @State private var missingOriginals: [SavedMediaAsset] = []
    @State private var relinkingStory: CameraRollStory?
    @State private var taste: EditorialTasteProfile?
    @State private var showsTaste = false
    @State private var ratingStory: CameraRollStory?
    @State private var feedbackSaved = Set<String>()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                Text("Find an idea worth making.")
                    .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                if !result.coverage.incompleteSpeech.isEmpty || !result.coverage.unindexed.isEmpty {
                    Label("Some footage needs checking", systemImage: "info.circle")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                }
                if stories.allSatisfy(\.rejected) { ContentUnavailableView("No story to suggest", systemImage: "photo.stack", description: Text("Try a smaller collection or a more specific story. You can also create a video from your chosen footage in chat.")) }
                ForEach(stories.filter { !$0.rejected }) { story in
                    VStack(alignment: .leading, spacing: 16) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(story.title).font(KriaFont.display(26))
                            Spacer()
                            Text("\(story.duration, specifier: "%.1f") sec").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        }
                        if let plan = story.contentPlan {
                            Text(plan.angle).font(KriaFont.body(16).weight(.semibold))
                            VStack(alignment: .leading, spacing: 6) {
                                Text("Why it could work").font(KriaFont.body(13).weight(.semibold))
                                Text(plan.whyItWorks).font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                                Text("For \(plan.audience)").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                            }
                            VStack(alignment: .leading, spacing: 10) {
                                HStack {
                                    Text("Proposed on-screen text").font(KriaFont.body(13).weight(.semibold))
                                    Spacer()
                                    Button("Edit") { editingWording = story }.font(KriaFont.body(13)).accessibilityIdentifier("story-edit-wording")
                                }
                                Text("“\(plan.openingText)”").font(KriaFont.body(16).weight(.medium))
                                if let closing = plan.closingText { Text("Then: “\(closing)”").font(KriaFont.body(14)) }
                            }.padding(16).frame(maxWidth: .infinity, alignment: .leading)
                                .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 16))
                        }
                        if taste?.enabled == true && taste?.memoryPaused == false {
                            Button(feedbackSaved.contains("\(story.id)-\(story.revision)") ? "Feedback saved" : "Would you post this?") { ratingStory = story }
                                .font(KriaFont.body(13)).frame(minHeight: 44)
                                .accessibilityIdentifier("idea-feedback")
                        }
                        if let research = story.research { CameraRollIdeaResearchView(research: research) }
                        DisclosureGroup("Story arc") {
                            VStack(alignment: .leading, spacing: 10) {
                                Text(story.rationale).font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                                Text(story.hook).font(KriaFont.body(14))
                                Text(story.payoff).font(KriaFont.body(14))
                            }.padding(.top, 8)
                        }.font(KriaFont.body(13))
                        Toggle("Original sound", isOn: Binding(
                            get: { story.audioPolicy != "muted" },
                            set: { keep in Task { await change(story, order: nil, audioPolicy: keep ? "preserve_source_audio" : "muted", action: keep ? "sound-original" : "sound-muted") } }
                        )).accessibilityIdentifier("story-sound")
                        if story.audioPolicy == "muted" { Text("Muted footage").font(.footnote).foregroundStyle(.secondary) }
                        ForEach(Array(story.moments.enumerated()), id: \.element.id) { index, moment in
                            VStack(alignment: .leading, spacing: 8) {
                                HStack(alignment: .top, spacing: 12) {
                                    if let entry = collection.entries.first(where: { $0.savedAsset?.id == moment.assetID }) {
                                        CameraRollCollectionThumbnail(entry: entry, api: api, size: CGSize(width: 72, height: 88))
                                            .clipShape(RoundedRectangle(cornerRadius: 10))
                                    }
                                    VStack(alignment: .leading, spacing: 6) {
                                        Text(moment.text).font(KriaFont.body(14).weight(.medium)).lineLimit(3)
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
                                        ForEach(result.coverage.moments.filter { alternative in
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
                        Button { selectionTask = Task { await select(story) } } label: {
                            Text(story.contentPlan == nil ? "Choose this story" : "Build this project").frame(maxWidth: .infinity)
                        }.buttonStyle(KriaPrimaryButtonStyle()).accessibilityIdentifier("story-choose-\(story.id)")
                        Button("Not this story", role: .destructive) { Task { await reject(story) } }
                    }.padding(20).background(KriaColor.paper, in: RoundedRectangle(cornerRadius: 22))
                        .overlay(RoundedRectangle(cornerRadius: 22).stroke(KriaColor.line.opacity(0.6), lineWidth: 1)).disabled(busy)
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
            .onAppear { stories = result.stories }
            .task {
                taste = try? await api.editorialTaste()
                for story in result.stories {
                    try? await api.recordCameraRollStoryExposure(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0)
                }
            }
            .onDisappear { selectionTask?.cancel(); player?.pause() }
            .interactiveDismissDisabled(busy)
            .sheet(isPresented: $showsTaste) {
                EditorialTasteView(api: api) { value in taste = value }
            }
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
            .sheet(item: $relinkingStory) { story in
                if let owner = api.mediaLibraryOwner {
                    CollectionOriginalsReview(assets: missingOriginals, owner: owner) {
                        relinkingStory = nil
                        selectionTask = Task { await select(story) }
                    }
                }
            }
        }
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
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func replace(_ story: CameraRollStory, index: Int, moment: CameraRollMoment) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let changed = try await api.correctCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, index: index, momentID: moment.id, eventID: "replace-\(story.id)-\(story.revision)-\(index)-\(moment.id)")
            stories = stories.map { $0.id == changed.id ? changed : $0 }
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func reject(_ story: CameraRollStory) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let changed = try await api.rejectCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, eventID: "reject-\(story.id)-\(story.revision)")
            stories = stories.map { $0.id == changed.id ? changed : $0 }
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func inspect(_ moment: CameraRollMoment) async {
        busy = true; error = nil; defer { busy = false }
        do {
            let result = try await api.inspectCameraRollMoment(collectionID: collection.id, revision: collection.serverRevision ?? 0, momentID: moment.id, question: question.isEmpty ? "What is visible?" : question, cacheOnly: cacheOnly)
            inspection = result.answer.isEmpty ? "The check could not answer confidently." : "\(result.answer) — \(result.evidence) (AI check; review the footage.)"
        } catch { self.error = error.localizedDescription }
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
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func select(_ story: CameraRollStory) async {
        busy = true; error = nil; defer { busy = false }
        do {
            guard let owner = api.mediaLibraryOwner else { throw APIError.unsupported }
            let assets = collection.entries.compactMap(\.savedAsset).filter { asset in story.moments.contains { $0.assetID == asset.id } }
            var missing: [SavedMediaAsset] = []
            for asset in assets where asset.kind == "video" {
                if try await SavedMediaReuse.adoptOriginal(asset: asset, owner: owner, api: api) == nil { missing.append(asset) }
            }
            if !missing.isEmpty { missingOriginals = missing; relinkingStory = story; return }
            var thread = try await api.createCameraRollStoryThread(id: story.id, revision: story.revision)
            guard let threadID = UUID(uuidString: thread.id), thread.runtimeVersion == 2 else { throw APIError.invalidResponse }
            var used = Set<UUID>()
            for moment in story.moments where used.insert(moment.assetID).inserted {
                try Task.checkCancellation()
                guard let asset = collection.entries.compactMap(\.savedAsset).first(where: { $0.id == moment.assetID }), let source = asset.sources.first(where: { $0.id == moment.sourceID && $0.generation == moment.generation }) else { throw APIError.invalidResponse }
                let mediaID = (source.role == "analysis_proxy" ? "analysis-proxy-" : "") + asset.id.uuidString
                thread = try await SavedMediaReuse.attach(asset: asset, source: source, threadID: threadID, itemID: thread.activePlanItemID, role: .clip, mediaID: mediaID, owner: owner, api: api)
            }
            _ = try await api.selectCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0, threadID: threadID, threadRevision: thread.revision, eventID: "choose-\(story.id)-\(story.revision)")
            let current = try await api.project(threadID: threadID)
            await onSelected(current)
        } catch is CancellationError { self.error = "Paused. The draft chat is saved; choose this story again to continue." }
        catch { self.error = error.localizedDescription }
    }
}

private struct CameraRollIdeaResearchView: View {
    let research: CameraRollStoryResearch
    var body: some View {
        DisclosureGroup("Research behind the idea") {
            VStack(alignment: .leading, spacing: 12) {
                Text("About \(research.topic)").font(KriaFont.body(13).weight(.semibold))
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
                            do { try await save(plan); dismiss() } catch { self.error = error.localizedDescription }
                        }
                    }.disabled(busy || plan.openingText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || plan.openingText.count > 100 || (plan.closingText?.count ?? 0) > 100 || plan.caption.count > 300).accessibilityIdentifier("story-save-wording") }
                }.kriaPage()
        }.interactiveDismissDisabled(busy)
    }
}

private struct CollectionOriginalsReview: View {
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
                    Text("Use the full-quality footage").font(KriaFont.display(26))
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
