import AppKit
import SwiftUI

final class AppDelegate: NSObject, NSApplicationDelegate {
    var onTerminate: (() -> Void)?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) { onTerminate?() }
}

@main
struct WatcherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store = WatcherStore()

    var body: some Scene {
        WindowGroup("Watcher") {
            ContentView()
                .environmentObject(store)
                .onAppear {
                    delegate.onTerminate = { [store] in MainActor.assumeIsolated { store.shutdown() } }
                    store.start()
                }
        }
        Settings { SettingsView().environmentObject(store) }
    }
}
