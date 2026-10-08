import AVFoundation
let a = CommandLine.arguments
let url = URL(fileURLWithPath: a[1]); let start = Double(a[2])!; let dur = Double(a[3])!; let precise = a[4] == "1"; let out = URL(fileURLWithPath: a[5])
let asset = AVURLAsset(url: url, options: precise ? [AVURLAssetPreferPreciseDurationAndTimingKey: true] : nil)
let sem = DispatchSemaphore(value: 0)
Task {
  let track = try await asset.loadTracks(withMediaType: .audio).first!
  print("duration", try await asset.load(.duration).seconds, "trackStart", try await track.load(.timeRange).start.seconds)
  let comp = AVMutableComposition()
  let t = comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)!
  try t.insertTimeRange(CMTimeRange(start: CMTime(seconds: start, preferredTimescale: 600), duration: CMTime(seconds: dur, preferredTimescale: 600)), of: track, at: .zero)
  try? FileManager.default.removeItem(at: out)
  let ex = AVAssetExportSession(asset: comp, presetName: AVAssetExportPresetAppleM4A)!
  ex.outputURL = out; ex.outputFileType = .m4a
  await ex.export()
  print("status", ex.status.rawValue, ex.error as Any)
  sem.signal()
}
sem.wait()
