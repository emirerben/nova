import SwiftUI

/// The row at the top of Settings that opens the guide.
struct WhatsNewEntryLabel: View {
    var body: some View {
        HStack(spacing: 14) {
            Image(systemName: "sparkles")
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(KriaColor.ink)
                .frame(width: 40, height: 40)
                .background(KriaColor.butter, in: Circle())
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text("What’s New")
                    .font(KriaFont.body(17).weight(.semibold))
                    .foregroundStyle(KriaColor.ink)
                Text("See what you can do in Kria now")
                    .font(KriaFont.body(13))
                    .foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Image(systemName: "chevron.right")
                .font(.system(size: 13))
                .foregroundStyle(KriaColor.mutedInk)
                .accessibilityHidden(true)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, minHeight: 64, alignment: .leading)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .contentShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .accessibilityElement(children: .combine)
    }
}

struct WhatsNewView: View {
    let api: any KriaAPIClient
    var releases: [WhatsNewRelease] = WhatsNewReleases.all

    @State private var availability: WhatsNewAvailability?
    @State private var destination: WhatsNewDestination?
    @State private var showsPrevious = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var visible: [WhatsNewRelease] { availability?.visible(releases) ?? [] }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Text("New things you can do in Kria, newest first.")
                    .font(KriaFont.body(15))
                    .foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                if availability == nil {
                    ProgressView().frame(maxWidth: .infinity, minHeight: 44).accessibilityIdentifier("whats-new-loading")
                } else if let latest = visible.first {
                    VStack(alignment: .leading, spacing: 14) {
                        KriaSectionLabel(title: "Latest")
                        WhatsNewReleaseView(release: latest, open: { destination = $0 })
                    }
                    let earlier = Array(visible.dropFirst())
                    if !earlier.isEmpty { previous(earlier) }
                } else {
                    Text("No new updates right now.")
                        .font(KriaFont.body(17).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(16)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                        .accessibilityIdentifier("whats-new-empty")
                }
            }
            .padding(24)
            .frame(maxWidth: 560, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(KriaColor.paper)
        .navigationTitle("What’s New")
        .navigationBarTitleDisplayMode(.inline)
        .navigationDestination(item: $destination) { target in
            switch target {
            case .textPreferences: TextPreferencesView(api: api)
            }
        }
        .task {
            // Popping back from a pushed screen restarts this task; keep what the first read found.
            guard availability == nil else { return }
            await loadAvailability()
        }
    }

    private func previous(_ earlier: [WhatsNewRelease]) -> some View {
        DisclosureGroup(isExpanded: $showsPrevious) {
            VStack(alignment: .leading, spacing: 28) {
                ForEach(earlier) { release in
                    WhatsNewReleaseView(release: release, open: { destination = $0 })
                }
            }
            .padding(.top, 12)
        } label: {
            Text("Previous updates")
                .font(KriaFont.body(17).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .accessibilityIdentifier("whats-new-previous")
        }
        .tint(KriaColor.ink)
        .transaction { if KriaMotion.isReduced(reduceMotion) { $0.animation = nil } }
    }

    /// Both reads can fail or be slow; a failure leaves its gate closed rather than blocking the guide.
    private func loadAvailability() async {
        async let capabilities = try? await api.creationCapabilities()
        async let memory = try? await api.textPreferenceMemory()
        let result = await WhatsNewAvailability(capabilities: capabilities, memory: memory)
        guard !Task.isCancelled else { return }
        availability = result
    }
}

private struct WhatsNewReleaseView: View {
    let release: WhatsNewRelease
    let open: (WhatsNewDestination) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(release.day.longDate())
                .font(KriaFont.body(22).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("whats-new-date-\(release.id)")
            ForEach(release.features) { WhatsNewFeatureCard(feature: $0, open: open) }
            if !release.fixes.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    KriaSectionLabel(title: "Also improved")
                    ForEach(Array(release.fixes.enumerated()), id: \.offset) { _, fix in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text("•").font(KriaFont.body(15)).foregroundStyle(KriaColor.zinc).accessibilityHidden(true)
                            Text(fix.text)
                                .font(KriaFont.body(15))
                                .foregroundStyle(KriaColor.ink)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .accessibilityElement(children: .contain)
            }
        }
    }
}

private struct WhatsNewFeatureCard: View {
    let feature: WhatsNewFeature
    let open: (WhatsNewDestination) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: feature.symbol)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(KriaColor.ink)
                    .frame(width: 36, height: 36)
                    .background(KriaColor.butter, in: Circle())
                    .accessibilityHidden(true)
                Text(feature.title)
                    .font(KriaFont.body(17).weight(.semibold))
                    .foregroundStyle(KriaColor.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
            }
            Text(feature.benefit)
                .font(KriaFont.body(15))
                .foregroundStyle(KriaColor.ink)
                .fixedSize(horizontal: false, vertical: true)
            if !feature.steps.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(feature.steps.enumerated()), id: \.offset) { index, step in
                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                            Text("\(index + 1).")
                                .font(KriaFont.body(15).weight(.semibold))
                                .foregroundStyle(KriaColor.ink)
                                .frame(minWidth: 22, alignment: .leading)
                            Text(step)
                                .font(KriaFont.body(15))
                                .foregroundStyle(KriaColor.ink)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            if let destination = feature.tryIt {
                Button { open(destination) } label: {
                    Text(destination.actionTitle).frame(maxWidth: .infinity)
                }
                .buttonStyle(KriaPrimaryButtonStyle())
                .accessibilityIdentifier("whats-new-try-\(feature.id)")
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("whats-new-card-\(feature.id)")
    }
}
