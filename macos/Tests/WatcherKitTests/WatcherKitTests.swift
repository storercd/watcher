import Foundation
import Testing

@testable import WatcherKit

@Suite struct SSEParserTests {
    @Test func parsesEventAndIgnoresKeepalive() throws {
        var parser = SSEParser()
        #expect(parser.feed(line: ": keepalive") == nil)
        #expect(parser.feed(line: "event: watcher_removed") == nil)
        #expect(parser.feed(line: "data: {\"type\":\"watcher_removed\",\"id\":\"abc\"}") == nil)
        let message = try #require(parser.feed(line: "") as SSEParser.Message?)
        #expect(message.event == "watcher_removed")
        guard case .watcherRemoved(let id)? = try decodeEvent(message) else { Issue.record("wrong event"); return }
        #expect(id == "abc")
    }

    @Test func decodesStateEvent() throws {
        let json = """
            {"watchers":[{"id":"1","type":"jenkins","label":"L","notes":"","url":"http://x","status":"building",
            "detail":"d","last_checked":null,"unacknowledged":false}],
            "settings":{"mode":"At Desk","ntfy_server":"s","ntfy_topic":"t","poll_interval":30,
            "renotify_on_restart":false,"modes":["At Desk","Away"]},"update":null}
            """
        let message = SSEParser.Message(event: "state", data: json)
        guard case .state(let state)? = try decodeEvent(message) else { Issue.record("wrong event"); return }
        #expect(state.watchers.first?.status == .building)
        #expect(state.watchers.first?.lastChecked == nil)
        #expect(state.settings.modes == ["At Desk", "Away"])
    }
}

@Suite struct CoreProcessTests {
    private var repoRoot: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    @Test func locateFindsSourceCheckout() throws {
        let command = try #require(
            CoreProcess.locate(environment: [:], bundleResources: nil, startingAt: repoRoot.appendingPathComponent("macos")))
        #expect(command.workingDirectory?.standardizedFileURL == repoRoot.standardizedFileURL)
        #expect(command.arguments.contains("--exit-on-stdin-close"))
    }

    @Test func locateReturnsNilWhenNothingFound() {
        #expect(CoreProcess.locate(environment: [:], bundleResources: nil, startingAt: URL(fileURLWithPath: "/tmp")) == nil)
    }

    /// Full round trip against the real Python engine, using a throwaway HOME so no real config is touched.
    @Test func endToEndAgainstPythonCore() async throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("watcher-e2e-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        setenv("HOME", home.path, 1)
        guard let command = CoreProcess.locate(environment: [:], bundleResources: nil, startingAt: repoRoot) else {
            return
        }
        let core = CoreProcess()
        let handshake = try await core.start(command)
        defer { core.stop() }

        let client = APIClient(port: handshake.port, token: handshake.token)
        #expect(try await client.state().watchers.isEmpty)

        var iterator = client.events().makeAsyncIterator()
        guard case .state? = try await iterator.next() else { Issue.record("expected initial state event"); return }

        let added = try await client.addWatcher(
            url: "https://jenkins.example.invalid/job/demo/", label: "Demo", notes: "n")
        guard case .watcherAdded(let item)? = try await iterator.next() else { Issue.record("expected watcher_added"); return }
        #expect(item.id == added.id)

        do {
            _ = try await client.addWatcher(url: "not a url", label: "", notes: "")
            Issue.record("expected a 422")
        } catch let error as APIError {
            #expect(error.status == 422)
        }

        try await client.removeWatcher(id: added.id)
        #expect(try await client.state().watchers.isEmpty)

        await client.shutdown()
        for _ in 0..<50 where core.isRunning { try await Task.sleep(nanoseconds: 100_000_000) }
        #expect(!(core.isRunning))
    }
}
