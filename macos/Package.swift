// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "DGXSparkBar",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "DGXSparkBar",
            path: "Sources/DGXSparkBar",
            // Swift 5 mode on purpose: the app is a handful of views around a
            // polling loop, and strict-concurrency ceremony would double its size
            // without making a menu bar icon any safer.
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
