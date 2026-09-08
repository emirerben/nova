import Foundation

public enum ExportRecovery {
    public static func canResume(_ checkpoint: ExportCheckpoint) -> Bool { checkpoint.status == .queued || checkpoint.status == .exporting }
    public static func isTerminal(_ checkpoint: ExportCheckpoint) -> Bool { !canResume(checkpoint) }
    public static func nextAction(for checkpoint: ExportCheckpoint) -> ExportStatus {
        switch checkpoint.status { case .queued, .exporting: return .exporting; case .cancelled, .failed: return .needsCloudFallback; case .completed, .needsCloudFallback: return checkpoint.status }
    }
}
