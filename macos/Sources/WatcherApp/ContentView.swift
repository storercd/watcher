import SwiftUI
import WatcherKit

enum WatchTab: String, CaseIterable, Identifiable {
    case immediate = "Immediate"
    case longTerm = "Long Term"
    var id: String { rawValue }
}

struct ContentView: View {
    @EnvironmentObject var store: WatcherStore
    @State private var editing: WatcherItem?
    @State private var adding = false
    @State private var tab: WatchTab = .immediate

    private func items(in tab: WatchTab) -> [WatcherItem] {
        store.watchers.filter { $0.isLongTerm == (tab == .longTerm) }
    }

    private func tabTitle(_ tab: WatchTab) -> String {
        let waiting = items(in: tab).filter(\.unacknowledged).count
        return waiting > 0 ? "\(tab.rawValue) (\(waiting))" : tab.rawValue
    }

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
                Picker("View", selection: $tab) {
                    ForEach(WatchTab.allCases) { Text(tabTitle($0)).tag($0) }
                }
                .pickerStyle(.segmented).labelsHidden()
                .padding(.horizontal, 10).padding(.vertical, 6)
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
            ToolbarItem {
                Button { store.pollNow() } label: { Label("Check now", systemImage: "arrow.clockwise") }
                    .help("Check all watchers now, instead of waiting for the next poll")
            }
            ToolbarItem {
                Button { adding = true } label: { Label("Add", systemImage: "plus") }
                    .help(tab == .longTerm
                        ? "Add a GitHub PR or issue to watch over hours or days"
                        : "Add a Jenkins job, GitHub PR or Actions run to watch")
            }
        }
        .sheet(isPresented: $adding) { WatcherForm(existing: nil, defaultLongTerm: tab == .longTerm) }
        .sheet(item: $editing) { WatcherForm(existing: $0, defaultLongTerm: false) }
        .alert("Something went wrong", isPresented: Binding(
            get: { store.errorMessage != nil }, set: { if !$0 { store.errorMessage = nil } })
        ) { Button("OK") {} } message: { Text(store.errorMessage ?? "") }
    }

    @ViewBuilder private func highlight(for item: WatcherItem) -> some View {
        if item.unacknowledged {
            switch item.status {
            case .success: Color.green.opacity(0.28)
            case .failure: Color.red.opacity(0.28)
            case .approvalRequired: Color.yellow.opacity(0.28)
            default: Color.orange.opacity(0.28)
            }
        }
    }

    private func openableURL(_ item: WatcherItem) -> URL? {
        guard let text = item.url, !ProcessTarget.isProcessURL(text) else { return nil }
        return URL(string: text)
    }

    private func open(_ item: WatcherItem) {
        if let target = item.url.flatMap({ AppTarget(url: $0) }) {
            NSRunningApplication.runningApplications(withBundleIdentifier: target.bundleID).first?.activate()
        } else if let url = openableURL(item) {
            NSWorkspace.shared.open(url)
        }
    }

    @ViewBuilder private var watcherList: some View {
        let shown = items(in: tab)
        if shown.isEmpty {
            Text(tab == .longTerm
                ? "Nothing on the long-term list. Click + to watch a PR or issue for a merge, close or any activity."
                : "Nothing watched yet. Click + to add a Jenkins job, PR, Actions run or running app.")
                .foregroundStyle(.secondary).multilineTextAlignment(.center).padding()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            List(shown) { item in
                WatcherRow(item: item)
                    .contentShape(Rectangle())
                    .onTapGesture { open(item) }
                    .listRowInsets(EdgeInsets())
                    .listRowBackground(highlight(for: item))
                    .contextMenu {
                        if item.unacknowledged { Button("Acknowledge") { store.acknowledge(item) } }
                        if openableURL(item) != nil {
                            Button(item.url.flatMap { AppTarget(url: $0) } != nil ? "Switch to App" : "Open in Browser") {
                                open(item)
                            }
                        }
                        Button("Edit…") { editing = item }
                        Divider()
                        Button("Remove", role: .destructive) { store.remove(item) }
                    }
            }
        }
    }
}

struct WatcherRow: View {
    @EnvironmentObject var store: WatcherStore
    let item: WatcherItem

    private func font(_ size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .system(size: size * store.settings.fontScale, weight: weight)
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon).foregroundStyle(color).font(font(15)).frame(width: 22 * store.settings.fontScale)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    if let doneTag { doneBadge(doneTag) }
                    Text(item.label).font(font(13, weight: .semibold))
                }
                Text(shownDetail)
                    .font(font(11)).foregroundStyle(.secondary).lineLimit(2)
                if item.isLongTerm, let summary = longTermSummary {
                    Text(summary).font(font(10)).foregroundStyle(.secondary)
                }
                if !item.notes.isEmpty { Text(item.notes).font(font(10)).foregroundStyle(.tertiary) }
                if let checked = item.lastChecked {
                    Text("Checked \(Date(timeIntervalSince1970: checked), style: .relative) ago")
                        .font(font(10)).foregroundStyle(.tertiary)
                }
            }
            Spacer()
            if item.unacknowledged {
                Button("Got it") { store.acknowledge(item) }
                    .help("Mark this result as seen")
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 10)
        .help(item.url ?? "")
    }

    private var longTermSummary: String? {
        guard let trigger = item.trigger.flatMap(LongTermTrigger.init(rawValue:)) else { return nil }
        guard let seconds = item.pollInterval else { return "Alerts on: \(trigger.title)" }
        return "Alerts on: \(trigger.title) · checks every \(formatInterval(seconds))"
    }

    // Watchers append " - merged" / " - archived" to the detail when the work is finished.
    private static let doneTags = ["merged", "archived"]

    private var doneTag: String? {
        Self.doneTags.first { item.detail.lowercased().hasSuffix(" - \($0)") }
    }

    private var shownDetail: String {
        guard let tag = doneTag else {
            return item.detail.isEmpty ? item.status.rawValue.capitalized : item.detail
        }
        let base = String(item.detail.dropLast(tag.count + 3))
        return base.isEmpty ? item.status.rawValue.capitalized : base
    }

    private func doneBadge(_ tag: String) -> some View {
        Text(tag.uppercased())
            .font(font(9, weight: .bold))
            .padding(.horizontal, 6).padding(.vertical, 1)
            .background(Capsule().fill(tag == "merged" ? Color.purple : Color.gray))
            .foregroundStyle(.white)
    }

    private var icon: String {
        switch item.status {
        case .success: return "checkmark.circle.fill"
        case .failure: return "xmark.octagon.fill"
        case .error: return "exclamationmark.triangle.fill"
        case .approvalRequired: return "hand.raised.fill"
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
        case .approvalRequired: return .yellow
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

func formatInterval(_ seconds: Double) -> String {
    if seconds >= 86400, seconds.truncatingRemainder(dividingBy: 86400) == 0 { return "\(Int(seconds / 86400)) d" }
    if seconds >= 3600, seconds.truncatingRemainder(dividingBy: 3600) == 0 { return "\(Int(seconds / 3600)) h" }
    return "\(Int((seconds / 60).rounded())) min"
}
