import Foundation

/// A running app to watch, encoded as `app://<bundle-id>[?window=<title>][&dock=1]`
/// (the format the Python core's app-progress watcher accepts).
public struct AppTarget: Equatable, Sendable {
    public static let scheme = "app://"

    public var bundleID: String
    public var windowTitle: String
    /// Also read a progress bar drawn on the app's Dock icon (needs Screen Recording permission).
    public var readDockIcon: Bool

    public init(bundleID: String, windowTitle: String = "", readDockIcon: Bool = false) {
        self.bundleID = bundleID
        self.windowTitle = windowTitle
        self.readDockIcon = readDockIcon
    }

    public init?(url: String) {
        let text = url.trimmingCharacters(in: .whitespaces)
        guard text.lowercased().hasPrefix(Self.scheme) else { return nil }
        let rest = text.dropFirst(Self.scheme.count)
        let parts = rest.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        let id = String(parts.first ?? "").removingPercentEncoding ?? ""
        guard !id.isEmpty else { return nil }
        var title = ""
        var dock = false
        if parts.count == 2 {
            for pair in parts[1].split(separator: "&") {
                let kv = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
                if kv.first == "window", kv.count == 2 {
                    title = String(kv[1]).replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? ""
                }
                if kv.first == "dock", kv.count == 2 { dock = kv[1] == "1" }
            }
        }
        self.init(bundleID: id, windowTitle: title, readDockIcon: dock)
    }

    public var url: String {
        var url = Self.scheme + Self.escape(bundleID)
        var params: [String] = []
        if !windowTitle.isEmpty { params.append("window=" + Self.escape(windowTitle)) }
        if readDockIcon { params.append("dock=1") }
        if !params.isEmpty { url += "?" + params.joined(separator: "&") }
        return url
    }

    private static func escape(_ text: String) -> String {
        text.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? text
    }
}
