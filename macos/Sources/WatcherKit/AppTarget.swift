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

/// A command running in a terminal, as listed by the core's `/v1/processes` endpoint.
public struct TerminalCommand: Codable, Identifiable, Equatable, Sendable {
    public let pid: Int
    public let tty: String
    public let started: Double
    public let elapsed: Int
    public let command: String
    public let cwd: String

    public var id: String { "\(pid)-\(Int(started))" }

    /// The `proc://<pid>?started=<epoch>&cmd=<text>` URL the core's process watcher accepts.
    public var url: String { ProcessTarget.url(pid: pid, started: started, command: command) }

    /// A short name for menus and the default label: the program and its arguments, without its directory.
    public var title: String {
        let parts = command.split(separator: " ", omittingEmptySubsequences: true)
        guard let first = parts.first else { return command }
        let program = first.split(separator: "/").last.map(String.init) ?? String(first)
        return ([program] + parts.dropFirst().map(String.init)).joined(separator: " ")
    }
}

public enum ProcessTarget {
    public static let scheme = "proc://"

    public static func isProcessURL(_ url: String) -> Bool {
        url.trimmingCharacters(in: .whitespaces).lowercased().hasPrefix(scheme)
    }

    public static func url(pid: Int, started: Double, command: String) -> String {
        var url = "\(scheme)\(pid)?started=\(Int(started))"
        let allowed = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~")
        if !command.isEmpty, let escaped = command.addingPercentEncoding(withAllowedCharacters: allowed) {
            url += "&cmd=" + escaped
        }
        return url
    }
}
