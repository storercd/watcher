// Experiment: dump the Dock's accessibility tree to see whether an app's
// Dock tile exposes progress (AXProgressIndicator, AXValue, AXStatusLabel...).
//
// Usage:
//   swift scripts/dock_ax_probe.swift                  # dump all Dock items once
//   swift scripts/dock_ax_probe.swift Lightroom        # dump only items whose title contains "Lightroom"
//   swift scripts/dock_ax_probe.swift Lightroom --watch  # re-dump every 2s, printing only changes
//
// Requires Accessibility permission for the terminal running it
// (System Settings > Privacy & Security > Accessibility).
import ApplicationServices
import AppKit
import Foundation

let args = Array(CommandLine.arguments.dropFirst())
let watch = args.contains("--watch")
let filter = args.first { !$0.hasPrefix("--") }?.lowercased()

guard AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary) else {
    print("Not trusted for Accessibility. Grant permission to your terminal, then re-run.")
    exit(1)
}

guard let dock = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock").first else {
    print("Dock process not found")
    exit(1)
}
let root = AXUIElementCreateApplication(dock.processIdentifier)

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &value) == .success ? value : nil
}

func attrNames(_ el: AXUIElement) -> [String] {
    var names: CFArray?
    return AXUIElementCopyAttributeNames(el, &names) == .success ? (names as? [String] ?? []) : []
}

func describe(_ v: AnyObject?) -> String {
    guard let v = v else { return "nil" }
    if CFGetTypeID(v) == AXUIElementGetTypeID() { return "<AXUIElement>" }
    if let a = v as? [AnyObject] { return "[\(a.count) items]" }
    // Strip pointer addresses (e.g. "0xb6a962500") so unchanged values don't show up as diffs.
    return "\(v)".replacingOccurrences(of: "0x[0-9a-f]+", with: "0x…", options: .regularExpression)
}

func dump(_ el: AXUIElement, depth: Int, into lines: inout [String]) {
    let pad = String(repeating: "  ", count: depth)
    let role = describe(attr(el, kAXRoleAttribute))
    lines.append("\(pad)\(role)")
    for name in attrNames(el).sorted() where name != kAXChildrenAttribute && name != kAXParentAttribute {
        lines.append("\(pad)  \(name) = \(describe(attr(el, name)))")
    }
    for child in (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? [] {
        dump(child, depth: depth + 1, into: &lines)
    }
}

func dockItems(_ el: AXUIElement) -> [AXUIElement] {
    var out: [AXUIElement] = []
    for child in (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? [] {
        if describe(attr(child, kAXSubroleAttribute)) == "AXApplicationDockItem" || describe(attr(child, kAXRoleAttribute)) == "AXDockItem" {
            out.append(child)
        }
        out += dockItems(child)
    }
    return out
}

func snapshot() -> [String: [String]] {
    var result: [String: [String]] = [:]
    for item in dockItems(root) {
        let title = describe(attr(item, kAXTitleAttribute))
        if let f = filter, !title.lowercased().contains(f) { continue }
        var lines: [String] = []
        dump(item, depth: 0, into: &lines)
        result[title] = lines
    }
    return result
}

var previous: [String: [String]] = [:]
repeat {
    let current = snapshot()
    if current.isEmpty { print("No matching Dock items.") }
    for (title, lines) in current.sorted(by: { $0.key < $1.key }) where lines != previous[title] {
        print("=== \(title) @ \(Date()) ===")
        print(lines.joined(separator: "\n"))
    }
    previous = current
    if watch { Thread.sleep(forTimeInterval: 2) }
} while watch
