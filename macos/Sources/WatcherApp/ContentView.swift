import SwiftUI
import WatcherKit

struct ContentView: View {
    @EnvironmentObject var store: WatcherStore
    @State private var editing: WatcherItem?
    @State private var adding = false

    var body: some View {
        VStack(spacing: 0) {
            if let update = store.update { UpdateBanner(update: update) }
            switch store.phase {
            case .starting:
                ProgressView("Starting Watcher core…").frame(maxWidth: .infinity, maxHeight: .infinity)
            case .failed(let message):
                VStack(spacing: 12) {
                    Image(systemName: "exclamationmark.triangle").font(.largeTitle)
                    Text(message).multilineTextAlignment(.center)
                    Button("Retry") { store.retry() }
                }
                .padding().frame(maxWidth: .infinity, maxHeight: .infinity)
            case .running:
                watcherList
            }
        }
        .frame(minWidth: 460, minHeight: 320)
        .toolbar {
            ToolbarItem {
                Picker("Mode", selection: Binding(get: { store.settings.mode }, set: { store.setMode($0) })) {
                    ForEach(store.settings.modes, id: \.self) { Text($0).tag($0) }
                }
                .pickerStyle(.menu)
                .help("Where notifications go: this Mac, or your phone via ntfy")
            }
            ToolbarItem { Button { store.pollNow() } label: { Label("Check now", systemImage: "arrow.clockwise") } }
            ToolbarItem { Button { adding = true } label: { Label("Add", systemImage: "plus") } }
        }
        .sheet(isPresented: $adding) { WatcherForm(existing: nil) }
        .sheet(item: $editing) { WatcherForm(existing: $0) }
        .alert("Something went wrong", isPresented: Binding(
            get: { store.errorMessage != nil }, set: { if !$0 { store.errorMessage = nil } })
        ) { Button("OK") {} } message: { Text(store.errorMessage ?? "") }
    }

    @ViewBuilder private var watcherList: some View {
        if store.watchers.isEmpty {
            Text("Nothing watched yet. Click + to add a Jenkins job, PR or Actions run.")
                .foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            List(store.watchers) { item in
                WatcherRow(item: item)
                    .contextMenu {
                        if item.unacknowledged { Button("Acknowledge") { store.acknowledge(item) } }
                        if let url = item.url.flatMap(URL.init(string:)) {
                            Button("Open in Browser") { NSWorkspace.shared.open(url) }
                        }
                        Button("Edit…") { editing = item }
                        Divider()
                        Button("Remove", role: .destructive) { store.remove(item) }
                    }
                    .onTapGesture(count: 2) { editing = item }
            }
        }
    }
}

struct WatcherRow: View {
    @EnvironmentObject var store: WatcherStore
    let item: WatcherItem

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon).foregroundStyle(color).font(.title3).frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.label).font(.headline)
                Text(item.detail.isEmpty ? item.status.rawValue.capitalized : item.detail)
                    .font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                if !item.notes.isEmpty { Text(item.notes).font(.caption).foregroundStyle(.tertiary) }
                if let checked = item.lastChecked {
                    Text("Checked \(Date(timeIntervalSince1970: checked), style: .relative) ago")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
            }
            Spacer()
            if item.unacknowledged { Button("Got it") { store.acknowledge(item) } }
        }
        .padding(.vertical, 3)
    }

    private var icon: String {
        switch item.status {
        case .success: return "checkmark.circle.fill"
        case .failure: return "xmark.octagon.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .building: return "hammer.fill"
        case .idle: return "pause.circle"
        case .unknown: return "questionmark.circle"
        }
    }

    private var color: Color {
        switch item.status {
        case .success: return .green
        case .failure: return .red
        case .error: return .orange
        case .building: return .blue
        case .idle, .unknown: return .secondary
        }
    }
}

struct UpdateBanner: View {
    @EnvironmentObject var store: WatcherStore
    let update: UpdateInfo

    var body: some View {
        HStack {
            Text("Update available: \(update.name)")
            Spacer()
            if let url = URL(string: update.url) { Button("Download") { NSWorkspace.shared.open(url) } }
            Button("Dismiss") { store.dismissUpdate() }
        }
        .padding(8).background(.yellow.opacity(0.25))
    }
}
