// Experiment: walk a running app's own accessibility tree and print the text
// it exposes (AXStaticText etc.), to see whether progress strings such as
// "Exporting 1 of 24" are readable, and under which window states
// (visible / minimized / hidden / other Space).
//
// Usage:
//   swift scripts/app_ax_probe.swift <bundle-id-or-name-substring> [--watch] [--all] [--wake]
//
//   --watch  re-scan every 2s and print only when the output changes
//   --all    print every element (role + title/value/description), not just text
//   --wake   set AXEnhancedUserInterface on the app, which some apps need
//            before they populate their accessibility tree
//
// Example:
//   swift scripts/app_ax_probe.swift lightroom --watch
//
// Requires Accessibility permission for the terminal running it.
import ApplicationServices
import AppKit
import Foundation

let args = Array(CommandLine.arguments.dropFirst())
let watch = args.contains("--watch")
let showAll = args.contains("--all")
let wake = args.contains("--wake")
guard let query = args.first(where: { !$0.hasPrefix("--") })?.lowercased() else {
    print("usage: app_ax_probe.swift <bundle-id-or-name> [--watch] [--all] [--wake]")
    exit(1)
}

guard AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary) else {
    print("Not trusted for Accessibility. Grant permission to your terminal, then re-run.")
    exit(1)
}

guard let app = NSWorkspace.shared.runningApplications.first(where: {
    $0.activationPolicy == .regular &&
        (($0.bundleIdentifier ?? "").lowercased().contains(query) || ($0.localizedName ?? "").lowercased().contains(query))
}) else {
    print("No running GUI app matches '\(query)'")
    exit(1)
}
print("Target: \(app.localizedName ?? "?") [\(app.bundleIdentifier ?? "?")] pid \(app.processIdentifier) hidden=\(app.isHidden)")

let root = AXUIElementCreateApplication(app.processIdentifier)
if wake {
    print("wake: AXEnhancedUserInterface -> \(AXUIElementSetAttributeValue(root, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue).rawValue)")
}

func attr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(el, name as CFString, &value) == .success ? value : nil
}

func str(_ el: AXUIElement, _ name: String) -> String? {
    guard let v = attr(el, name) else { return nil }
    if let s = v as? String { return s.isEmpty ? nil : s }
    if let n = v as? NSNumber { return n.stringValue }
    return nil
}

let textRoles: Set<String> = ["AXStaticText", "AXProgressIndicator", "AXTextField", "AXTextArea"]
var nodeCount = 0

func walk(_ el: AXUIElement, depth: Int, into lines: inout [String]) {
    nodeCount += 1
    if nodeCount > 5000 || depth > 40 { return }
    let role = str(el, kAXRoleAttribute) ?? "?"
    let bits = [
        str(el, kAXTitleAttribute).map { "title=\($0)" },
        str(el, kAXValueAttribute).map { "value=\($0)" },
        str(el, kAXDescriptionAttribute).map { "desc=\($0)" },
    ].compactMap { $0 }
    if showAll || (textRoles.contains(role) && !bits.isEmpty) {
        lines.append("\(String(repeating: "  ", count: depth))\(role) \(bits.joined(separator: " | "))")
    }
    for child in (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? [] {
        walk(child, depth: depth + 1, into: &lines)
    }
}

func snapshot() -> String {
    var lines: [String] = ["hidden=\(app.isHidden) active=\(app.isActive)"]
    nodeCount = 0
    let windows = (attr(root, kAXWindowsAttribute) as? [AXUIElement]) ?? []
    lines.append("windows listed: \(windows.count)")
    for w in windows {
        let minimized = str(w, kAXMinimizedAttribute) ?? "?"
        lines.append("WINDOW '\(str(w, kAXTitleAttribute) ?? "")' minimized=\(minimized)")
        walk(w, depth: 1, into: &lines)
    }
    if nodeCount > 5000 { lines.append("(node cap reached)") }
    return lines.joined(separator: "\n")
}

var previous = ""
repeat {
    let current = snapshot()
    if current != previous {
        print("=== \(Date()) ===")
        print(current)
        previous = current
    }
    if watch { Thread.sleep(forTimeInterval: 2) }
} while watch
