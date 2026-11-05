import AppKit
import SwiftUI

enum BuildChannel {
    static let isDev = (Bundle.main.object(forInfoDictionaryKey: "WatcherBuildChannel") as? String) == "dev"
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var onTerminate: (() -> Void)?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        if BuildChannel.isDev { NSApp.dockTile.badgeLabel = "DEV" }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) { onTerminate?() }
}

@main
struct WatcherApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var store = WatcherStore()

    var body: some Scene {
        WindowGroup(BuildChannel.isDev ? "Watcher — DEV BUILD" : "Watcher") {
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
