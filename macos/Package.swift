// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Watcher",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "WatcherApp", targets: ["WatcherApp"]),
        .library(name: "WatcherKit", targets: ["WatcherKit"]),
    ],
    targets: [
        .target(name: "WatcherKit"),
        .executableTarget(name: "WatcherApp", dependencies: ["WatcherKit"]),
        .testTarget(name: "WatcherKitTests", dependencies: ["WatcherKit"]),
    ]
)
