import Foundation

extension CameraPulse {
    public static func scale(pulses: [CameraPulse], time: Double, dimension: Int) -> Double {
        let amount = min(0.12, pulses.reduce(0.0) { value, pulse in
            guard time >= pulse.start && time < pulse.end else { return value }
            let wave = sin(.pi * (time - pulse.start) / (pulse.end - pulse.start))
            return value + pulse.intensity * wave * wave
        })
        // FFmpeg rounds each scaled dimension down to an even pixel count.
        return floor(Double(dimension) * (1 + amount) / 2) * 2 / Double(dimension)
    }
}
