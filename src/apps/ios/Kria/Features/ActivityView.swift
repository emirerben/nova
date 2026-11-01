import SwiftUI

struct ActivityView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var notifications: NativeNotificationCoordinator
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var items: [NativeActivity] = []
    @State private var nextBefore: String?
    @State private var isLoading = false
    @State private var opening: UUID?
    @State private var message: String?

    var body: some View {
        List {
            if notifications.available {
                Button(notifications.needsRevocation ? "Retry notification cleanup" : notifications.optedIn ? "Turn off notifications" : "Notify me about my videos") {
                    Task {
                        if notifications.optedIn || notifications.needsRevocation { await notifications.disable() }
                        else { await notifications.enable() }
                    }
                }.disabled(notifications.isRequestingPermission)
            }
            if let status = notifications.message { Text(status).foregroundStyle(KriaColor.zinc) }

            if let message { Text(message).foregroundStyle(KriaColor.zinc).accessibilityIdentifier("activity-error") }
            if items.isEmpty && !isLoading && message == nil {
                ContentUnavailableView("No activity yet", systemImage: "bell", description: Text("Finished videos and projects that need your attention appear here."))
            }
            ForEach(items) { item in
                Button { Task { await open(item) } } label: {
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: item.kind == .videoReady ? "checkmark.circle" : "exclamationmark.circle")
                            .foregroundStyle(KriaColor.ink)
                        VStack(alignment: .leading, spacing: 6) {
                            Text(item.title).font(KriaFont.body(16).weight(item.readAt == nil ? .semibold : .regular))
                            Text(item.detail).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                            Text(item.createdAt, style: .relative).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                        }
                        Spacer(minLength: 0)
                        if opening == item.id { ProgressView() }
                    }.padding(.vertical, 8).contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(opening != nil)
                .accessibilityIdentifier("activity-item-\(item.id)")
            }
            if nextBefore != nil { Button("Load more") { Task { await load(more: true) } }.disabled(isLoading) }
            if isLoading { ProgressView().frame(maxWidth: .infinity) }
        }
        .scrollContentBackground(.hidden)
        .background(KriaColor.paper)
        .navigationTitle("Activity")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        .task { await load() }
        .refreshable { await load() }
        .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await load() } } }
        .accessibilityIdentifier("activity-inbox")
    }

    @MainActor private func load(more: Bool = false) async {
        guard !isLoading else { return }
        let owner = model.api.mediaLibraryOwner
        isLoading = true
        defer { isLoading = false }
        do {
            let page = try await model.api.nativeActivities(before: more ? nextBefore : nil)
            guard owner == model.api.mediaLibraryOwner, !Task.isCancelled else { return }
            items = more ? items + page.items.filter { incoming in !items.contains { $0.id == incoming.id } } : page.items
            nextBefore = page.nextBefore
            message = nil
        } catch {
            guard owner == model.api.mediaLibraryOwner else { return }
            message = String(localized: "Activity couldn't load. Pull down to try again.")
        }
    }

    @MainActor private func open(_ item: NativeActivity) async {
        guard opening == nil else { return }
        let owner = model.api.mediaLibraryOwner
        opening = item.id
        defer { opening = nil }
        do {
            guard try await model.openNativeActivity(id: item.id, expectedOwner: owner) else { return }
            dismiss()
        } catch {
            guard owner == model.api.mediaLibraryOwner else { return }
            message = String(localized: "This activity is no longer available. Refresh Activity to see the latest status.")
        }
    }
}
