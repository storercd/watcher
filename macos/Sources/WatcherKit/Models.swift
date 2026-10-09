import Foundation

public enum WatchStatus: String, Codable, Sendable {
    case unknown, idle, building, success, failure, error
}

public struct WatcherItem: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public var type: String
    public var label: String
    public var notes: String
    public var url: String?
    public var status: WatchStatus
    public var detail: String
    public var lastChecked: Double?
    public var unacknowledged: Bool

    enum CodingKeys: String, CodingKey {
        case id, type, label, notes, url, status, detail
        case lastChecked = "last_checked"
        case unacknowledged
    }
}

public struct Settings: Codable, Equatable, Sendable {
    public var mode: String
    public var ntfyServer: String
    public var ntfyTopic: String
    public var pollInterval: Double
    public var renotifyOnRestart: Bool
    public var modes: [String]
    public var fontScale: Double
    public var alwaysOnTop: Bool

    enum CodingKeys: String, CodingKey {
        case mode
        case fontScale = "font_scale"
        case alwaysOnTop = "always_on_top"
        case ntfyServer = "ntfy_server"
        case ntfyTopic = "ntfy_topic"
        case pollInterval = "poll_interval"
        case renotifyOnRestart = "renotify_on_restart"
        case modes
    }

    public init(
        mode: String = "At Desk", ntfyServer: String = "", ntfyTopic: String = "",
        pollInterval: Double = 30, renotifyOnRestart: Bool = false, modes: [String] = ["At Desk", "Away"],
        fontScale: Double = 1.0, alwaysOnTop: Bool = false
    ) {
        self.fontScale = fontScale
        self.alwaysOnTop = alwaysOnTop
        self.mode = mode
        self.ntfyServer = ntfyServer
        self.ntfyTopic = ntfyTopic
        self.pollInterval = pollInterval
        self.renotifyOnRestart = renotifyOnRestart
        self.modes = modes
    }
}

extension Settings {
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        mode = try c.decode(String.self, forKey: .mode)
        ntfyServer = try c.decode(String.self, forKey: .ntfyServer)
        ntfyTopic = try c.decode(String.self, forKey: .ntfyTopic)
        pollInterval = try c.decode(Double.self, forKey: .pollInterval)
        renotifyOnRestart = try c.decode(Bool.self, forKey: .renotifyOnRestart)
        modes = try c.decode([String].self, forKey: .modes)
        fontScale = try c.decodeIfPresent(Double.self, forKey: .fontScale) ?? 1.0
        alwaysOnTop = try c.decodeIfPresent(Bool.self, forKey: .alwaysOnTop) ?? false
    }
}

public struct UpdateInfo: Codable, Equatable, Sendable {
    public var version: String
    public var url: String
    public var name: String
}

public struct EngineState: Codable, Sendable {
    public var watchers: [WatcherItem]
    public var settings: Settings
    public var update: UpdateInfo?
}

/// A decoded Server-Sent Event from `/v1/events`.
public enum EngineEvent: Sendable {
    case state(EngineState)
    case watcherAdded(WatcherItem)
    case watcherUpdated(WatcherItem)
    case watcherRemoved(id: String)
    case settingsChanged(Settings)
    case updateAvailable(UpdateInfo)
    case notification(title: String, message: String, id: String)
}

public struct Handshake: Codable, Sendable {
    public let port: Int
    public let token: String
}
