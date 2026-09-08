// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "KriaMediaEngine",
    platforms: [
        .iOS(.v18),
        .macOS(.v13)
    ],
    products: [
        .library(name: "KriaMediaEngine", targets: ["KriaMediaEngine"])
    ],
    targets: [
        .target(name: "KriaMediaEngine"),
        .testTarget(name: "KriaMediaEngineTests", dependencies: ["KriaMediaEngine"])
    ]
)
