#if DEBUG
import SwiftUI

enum NativeSplitPickerFixture {
    static var isActive: Bool { ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-split-picker") }
    /// `KRIA_SPLIT_PICKER_LIBRARY_DELAY_S` sets how long the library check stays unanswered.
    static var libraryAnswerDelay: Duration {
        .seconds(Double(ProcessInfo.processInfo.environment["KRIA_SPLIT_PICKER_LIBRARY_DELAY_S"] ?? "") ?? 3)
    }
}
#endif
