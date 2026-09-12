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
        .target(name: "CVPX", exclude: ["vendor/LICENSE", "vendor/PATENTS", "vendor/AUTHORS", "vendor/README.kria.md"], publicHeadersPath: "include", cSettings: [.headerSearchPath("vendor"), .unsafeFlags(["-O2"])]),
        .target(name: "KriaMediaEngine", dependencies: ["CVPX"], resources: [.process("Resources")]),
        .testTarget(name: "KriaMediaEngineTests", dependencies: ["KriaMediaEngine"], resources: [.copy("Fixtures")])
    ]
)
