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
        if recipe.audio.duckOriginalDuringMusic {
            return CapabilityDecision(route: .cloud, reason: "Audio ducking is not supported by this renderer")
        }
        let missing = recipe.effectiveCapabilities.subtracting(provider.capabilities)
        if !missing.isEmpty { return CapabilityDecision(route: .cloud, missingCapabilities: missing, reason: "Renderer does not support required capabilities") }
        if let freeStorageBytes, let estimatedTemporaryBytes, freeStorageBytes < estimatedTemporaryBytes { return CapabilityDecision(route: .cloud, reason: "Insufficient temporary storage") }
        if thermalState == .serious || thermalState == .critical { return CapabilityDecision(route: .cloud, reason: "Device thermal state is \(thermalState.rawValue)") }
        guard recipe.rendererVersion == "kria-ios-\(recipe.schemaVersion)" else {
            // Distinct from "Invalid edit recipe" below: this build's renderer
            // doesn't speak this recipe's version at all, vs. a validate()
            // failure against a version it does understand. Both used to read
            // as the same generic string; `DeviceRenderFailureReasonCode
            // .forRouteDecision` keys off "renderer version" to route this to
            // `.rendererOutdated` instead of the unsupported-FEATURE bucket.
            return CapabilityDecision(route: .cloud, reason: "Unsupported renderer version")
        }
        do { try recipe.validate() } catch let error as RecipeError {
            if case .unsupportedSchema = error {
                // `recipe.validate()` itself detected a schema newer than this
                // build supports (as opposed to a specific unsupported feature
                // within an otherwise-valid, understood schema) — same
                // "update the app" bucket as the renderer-version guard above.
                return CapabilityDecision(route: .cloud, reason: "Unsupported renderer version for this recipe schema")
            }
            return CapabilityDecision(route: .cloud, reason: "Invalid edit recipe")
        } catch {
            return CapabilityDecision(route: .cloud, reason: "Invalid edit recipe")
        }
        return CapabilityDecision(route: .local)
    }
}

public enum ThermalState: String, Sendable { case nominal, fair, serious, critical, unknown }

public struct StorageEstimate: Equatable, Sendable {
    public var requiredBytes: Int64
    public init(requiredBytes: Int64) { self.requiredBytes = requiredBytes }

    /// Bytes of free space needed to safely produce a device render's output.
    ///
    /// This used to be `(pendingProjects + 2) × sourceBytes + 100MB` —
    /// treating the *source* footage as needing to be duplicated multiple
    /// times over. That's wrong: the originals already live on the phone
    /// before a render starts, and a render only ever writes ONE new file,
    /// the exported output. A project cut from several GB of 4K/ProRes
    /// clips would fail this gate on a phone with completely ordinary free
    /// space (KRI-118).
    ///
    /// This instead scales with the estimated OUTPUT size. Kria's exports
    /// are always short-form (target sub-60s, see root CLAUDE.md) encoded at
    /// `LocalExportPreset.default`'s H.264 bitrate, so one export is bounded
    /// to roughly `durationS × videoBitrate / 8` bytes regardless of how much
    /// source footage it was cut from. `pendingProjects` scales that bounded
    /// per-export estimate — each queued render eventually writes its own
    /// output — not the (irrelevant) source bytes. The 1.2x headroom + flat
    /// 200MB floor cover encoder/container overhead and any other transient
    /// files (thumbnails, muxed intermediates) a render touches.
    public static func forEstimatedOutput(durationS: TimeInterval, pendingProjects: Int = 0) -> StorageEstimate {
        let safeDuration = durationS.isFinite ? max(0, durationS) : Double.greatestFiniteMagnitude
        let bytesPerSecond = Double(LocalExportPreset.default.videoBitrate) / 8.0
        // Double arithmetic (not Int) so a pathological `pendingProjects` (e.g. `.max`)
        // overshoots into `required >= Int64.max` below instead of trapping on overflow.
        let exports = max(1.0, Double(pendingProjects) + 1.0)
        let estimatedOutputBytes = safeDuration * bytesPerSecond * exports
        let required = estimatedOutputBytes * 1.2 + 200 * 1024 * 1024
        guard required.isFinite, required < Double(Int64.max) else { return StorageEstimate(requiredBytes: .max) }
        return StorageEstimate(requiredBytes: Int64(required))
    }
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
