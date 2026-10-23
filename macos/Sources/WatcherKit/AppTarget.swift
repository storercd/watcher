import Foundation

/// A running app to watch, encoded as `app://<bundle-id>[?window=<title>]`
/// (the format the Python core's app-progress watcher accepts).
public struct AppTarget: Equatable, Sendable {
    public static let scheme = "app://"

    public var bundleID: String
    public var windowTitle: String

    public init(bundleID: String, windowTitle: String = "") {
        self.bundleID = bundleID
        self.windowTitle = windowTitle
    }

    public init?(url: String) {
        let text = url.trimmingCharacters(in: .whitespaces)
        guard text.lowercased().hasPrefix(Self.scheme) else { return nil }
        let rest = text.dropFirst(Self.scheme.count)
        let parts = rest.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false)
        let id = String(parts.first ?? "").removingPercentEncoding ?? ""
        guard !id.isEmpty else { return nil }
        var title = ""
        if parts.count == 2 {
            for pair in parts[1].split(separator: "&") {
                let kv = pair.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
                if kv.first == "window", kv.count == 2 {
                    title = String(kv[1]).replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? ""
                }
            }
        }
        self.init(bundleID: id, windowTitle: title)
    }

    public var url: String {
        var url = Self.scheme + Self.escape(bundleID)
        if !windowTitle.isEmpty { url += "?window=" + Self.escape(windowTitle) }
        return url
    }

    private static func escape(_ text: String) -> String {
        text.addingPercentEncoding(withAllowedCharacters: CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")) ?? text
    }
}
