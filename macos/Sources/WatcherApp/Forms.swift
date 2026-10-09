import AppKit
import SwiftUI
import WatcherKit

struct WatcherForm: View {
    @EnvironmentObject var store: WatcherStore
    @Environment(\.dismiss) private var dismiss
    let existing: WatcherItem?

    @State private var url = ""
    @State private var label = ""
    @State private var notes = ""
    @State private var error: String?
    @State private var busy = false

    var body: some View {
        Form {
            TextField("URL", text: $url, prompt: Text("Jenkins job, GitHub PR or Actions run URL"))
            TextField("Label", text: $label, prompt: Text("Optional"))
            TextField("Notes", text: $notes, axis: .vertical).lineLimit(2...4)
            if let error { Text(error).foregroundStyle(.red) }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }.keyboardShortcut(.cancelAction)
                Button(existing == nil ? "Add" : "Save") { submit() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(busy || url.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding().frame(width: 460)
        .onAppear {
            guard let existing else { return }
            url = existing.url ?? ""
            label = existing.label
            notes = existing.notes
        }
    }

    private func submit() {
        busy = true
        let (url, label, notes) = (url.trimmingCharacters(in: .whitespaces), label, notes)
        Task {
            let failure = await store.perform { client in
                if let existing {
                    _ = try await client.updateWatcher(id: existing.id, url: url, label: label, notes: notes)
                } else {
                    _ = try await client.addWatcher(url: url, label: label, notes: notes)
                }
            }
            busy = false
            if let failure { error = failure } else { dismiss() }
        }
    }
}

struct SettingsView: View {
    @EnvironmentObject var store: WatcherStore
    @State private var server = ""
    @State private var topic = ""
    @State private var interval = 30.0
    @State private var renotify = false
    @State private var error: String?
    @State private var loaded = false

    var body: some View {
        Form {
            Section("Appearance") {
                Stepper(
                    "Text size: \(Int((store.settings.fontScale * 100).rounded()))%",
                    value: Binding(
                        get: { store.settings.fontScale },
                        set: { store.setSettings(["font_scale": ($0 * 10).rounded() / 10]) }),
                    in: 0.7...2.0, step: 0.1)
                Toggle(
                    "Keep window on top",
                    isOn: Binding(
                        get: { store.settings.alwaysOnTop }, set: { store.setSettings(["always_on_top": $0]) }))
            }
            Section("Polling") {
                TextField("Seconds between checks", value: $interval, format: .number)
                Toggle("Notify again for results that finished before the app restarted", isOn: $renotify)
            }
            Section("Away mode (ntfy)") {
                TextField("Server", text: $server)
                TextField("Topic", text: $topic)
            }
            if let error { Text(error).foregroundStyle(.red) }
            Button("Save") { save() }.keyboardShortcut(.defaultAction)
        }
        .padding().frame(width: 460)
        .onChange(of: store.settings) { _ in load() }
        .onAppear { load() }
    }

    private func load() {
        guard !loaded || store.phase == .running else { return }
        let s = store.settings
        server = s.ntfyServer
        topic = s.ntfyTopic
        interval = s.pollInterval
        renotify = s.renotifyOnRestart
        loaded = true
    }

    private func save() {
        let patch: [String: Any] = [
            "ntfy_server": server, "ntfy_topic": topic, "poll_interval": interval, "renotify_on_restart": renotify,
        ]
        Task {
            error = await store.perform { try await $0.updateSettings(patch) }
            if error == nil { NSApp.keyWindow?.performClose(nil) }
        }
    }
}
