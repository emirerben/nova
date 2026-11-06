import Foundation

/// The content of the What's New guide. Adding an update edits this file alone: append a release at the top
/// (days come from the `## [version] - date` headers in CHANGELOG.md) and gate anything that is not on for
/// every account. Quote button and screen names exactly as the app shows them.
enum WhatsNewReleases {
    static let all: [WhatsNewRelease] = [
        WhatsNewRelease(
            day: WhatsNewDay(2026, 11, 1),
            features: [
                WhatsNewFeature(
                    id: "lipsync",
                    symbol: "music.mic",
                    title: "Make a lip-sync video",
                    benefit: "Add a song, then your takes of you singing along. Kria lines them up with the music.",
                    steps: [
                        "Tap the menu button at the top left (Open projects).",
                        "Tap New chat.",
                        "Swipe the cards to the last one, Lip-sync, and tap it.",
                        "Add your song first, then your clips.",
                    ],
                    requires: .lipsyncFormat,
                    tryIt: nil
                ),
                WhatsNewFeature(
                    id: "text-save-all",
                    symbol: "checkmark.circle",
                    title: "Save all your text styles at once",
                    benefit: "Edit as many text types as you like, then save once. Each type shows whether it follows All text or has a change of its own.",
                    steps: [
                        "In Settings, open Text preferences.",
                        "Pick a type: All text, Title, Label, Corner text, Caption or Lyric.",
                        "Change its fonts, placement, color or effects. “From All text” means it follows your All text choice.",
                        "Tap Save preferences to save every type you edited.",
                    ],
                    requires: .textPreferences,
                    tryIt: .textPreferences
                ),
            ],
            fixes: [
                WhatsNewFix(text: "The clip picker walks you through every clip group before Done, even a group that was already ticked for you.",
                            requires: .clipSelectionQuestions),
            ]
        ),
        WhatsNewRelease(
            day: WhatsNewDay(2026, 10, 31),
            features: [
                WhatsNewFeature(
                    id: "text-preview-frame",
                    symbol: "photo.on.rectangle",
                    title: "Preview your text types on a video frame",
                    benefit: "Text preferences shows each text type on a sample video frame, so you can see a change before you save it.",
                    steps: [
                        "In Settings, open Text preferences.",
                        "Pick a type at the top.",
                        "Tap the frame to see it larger.",
                    ],
                    requires: .textPreferences,
                    tryIt: .textPreferences
                ),
            ],
            fixes: []
        ),
        WhatsNewRelease(
            day: WhatsNewDay(2026, 10, 30),
            features: [],
            fixes: [
                WhatsNewFix(text: "Renaming a chat while Kria is planning no longer throws away the finished plan.", requires: nil),
                WhatsNewFix(text: "A shot’s “for 3 seconds” no longer sets the length of the whole video.", requires: nil),
                WhatsNewFix(text: "Choosing “None of these” in the clip picker no longer stops your draft.", requires: .clipSelectionQuestions),
            ]
        ),
        WhatsNewRelease(
            day: WhatsNewDay(2026, 10, 29),
            features: [
                WhatsNewFeature(
                    id: "message-receipt",
                    symbol: "paperplane",
                    title: "See your message the moment you send it",
                    benefit: "Your message appears in the chat right away. If Kria can’t confirm it, you’ll see “Message not confirmed” and a Retry message button.",
                    steps: [],
                    requires: nil,
                    tryIt: nil
                ),
            ],
            fixes: [
                WhatsNewFix(text: "Send no longer stays greyed out after a crash interrupts an upload.", requires: nil),
                WhatsNewFix(text: "Use this idea in Camera roll now opens that idea’s chat.", requires: .cameraRoll),
            ]
        ),
        WhatsNewRelease(
            day: WhatsNewDay(2026, 10, 28),
            features: [
                WhatsNewFeature(
                    id: "format-examples",
                    symbol: "play.rectangle",
                    title: "Watch examples on the format cards",
                    benefit: "A new chat shows a short example video on the format cards. Tap the pause button on a card to stop it.",
                    steps: [
                        "Tap the menu button at the top left (Open projects).",
                        "Tap New chat.",
                        "Swipe through the cards under “What are we making?”.",
                    ],
                    requires: nil,
                    tryIt: nil
                ),
                WhatsNewFeature(
                    id: "camera-roll-events",
                    symbol: "photo.stack",
                    title: "Plan videos around an event",
                    benefit: "Group what you filmed into events, like a trip or a weekend. Kria suggests videos to make from each one.",
                    steps: [
                        "Tap the menu button at the top left (Open projects).",
                        "Open Camera roll.",
                        "Tap Choose photos and videos, or New event if you already have one.",
                        "Open an event to see its ideas, then tap Use this idea.",
                    ],
                    requires: .cameraRoll,
                    tryIt: nil
                ),
            ],
            fixes: [
                WhatsNewFix(text: "Videos you finished before an app update still play afterward.", requires: nil),
                WhatsNewFix(text: "In the editor’s Kria chat, Open editor switches to the newer version and asks before it drops your edits.", requires: nil),
                WhatsNewFix(text: "Dragging a text-selection handle in the message box moves the selection instead of opening the projects menu.", requires: nil),
            ]
        ),
    ]
}
