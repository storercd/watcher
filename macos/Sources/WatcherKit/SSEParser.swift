import Foundation

/// Incremental parser for the `text/event-stream` framing: `event:`/`data:` lines, blank line ends an event.
public struct SSEParser {
    public struct Message: Equatable, Sendable {
        public var event: String
        public var data: String
    }

    private var event = "message"
    private var dataLines: [String] = []

    public init() {}

    /// Feed one line (without its newline). Returns a message when the line completes one.
    public mutating func feed(line: String) -> Message? {
        if line.isEmpty {
            defer { event = "message"; dataLines = [] }
            return dataLines.isEmpty ? nil : Message(event: event, data: dataLines.joined(separator: "\n"))
        }
        if line.hasPrefix(":") { return nil }
        let parts = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        let field = String(parts[0])
        var value = parts.count > 1 ? String(parts[1]) : ""
        if value.hasPrefix(" ") { value.removeFirst() }
        switch field {
        case "event": event = value
        case "data": dataLines.append(value)
        default: break
        }
        return nil
    }
}

public func decodeEvent(_ message: SSEParser.Message) throws -> EngineEvent? {
    let data = Data(message.data.utf8)
    let decoder = JSONDecoder()
    struct W: Decodable { let watcher: WatcherItem }
    struct S: Decodable { let settings: Settings }
    struct R: Decodable { let id: String }
    struct N: Decodable { let title: String; let message: String; let id: String }
    switch message.event {
    case "state": return .state(try decoder.decode(EngineState.self, from: data))
    case "watcher_added": return .watcherAdded(try decoder.decode(W.self, from: data).watcher)
    case "watcher_updated": return .watcherUpdated(try decoder.decode(W.self, from: data).watcher)
    case "watcher_removed": return .watcherRemoved(id: try decoder.decode(R.self, from: data).id)
    case "settings_changed": return .settingsChanged(try decoder.decode(S.self, from: data).settings)
    case "update_available": return .updateAvailable(try decoder.decode(UpdateInfo.self, from: data))
    case "notification":
        let n = try decoder.decode(N.self, from: data)
        return .notification(title: n.title, message: n.message, id: n.id)
    default: return nil
    }
}
