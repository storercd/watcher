import Foundation

public struct APIError: Error, LocalizedError, Sendable {
    public let status: Int
    public let message: String
    public var errorDescription: String? { message }
}

/// Talks to the local Watcher engine API (see docs/api.yaml).
public final class APIClient: @unchecked Sendable {
    private let base: URL
    private let token: String
    private let session: URLSession

    public init(port: Int, token: String, session: URLSession = .shared) {
        self.base = URL(string: "http://127.0.0.1:\(port)")!
        self.token = token
        self.session = session
    }

    private func makeRequest(_ method: String, _ path: String, body: [String: Any]? = nil) throws -> URLRequest {
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return request
    }

    @discardableResult
    private func send(_ method: String, _ path: String, body: [String: Any]? = nil) async throws -> Data {
        let (data, response) = try await session.data(for: makeRequest(method, path, body: body))
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let message = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String
            throw APIError(status: status, message: message ?? "HTTP \(status)")
        }
        return data
    }

    public func state() async throws -> EngineState {
        try JSONDecoder().decode(EngineState.self, from: try await send("GET", "/v1/state"))
    }

    public func addWatcher(url: String, label: String, notes: String) async throws -> WatcherItem {
        let data = try await send("POST", "/v1/watchers", body: ["url": url, "label": label, "notes": notes])
        return try JSONDecoder().decode(WatcherItem.self, from: data)
    }

    public func updateWatcher(id: String, url: String, label: String, notes: String) async throws -> WatcherItem {
        let data = try await send("PATCH", "/v1/watchers/\(id)", body: ["url": url, "label": label, "notes": notes])
        return try JSONDecoder().decode(WatcherItem.self, from: data)
    }

    public func removeWatcher(id: String) async throws {
        try await send("DELETE", "/v1/watchers/\(id)")
    }

    public func acknowledge(id: String) async throws {
        try await send("POST", "/v1/watchers/\(id)/acknowledge")
    }

    public func pollNow() async throws {
        try await send("POST", "/v1/poll")
    }

    public func updateSettings(_ patch: [String: Any]) async throws {
        try await send("PATCH", "/v1/settings", body: patch)
    }

    public func dismissUpdate(version: String) async throws {
        try await send("POST", "/v1/update/dismiss", body: ["version": version])
    }

    public func shutdown() async {
        _ = try? await send("POST", "/v1/shutdown")
    }

    /// Streams engine events until the connection drops or the task is cancelled.
    public func events() -> AsyncThrowingStream<EngineEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try makeRequest("GET", "/v1/events")
                    request.timeoutInterval = .infinity
                    let (bytes, response) = try await session.bytes(for: request)
                    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                    guard status == 200 else { throw APIError(status: status, message: "HTTP \(status)") }
                    var parser = SSEParser()
                    // `bytes.lines` drops empty lines, which are what terminate an SSE event.
                    var buffer: [UInt8] = []
                    for try await byte in bytes {
                        guard byte == UInt8(ascii: "\n") else {
                            buffer.append(byte)
                            continue
                        }
                        if buffer.last == UInt8(ascii: "\r") { buffer.removeLast() }
                        let line = String(decoding: buffer, as: UTF8.self)
                        buffer.removeAll(keepingCapacity: true)
                        if let message = parser.feed(line: line), let event = try decodeEvent(message) {
                            continuation.yield(event)
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
