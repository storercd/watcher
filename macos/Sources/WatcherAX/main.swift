// watcher-ax: reads other apps' progress bars through the macOS Accessibility API.
//
//   watcher-ax progress <bundle-id> [--window-title <substring>]
//   watcher-ax apps
//
// Prints one JSON object on stdout. Needs Accessibility permission for the
// app that launched it (Watcher.app).
import AppKit
import ApplicationServices
import Foundation

func emit(_ object: [String: Any], exitCode: Int32 = 0) -> Never {
    let data = (try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])) ?? Data("{}".utf8)
    print(String(decoding: data, as: UTF8.self))
    exit(exitCode)
}

func fail(_ code: String, _ message: String) -> Never {
    emit(["error": code, "message": message], exitCode: 2)
}

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &value) == .success ? value : nil
}

func string(_ el: AXUIElement, _ name: String) -> String? {
    (attr(el, name) as? String).flatMap { $0.isEmpty ? nil : $0 }
}

func number(_ el: AXUIElement, _ name: String) -> Double? {
    guard let value = attr(el, name) else { return nil }
    if let n = value as? NSNumber { return n.doubleValue }
    return (value as? String).flatMap(Double.init)
}

let maxNodes = 5000
let maxDepth = 40

/// Collects every AXProgressIndicator under `el` (bounded, to survive huge trees).
func collectProgress(_ el: AXUIElement, depth: Int, budget: inout Int, into out: inout [[String: Any]]) {
    budget -= 1
    if budget <= 0 || depth > maxDepth { return }
    if string(el, kAXRoleAttribute) == "AXProgressIndicator" {
        var entry: [String: Any] = [:]
        if let v = number(el, kAXValueAttribute) { entry["value"] = v }
        if let lo = number(el, kAXMinValueAttribute) { entry["min"] = lo }
        if let hi = number(el, kAXMaxValueAttribute) { entry["max"] = hi }
        if let t = string(el, kAXTitleAttribute) ?? string(el, kAXDescriptionAttribute) { entry["title"] = t }
        out.append(entry)
    }
    for child in (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? [] {
        collectProgress(child, depth: depth + 1, budget: &budget, into: &out)
    }
}

func requireTrust() {
    if !AXIsProcessTrusted() {
        fail("accessibility_denied", "Accessibility permission is not granted to Watcher.")
    }
}

func progress(bundleID: String, windowTitle: String?) -> Never {
    requireTrust()
    guard let app = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID).first else {
        emit(["running": false, "windows": 0, "progress": [[String: Any]]()])
    }
    let root = AXUIElementCreateApplication(app.processIdentifier)
    let allWindows = (attr(root, kAXWindowsAttribute) as? [AXUIElement]) ?? []
    let needle = windowTitle?.lowercased()
    let windows = allWindows.filter {
        guard let needle else { return true }
        return (string($0, kAXTitleAttribute) ?? "").lowercased().contains(needle)
    }
    var found: [[String: Any]] = []
    var budget = maxNodes
    for window in windows {
        collectProgress(window, depth: 0, budget: &budget, into: &found)
    }
    emit(["running": true, "windows": windows.count, "progress": found])
}

func listApps() -> Never {
    let apps = NSWorkspace.shared.runningApplications
        .filter { $0.activationPolicy == .regular }
        .compactMap { app -> [String: Any]? in
            guard let id = app.bundleIdentifier else { return nil }
            return ["bundle_id": id, "name": app.localizedName ?? id]
        }
        .sorted { ($0["name"] as? String ?? "") < ($1["name"] as? String ?? "") }
    emit(["apps": apps])
}

let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "progress":
    guard args.count >= 2 else { fail("usage", "progress <bundle-id> [--window-title <text>]") }
    var title: String?
    if let i = args.firstIndex(of: "--window-title"), i + 1 < args.count { title = args[i + 1] }
    progress(bundleID: args[1], windowTitle: title)
case "apps":
    listApps()
default:
    fail("usage", "usage: watcher-ax progress <bundle-id> [--window-title <text>] | apps")
}
