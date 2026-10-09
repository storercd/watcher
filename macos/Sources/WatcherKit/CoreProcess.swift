import Foundation

public enum CoreError: Error, LocalizedError {
    case notFound
    case handshakeFailed(String)
    public var errorDescription: String? {
        switch self {
        case .notFound:
            return "Could not find the Watcher core. Set WATCHER_REPO to the repo checkout, or use a packaged build."
        case .handshakeFailed(let why):
            return "The Watcher core failed to start: \(why)"
        }
    }
}

/// Launches and supervises the Python engine (`python -m watcher.api`) as a child process.
public final class CoreProcess: @unchecked Sendable {
    public struct Command: Equatable {
        public var executable: URL
        public var arguments: [String]
        public var workingDirectory: URL?

        public init(executable: URL, arguments: [String], workingDirectory: URL?) {
            self.executable = executable
            self.arguments = arguments
            self.workingDirectory = workingDirectory
        }
    }

    private var process: Process?
    private var stdinPipe: Pipe?
    private let stderrTail = LockedString()

    public init() {}

    /// Finds how to run the core: a frozen sidecar in the app bundle first, then a source checkout.
    public static func locate(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        bundleResources: URL? = Bundle.main.resourceURL,
        startingAt start: URL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath),
        fileManager: FileManager = .default
    ) -> Command? {
        let flags = ["--exit-on-stdin-close"]
        if let override = environment["WATCHER_CORE_EXEC"], fileManager.isExecutableFile(atPath: override) {
            return Command(executable: URL(fileURLWithPath: override), arguments: flags, workingDirectory: nil)
        }
        if let resources = bundleResources {
            let frozen = resources.appendingPathComponent("watcher-core/watcher-core")
            if fileManager.isExecutableFile(atPath: frozen.path) {
                return Command(executable: frozen, arguments: flags, workingDirectory: nil)
            }
        }
        guard let repo = findRepo(environment: environment, startingAt: start, fileManager: fileManager) else {
            return nil
        }
        var python = environment["WATCHER_PYTHON"]
        if python == nil {
            python = [".venv/bin/python3", "venv/bin/python3"]
                .map { repo.appendingPathComponent($0).path }
                .first { fileManager.isExecutableFile(atPath: $0) }
        }
        if let python {
            return Command(
                executable: URL(fileURLWithPath: python), arguments: ["-m", "watcher.api"] + flags,
                workingDirectory: repo)
        }
        return Command(
            executable: URL(fileURLWithPath: "/usr/bin/env"), arguments: ["python3", "-m", "watcher.api"] + flags,
            workingDirectory: repo)
    }

    static func findRepo(environment: [String: String], startingAt start: URL, fileManager: FileManager) -> URL? {
        if let explicit = environment["WATCHER_REPO"] { return URL(fileURLWithPath: explicit) }
        var dir = start.standardizedFileURL
        for _ in 0..<6 {
            if fileManager.fileExists(atPath: dir.appendingPathComponent("watcher/api/__main__.py").path) { return dir }
            let parent = dir.deletingLastPathComponent()
            if parent == dir { break }
            dir = parent
        }
        return nil
    }

    /// Starts the core and returns the connection details it prints on its first stdout line.
    public func start(_ override: Command? = nil) async throws -> Handshake {
        guard let command = override ?? CoreProcess.locate(),
            FileManager.default.isExecutableFile(atPath: command.executable.path)
        else { throw CoreError.notFound }
        let process = Process()
        process.executableURL = command.executable
        process.arguments = command.arguments
        process.currentDirectoryURL = command.workingDirectory
        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
        let tail = stderrTail
        stderr.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return handle.readabilityHandler = nil }
            FileHandle.standardError.write(data)
            tail.append(String(decoding: data, as: UTF8.self))
        }
        do { try process.run() } catch { throw CoreError.handshakeFailed(error.localizedDescription) }
        self.process = process
        self.stdinPipe = stdin

        let line: String? = try await withThrowingTaskGroup(of: String?.self) { group in
            group.addTask {
                for try await line in stdout.fileHandleForReading.bytes.lines { return line }
                return nil
            }
            group.addTask {
                try await Task.sleep(nanoseconds: 30 * 1_000_000_000)
                return nil
            }
            let first = try await group.next() ?? nil
            group.cancelAll()
            return first
        }
        guard let line, let data = line.data(using: .utf8),
            let handshake = try? JSONDecoder().decode(Handshake.self, from: data)
        else {
            stop()
            let detail = stderrTail.value.trimmingCharacters(in: .whitespacesAndNewlines)
            throw CoreError.handshakeFailed(detail.isEmpty ? "no handshake received" : detail)
        }
        return handshake
    }

    public var isRunning: Bool { process?.isRunning ?? false }

    /// Closing stdin tells the engine to exit; terminate is the backstop.
    public func stop() {
        try? stdinPipe?.fileHandleForWriting.close()
        if let process, process.isRunning {
            DispatchQueue.global().asyncAfter(deadline: .now() + 2) { if process.isRunning { process.terminate() } }
        }
    }
}


/// Keeps the last few KB of the core's stderr for error messages.
final class LockedString: @unchecked Sendable {
    private let lock = NSLock()
    private var text = ""

    func append(_ more: String) {
        lock.lock()
        defer { lock.unlock() }
        text = String((text + more).suffix(4000))
    }

    var value: String {
        lock.lock()
        defer { lock.unlock() }
        return text
    }
}
