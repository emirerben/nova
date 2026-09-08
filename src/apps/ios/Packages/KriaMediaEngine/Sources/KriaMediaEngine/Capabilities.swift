import Foundation

public enum ExportRoute: String, Sendable { case local, cloud }
public struct CapabilityDecision: Equatable, Sendable {
    public var route: ExportRoute; public var missingCapabilities: Set<MediaCapability>; public var reason: String?
    public init(route: ExportRoute, missingCapabilities: Set<MediaCapability> = [], reason: String? = nil) { self.route = route; self.missingCapabilities = missingCapabilities; self.reason = reason }
}
public protocol RendererCapabilityProviding: Sendable { var capabilities: Set<MediaCapability> { get } }
public struct DefaultRendererCapabilities: RendererCapabilityProviding {
    public let capabilities: Set<MediaCapability>
    public init(capabilities: Set<MediaCapability> = Set([.basicComposition, .animatedText, .crossfade, .audioMix, .variableSpeed, .local1080Export])) { self.capabilities = capabilities }
}
public struct CapabilityNegotiator: Sendable {
    public let provider: any RendererCapabilityProviding
    public init(provider: any RendererCapabilityProviding = DefaultRendererCapabilities()) { self.provider = provider }
    public func decide(for recipe: EditRecipe, freeStorageBytes: Int64? = nil, estimatedTemporaryBytes: Int64? = nil, thermalState: ThermalState = .nominal) -> CapabilityDecision {
        let missing = recipe.requiredCapabilities.subtracting(provider.capabilities)
        if !missing.isEmpty { return CapabilityDecision(route: .cloud, missingCapabilities: missing, reason: "Renderer does not support required capabilities") }
        if let freeStorageBytes, let estimatedTemporaryBytes, freeStorageBytes < estimatedTemporaryBytes { return CapabilityDecision(route: .cloud, reason: "Insufficient temporary storage") }
        if thermalState == .serious || thermalState == .critical { return CapabilityDecision(route: .cloud, reason: "Device thermal state is \(thermalState.rawValue)") }
        return CapabilityDecision(route: .local)
    }
}

public enum ThermalState: String, Sendable { case nominal, fair, serious, critical, unknown }

public struct StorageEstimate: Equatable, Sendable {
    public var requiredBytes: Int64
    public init(requiredBytes: Int64) { self.requiredBytes = requiredBytes }
    public static func forAssetBytes(_ sourceBytes: Int64, projectCount: Int = 1) -> StorageEstimate { let multiplier = max(1, projectCount); return StorageEstimate(requiredBytes: sourceBytes * Int64(2 + multiplier) + 100 * 1024 * 1024) }
}

public struct MetricEvent: Codable, Equatable, Sendable {
    public enum Name: String, Codable, Sendable { case previewFPS, droppedFrames, seekLatency, timelineGestureLatency, proxyDuration, proxyUpload, exportDuration, peakMemory, temporaryStorage, thermalChange, batteryImpact, fallback }
    public var name: Name; public var value: Double; public var timestamp: Date; public var projectID: String?
    public init(name: Name, value: Double, timestamp: Date = Date(), projectID: String? = nil) { self.name = name; self.value = value; self.timestamp = timestamp; self.projectID = projectID }
}
public protocol MediaInstrumentation: Sendable { func record(_ event: MetricEvent) }
public final class MetricsCollector: @unchecked Sendable, MediaInstrumentation {
    private let lock = NSLock(); private var events: [MetricEvent] = []
    public init() {}
    public func record(_ event: MetricEvent) { lock.lock(); events.append(event); lock.unlock() }
    public func snapshot() -> [MetricEvent] { lock.lock(); defer { lock.unlock() }; return events }
    public func reset() { lock.lock(); events.removeAll(); lock.unlock() }
}

public struct FPSMeter: Sendable {
    private var frameCount = 0; private var start: ContinuousClock.Instant?
    public init() {}
    public mutating func frame(at now: ContinuousClock.Instant = ContinuousClock.now) -> Double? { if start == nil { start = now }; frameCount += 1; guard let start, now - start >= .seconds(1) else { return nil }; let seconds = Double((now - start).components.seconds) + Double((now - start).components.attoseconds) / 1e18; let fps = Double(frameCount) / max(seconds, 0.0001); self.start = now; frameCount = 0; return fps }
}

public struct SeekLatencyMeter: Sendable {
    private var requested: ContinuousClock.Instant?
    public init() {}
    public mutating func request(at now: ContinuousClock.Instant = ContinuousClock.now) { requested = now }
    public mutating func visible(at now: ContinuousClock.Instant = ContinuousClock.now) -> TimeInterval? { guard let requested else { return nil }; self.requested = nil; let d = now - requested; return Double(d.components.seconds) + Double(d.components.attoseconds) / 1e18 }
}
