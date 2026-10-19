import SwiftUI
import AVKit

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

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Text("Choose a story to prepare its draft. Rendering needs a separate approval in the chat.")
                    Text("Source windows · No added text or music").font(.footnote).foregroundStyle(.secondary)
                    Text("Story ideas currently use videos with original iPhone bindings.").font(.footnote).foregroundStyle(.secondary)
                    if !result.coverage.incompleteSpeech.isEmpty || !result.coverage.unindexed.isEmpty {
                        Text("Some speech or footage is not fully indexed. Review the source intervals before choosing.")
                            .font(.footnote).foregroundStyle(KriaColor.failureText)
                    }
                }
                if stories.allSatisfy(\.rejected) { ContentUnavailableView("No story to suggest", systemImage: "photo.stack", description: Text("Try a smaller collection or a more specific story. You can also create a video from your chosen footage in chat.")) }
                ForEach(stories.filter { !$0.rejected }) { story in
                    Section(story.title) {
                        Text(story.rationale)
                        LabeledContent("Hook", value: story.hook)
                        LabeledContent("Payoff", value: story.payoff)
                        Text("\(story.duration, specifier: "%.1f") seconds").font(.footnote)
                        Toggle("Original sound", isOn: Binding(
                            get: { story.audioPolicy != "muted" },
                            set: { keep in Task { await change(story, order: nil, audioPolicy: keep ? "preserve_source_audio" : "muted", action: keep ? "sound-original" : "sound-muted") } }
                        )).accessibilityIdentifier("story-sound")
                        if story.audioPolicy == "muted" { Text("Muted footage").font(.footnote).foregroundStyle(.secondary) }
                        ForEach(Array(story.moments.enumerated()), id: \.element.id) { index, moment in
                            VStack(alignment: .leading, spacing: 8) {
                                Text("\(index + 1). \(sourceLabel(moment)) · \(moment.start, specifier: "%.1f")–\(moment.end, specifier: "%.1f")s").font(.subheadline.weight(.medium))
                                Text(moment.text).font(.subheadline)
                                if moment.uncertain { Text("AI evidence — review the footage").font(.caption).foregroundStyle(.secondary) }
                                HStack {
                                    Button("Preview interval") { Task { await preview(moment) } }
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
                                }.buttonStyle(.bordered)
                                Button("Check this interval") { Task { await inspect(moment) } }.font(.footnote)
                            }.padding(.vertical, 4)
                        }
                        Button("Choose this story") {
                            selectionTask = Task { await select(story) }
                        }.buttonStyle(.borderedProminent).accessibilityIdentifier("story-choose-\(story.id)")
                        Button("Not this story", role: .destructive) { Task { await reject(story) } }
                    }.disabled(busy)
                }
                Section("Targeted check") {
                    TextField("Question about an interval", text: $question)
                    Text("Checks use a short analysis copy, up to three per collection revision. AI answers can still be wrong.").font(.footnote).foregroundStyle(.secondary)
                    if let inspection { Text(inspection).textSelection(.enabled) }
                }
                if busy { ProgressView("Preparing…") }
                if let error { Text(error).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("story-error") }
            }
            .navigationTitle("Story ideas").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.disabled(busy) } }
            .onAppear { stories = result.stories }
            .task {
                for story in result.stories {
                    try? await api.recordCameraRollStoryExposure(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0)
                }
            }
            .onDisappear { selectionTask?.cancel(); player?.pause() }
            .interactiveDismissDisabled(busy)
            .sheet(isPresented: $showsPreview, onDismiss: { player?.pause(); player = nil }) {
                if let player { VideoPlayer(player: player).ignoresSafeArea(edges: .bottom) }
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
