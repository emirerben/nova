import SwiftUI
import UIKit
import XCTest
@testable import Kria

/// KRI-120: a message's long-press Copy menu gives VoiceOver no action, so
/// each message also needs Copy as an accessibility action. XCUITest can't
/// list custom actions, so this reads them in process.
@MainActor
final class ChatMessageCopyTests: XCTestCase {
    func testPromptsAndRepliesExposeCopyAsAnAccessibilityAction() throws {
        let restoreAutomation = try enableAccessibilityAutomation()
        defer { restoreAutomation() }

        let messages = [
            (ChatTranscriptMessage(id: "sent", role: .user, content: "Keep the candid reactions."), "You: Keep the candid reactions."),
            (ChatTranscriptMessage(id: "pending", role: .user, content: "End on the sunset.", isPending: true), "You: End on the sunset."),
            (
                ChatTranscriptMessage(
                    id: "reply",
                    role: .assistant,
                    content: "Should the edit end on the sunset?",
                    options: ["End on the sunset", "End on the arrival"],
                    recommendedOption: "End on the sunset"
                ),
                "Kria: Should the edit end on the sunset?"
            ),
        ]

        for (message, label) in messages {
            let elements = accessibilityElements(
                rendering: ChatMessageRow(message: message, onSelectOption: { _ in }),
                until: label
            )
            let element = try XCTUnwrap(
                elements.first { $0.accessibilityLabel == label },
                "No element labelled \(label) in \(elements.map { $0.accessibilityLabel ?? "" })"
            )
            let copyActions = (element.accessibilityCustomActions ?? []).filter { $0.name == "Copy" }
            XCTAssertEqual(copyActions.count, 1)
            let copy = try XCTUnwrap(copyActions.first)

            UIPasteboard.general.string = ""
            if let handler = copy.actionHandler {
                _ = handler(copy)
            } else {
                _ = (copy.target as? NSObject)?.perform(copy.selector, with: copy)
            }
            XCTAssertEqual(UIPasteboard.general.string, message.content)

            // Option chips keep their own labels and plain taps.
            for option in message.options {
                let expected = option == message.recommendedOption ? "\(option) (recommended)" : option
                let chip = try XCTUnwrap(elements.first { $0.accessibilityLabel == expected }, "Missing option chip \(expected)")
                XCTAssertTrue(chip.accessibilityTraits.contains(.button))
                XCTAssertNil(chip.accessibilityCustomActions?.first { $0.name == "Copy" })
            }
        }
    }

    /// SwiftUI builds no accessibility tree until an assistive client asks for
    /// one, so turn on the same automation switch XCUITest uses (the
    /// technique AccessibilitySnapshot relies on).
    private func enableAccessibilityAutomation() throws -> () -> Void {
        let root = ProcessInfo.processInfo.environment["IPHONE_SIMULATOR_ROOT"] ?? ""
        let library = try XCTUnwrap(dlopen(root + "/usr/lib/libAccessibility.dylib", RTLD_NOW), "libAccessibility is unavailable")
        typealias IsEnabled = @convention(c) () -> Int32
        typealias SetEnabled = @convention(c) (Int32) -> Void
        let isEnabled = unsafeBitCast(try XCTUnwrap(dlsym(library, "_AXSAutomationEnabled")), to: IsEnabled.self)
        let setEnabled = unsafeBitCast(try XCTUnwrap(dlsym(library, "_AXSSetAutomationEnabled")), to: SetEnabled.self)
        let previous = isEnabled()
        setEnabled(1)
        return { setEnabled(previous) }
    }

    /// SwiftUI fills the tree in asynchronously, so poll until `label` appears
    /// (or give up after a few seconds) instead of waiting a fixed time.
    private func accessibilityElements(rendering view: some View, until label: String) -> [NSObject] {
        let host = UIHostingController(rootView: view)
        let window = UIWindow(frame: CGRect(x: 0, y: 0, width: 390, height: 844))
        window.rootViewController = host
        window.makeKeyAndVisible()
        defer { window.isHidden = true }
        host.view.layoutIfNeeded()

        func collect() -> [NSObject] {
            var found: [NSObject] = []
            func visit(_ node: NSObject) {
                if node.isAccessibilityElement { found.append(node) }
                var children = (node.accessibilityElements as? [NSObject]) ?? []
                if children.isEmpty, let view = node as? UIView { children = view.subviews }
                children.forEach(visit)
            }
            visit(host.view)
            return found
        }

        let deadline = Date().addingTimeInterval(5)
        var elements = collect()
        while !elements.contains(where: { $0.accessibilityLabel == label }), Date() < deadline {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
            elements = collect()
        }
        return elements
    }
}
