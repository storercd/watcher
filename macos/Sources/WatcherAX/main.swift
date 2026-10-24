// watcher-ax: reads other apps' progress bars through the macOS Accessibility API.
//
//   watcher-ax progress <bundle-id> [--window-title <substring>] [--dock]
//   watcher-ax apps
//
// Prints one JSON object on stdout. Needs Accessibility permission for the
// app that launched it (Watcher.app). `--dock` also reads a progress bar drawn
// on the app's Dock icon (which no accessibility API exposes) by screenshotting
// the tile, and so needs Screen Recording permission too.
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

// MARK: - Dock tile progress (pixel based)

struct Pixels {
    let width: Int
    let height: Int
    let data: [UInt8]  // RGBA

    func rgb(_ x: Int, _ y: Int) -> (Int, Int, Int) {
        let i = (y * width + x) * 4
        return (Int(data[i]), Int(data[i + 1]), Int(data[i + 2]))
    }
}

func distance(_ a: (Int, Int, Int), _ b: (Int, Int, Int)) -> Int {
    max(abs(a.0 - b.0), abs(a.1 - b.1), abs(a.2 - b.2))
}

struct Run {
    var start: Int
    var end: Int  // exclusive
    var color: (Int, Int, Int)
    var length: Int { end - start }
}

/// Splits one pixel row into runs of near-constant color.
func runs(in px: Pixels, row y: Int) -> [Run] {
    var out: [Run] = []
    var x = 0
    while x < px.width {
        let color = px.rgb(x, y)
        var end = x + 1
        while end < px.width && distance(px.rgb(end, y), color) <= 18 { end += 1 }
        out.append(Run(start: x, end: end, color: color))
        x = end
    }
    return out
}

struct Bar {
    var left: Int
    var right: Int
    var top: Int
    var bottom: Int  // exclusive
    var fill: Int
    var thickness: Int { bottom - top }
    var width: Int { right - left }
}

/// Finds a horizontal bar: a long, uniform "track" run with a differently colored "fill" run
/// directly to its left, repeated over a few consecutive rows. Returns the best candidate.
func findBar(in px: Pixels) -> Bar? {
    let minTrack = px.width * 35 / 100
    let maxThickness = max(4, px.height / 5)
    var best: Bar?
    var current: Bar?

    func flush() {
        if let bar = current, bar.thickness >= max(5, px.height / 20) && bar.thickness <= maxThickness {
            if best == nil || bar.width * bar.thickness > best!.width * best!.thickness { best = bar }
        }
        current = nil
    }

    for y in 0..<px.height {
        var candidate: Bar?
        let rowRuns = runs(in: px, row: y)
        search: for i in 0..<rowRuns.count {
            let fill = rowRuns[i]
            // A real bar sits inside its icon: there is something on both sides of it.
            guard fill.length >= 2, i > 0 else { continue }
            // Skip an antialiased 1-3px edge between the fill and the track.
            var gap = 0
            for track in rowRuns[(i + 1)...] {
                if track.length >= minTrack {
                    if distance(fill.color, track.color) >= 60, track.end < px.width {
                        candidate = Bar(left: fill.start, right: track.end, top: y, bottom: y + 1, fill: fill.length + gap / 2)
                        break search
                    }
                    break
                }
                gap += track.length
                if gap > 3 { break }
            }
        }
        if let c = candidate, var cur = current, abs(c.left - cur.left) <= 3, abs(c.right - cur.right) <= 3 {
            cur.bottom = y + 1
            cur.fill = max(cur.fill, c.fill)
            current = cur
        } else {
            flush()
            current = candidate
        }
    }
    flush()
    return best
}

func loadPixels(_ path: String) -> Pixels? {
    guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(src, 0, nil) else { return nil }
    let w = image.width, h = image.height
    var data = [UInt8](repeating: 0, count: w * h * 4)
    let drawn = data.withUnsafeMutableBytes { buf -> Bool in
        guard let ctx = CGContext(
            data: buf.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return false }
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return true
    }
    return drawn ? Pixels(width: w, height: h, data: data) : nil
}

/// The on-screen frame (points) of the app's Dock tile, found through the Dock's accessibility tree.
func dockTileFrame(for app: NSRunningApplication) -> CGRect? {
    guard let dock = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock").first else {
        return nil
    }
    let name = app.localizedName
    let bundlePath = app.bundleURL?.standardizedFileURL.path
    var found: CGRect?

    func visit(_ el: AXUIElement, depth: Int) {
        if found != nil || depth > 6 { return }
        if string(el, kAXSubroleAttribute) == "AXApplicationDockItem" {
            let urlMatch = (attr(el, "AXURL") as? URL)?.standardizedFileURL.path == bundlePath
            if urlMatch || string(el, kAXTitleAttribute) == name,
               let posValue = attr(el, kAXPositionAttribute), let sizeValue = attr(el, kAXSizeAttribute) {
                var p = CGPoint.zero, s = CGSize.zero
                AXValueGetValue(posValue as! AXValue, .cgPoint, &p)
                AXValueGetValue(sizeValue as! AXValue, .cgSize, &s)
                found = CGRect(origin: p, size: s)
            }
            return
        }
        for child in (attr(el, kAXChildrenAttribute) as? [AXUIElement]) ?? [] { visit(child, depth: depth + 1) }
    }
    visit(AXUIElementCreateApplication(dock.processIdentifier), depth: 0)
    return found
}

/// Reads the progress bar drawn on the app's Dock icon, as one `progress` entry (0-100) or nil if none is showing.
func dockProgress(for app: NSRunningApplication) -> [String: Any]? {
    guard CGPreflightScreenCaptureAccess() else {
        fail("screen_recording_denied", "Screen Recording permission is not granted to Watcher.")
    }
    guard let frame = dockTileFrame(for: app) else { return nil }
    let path = NSTemporaryDirectory() + "watcher-dock-\(getpid()).png"
    defer { try? FileManager.default.removeItem(atPath: path) }
    let rect = "\(Int(frame.minX.rounded())),\(Int(frame.minY.rounded())),\(Int(frame.width.rounded())),\(Int(frame.height.rounded()))"
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-R", rect, path]
    do { try capture.run() } catch { return nil }
    capture.waitUntilExit()
    guard let px = loadPixels(path), let bar = findBar(in: px) else { return nil }
    let pct = 100.0 * Double(bar.fill) / Double(bar.width)
    return ["value": pct.rounded(), "min": 0, "max": 100, "title": "Dock icon", "source": "dock"]
}

func requireTrust() {
    if !AXIsProcessTrusted() {
        fail("accessibility_denied", "Accessibility permission is not granted to Watcher.")
    }
}

func progress(bundleID: String, windowTitle: String?, dock: Bool) -> Never {
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
    if found.isEmpty, dock, let entry = dockProgress(for: app) { found.append(entry) }
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
    guard args.count >= 2 else { fail("usage", "progress <bundle-id> [--window-title <text>] [--dock]") }
    var title: String?
    if let i = args.firstIndex(of: "--window-title"), i + 1 < args.count { title = args[i + 1] }
    progress(bundleID: args[1], windowTitle: title, dock: args.contains("--dock"))
case "apps":
    listApps()
default:
    fail("usage", "usage: watcher-ax progress <bundle-id> [--window-title <text>] [--dock] | apps")
}
