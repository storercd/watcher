import AppKit
import ApplicationServices
import SwiftUI
import WatcherKit

struct WatcherForm: View {
    @EnvironmentObject var store: WatcherStore
    @Environment(\.dismiss) private var dismiss
    let existing: WatcherItem?
    let defaultLongTerm: Bool

    private static let intervalChoices: [Double] = [300, 600, 1800, 3600, 14400, 86400]

    @State private var longTerm = false
    @State private var trigger: LongTermTrigger = .closed
    @State private var interval: Double = 600
    @State private var url = ""
    @State private var label = ""
    @State private var notes = ""
    @State private var error: String?
    @State private var busy = false
    @State private var windowTitle = ""
    @State private var readDockIcon = false
    @State private var commands: [TerminalCommand] = []

    private var appTarget: AppTarget? { longTerm ? nil : AppTarget(url: url) }
    private var triggerChoices: [LongTermTrigger] { LongTermTrigger.available(for: url) }

    var body: some View {
        Form {
            HStack {
                TextField(
                    "URL", text: $url,
                    prompt: Text(longTerm ? "GitHub PR or issue URL" : "Jenkins job, GitHub PR or Actions run URL"))
                if !longTerm {
                    Menu("Running app…") {
                        ForEach(runningApps, id: \.bundleIdentifier) { app in
                            Button(app.localizedName ?? app.bundleIdentifier ?? "") { choose(app) }
                        }
                    }
                    .fixedSize()
                    Menu("Running command…") {
                        if commands.isEmpty { Text("No commands running in a terminal") }
                        ForEach(commands) { command in
                            Button("\(command.title) (\(formatElapsed(command.elapsed)))") { choose(command) }
                        }
                        Divider()
                        Button("Refresh") { loadCommands() }
                    }
                    .fixedSize()
                }
            }
            if longTerm {
                Picker("Alert on", selection: $trigger) {
                    ForEach(triggerChoices, id: \.self) { Text($0.title).tag($0) }
                }
                .onChange(of: url) { _ in
                    if !triggerChoices.contains(trigger) { trigger = .closed }
                }
                Picker("Check every", selection: $interval) {
                    ForEach(Self.intervalChoices + (Self.intervalChoices.contains(interval) ? [] : [interval]), id: \.self) {
                        Text(formatInterval($0)).tag($0)
                    }
                }
            }
            if ProcessTarget.isProcessURL(url) {
                Text("Alerts when this command exits. Success or failure isn't reported, only that it finished.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            if let target = appTarget {
                TextField(
                    "Window title", text: $windowTitle,
                    prompt: Text("Optional - only watch windows whose title contains this")
                )
                .onChange(of: windowTitle) { title in
                    url = AppTarget(bundleID: target.bundleID, windowTitle: title, readDockIcon: readDockIcon).url
                }
                Toggle("Also read the Dock icon's progress bar", isOn: $readDockIcon)
                    .onChange(of: readDockIcon) { enabled in
                        url = AppTarget(bundleID: target.bundleID, windowTitle: windowTitle, readDockIcon: enabled).url
                        if enabled { _ = CGRequestScreenCaptureAccess() }
                    }
                if readDockIcon {
                    Text("For apps like Lightroom that draw progress without exposing it. Works while the app is minimized; needs Screen Recording permission.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Text("Alerts when \(target.bundleID)'s progress bar finishes. Needs Accessibility permission.")
                    .font(.caption).foregroundStyle(.secondary)
            }
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
            loadCommands()
            guard let existing else {
                longTerm = defaultLongTerm
                return
            }
            longTerm = existing.isLongTerm
            trigger = existing.trigger.flatMap(LongTermTrigger.init(rawValue:)) ?? .closed
            interval = existing.pollInterval ?? 600
            url = existing.url ?? ""
            windowTitle = AppTarget(url: url)?.windowTitle ?? ""
            readDockIcon = AppTarget(url: url)?.readDockIcon ?? false
            label = existing.label
            notes = existing.notes
        }
    }

    private var runningApps: [NSRunningApplication] {
        NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular && $0.bundleIdentifier != nil }
            .filter { $0.bundleIdentifier != Bundle.main.bundleIdentifier }
            .sorted { ($0.localizedName ?? "").localizedCaseInsensitiveCompare($1.localizedName ?? "") == .orderedAscending }
    }

    private func loadCommands() {
        Task {
            var loaded: [TerminalCommand] = []
            _ = await store.perform { loaded = try await $0.terminalCommands() }
            commands = loaded
        }
    }

    private func choose(_ command: TerminalCommand) {
        url = command.url
        if label.isEmpty { label = "Command: \(command.title)" }
    }

    private func formatElapsed(_ seconds: Int) -> String {
        if seconds >= 3600 { return "\(seconds / 3600)h \(seconds % 3600 / 60)m" }
        return seconds >= 60 ? "\(seconds / 60)m" : "\(seconds)s"
    }

    private func choose(_ app: NSRunningApplication) {
        guard let id = app.bundleIdentifier else { return }
        windowTitle = ""
        readDockIcon = false
        url = AppTarget(bundleID: id).url
        if label.isEmpty { label = app.localizedName ?? id }
        // Reading another app's progress bar needs the Accessibility permission.
        let prompt = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        _ = AXIsProcessTrustedWithOptions([prompt: true] as CFDictionary)
    }

    private func submit() {
        busy = true
        let (url, label, notes) = (url.trimmingCharacters(in: .whitespaces), label, notes)
        let (longTerm, trigger, interval) = (longTerm, trigger.rawValue, interval)
        Task {
            let failure = await store.perform { client in
                if let existing {
                    _ = try await client.updateWatcher(
                        id: existing.id, url: url, label: label, notes: notes,
                        trigger: longTerm ? trigger : nil, pollInterval: longTerm ? interval : nil)
                } else if longTerm {
                    _ = try await client.addWatcher(
                        url: url, label: label, notes: notes,
                        category: "long_term", trigger: trigger, pollInterval: interval)
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
