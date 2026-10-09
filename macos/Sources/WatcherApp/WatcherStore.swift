import AppKit
import SwiftUI
import WatcherKit

@MainActor
final class WatcherStore: ObservableObject {
    enum Phase: Equatable {
        case starting
        case running
        case failed(String)
    }

    @Published private(set) var phase: Phase = .starting
    @Published private(set) var watchers: [WatcherItem] = []
    @Published private(set) var settings = Settings()
    @Published private(set) var update: UpdateInfo?
    @Published var errorMessage: String?

    private let core = CoreProcess()
    private var client: APIClient?
    private var eventTask: Task<Void, Never>?

    var unacknowledgedCount: Int { watchers.filter(\.unacknowledged).count }

    func start() {
        guard eventTask == nil else { return }
        phase = .starting
        eventTask = Task { [weak self] in
            guard let self else { return }
            do {
                let handshake = try await core.start()
                let client = APIClient(port: handshake.port, token: handshake.token)
                self.client = client
                for try await event in client.events() { apply(event) }
                if !Task.isCancelled { phase = .failed("The Watcher core stopped unexpectedly.") }
            } catch {
                if !Task.isCancelled { phase = .failed(error.localizedDescription) }
            }
            eventTask = nil
        }
    }

    func retry() {
        core.stop()
        eventTask?.cancel()
        eventTask = nil
        start()
    }

    func shutdown() {
        eventTask?.cancel()
        core.stop()
    }

    private func apply(_ event: EngineEvent) {
        switch event {
        case .state(let state):
            watchers = state.watchers
            settings = state.settings
            update = state.update
            phase = .running
        case .watcherAdded(let item):
            if let index = watchers.firstIndex(where: { $0.id == item.id }) {
                watchers[index] = item
            } else {
                watchers.append(item)
            }
        case .watcherUpdated(let item):
            if let index = watchers.firstIndex(where: { $0.id == item.id }) { watchers[index] = item }
        case .watcherRemoved(let id):
            watchers.removeAll { $0.id == id }
        case .settingsChanged(let new):
            settings = new
        case .updateAvailable(let info):
            update = info
        case .notification:
            NSApp.requestUserAttention(.criticalRequest)
        }
        applyWindowLevel()
        NSApp.dockTile.badgeLabel = unacknowledgedCount > 0 ? "\(unacknowledgedCount)" : nil
    }

    private func applyWindowLevel() {
        let level: NSWindow.Level = settings.alwaysOnTop ? .floating : .normal
        for window in NSApp.windows where window.canBecomeMain { window.level = level }
    }

    func setSettings(_ patch: [String: Any]) { run { try await $0.updateSettings(patch) } }

    private func run(_ operation: @escaping (APIClient) async throws -> Void) {
        guard let client else { return }
        Task {
            do { try await operation(client) } catch { errorMessage = error.localizedDescription }
        }
    }

    /// Runs an operation whose failure the calling sheet wants to show inline.
    func perform(_ operation: (APIClient) async throws -> Void) async -> String? {
        guard let client else { return "Not connected to the Watcher core." }
        do {
            try await operation(client)
            return nil
        } catch { return error.localizedDescription }
    }

    func acknowledge(_ item: WatcherItem) { run { try await $0.acknowledge(id: item.id) } }
    func remove(_ item: WatcherItem) { run { try await $0.removeWatcher(id: item.id) } }
    func pollNow() { run { try await $0.pollNow() } }
    func dismissUpdate() {
        guard let version = update?.version else { return }
        update = nil
        run { try await $0.dismissUpdate(version: version) }
    }
    func setMode(_ mode: String) { run { try await $0.updateSettings(["mode": mode]) } }
}
