#if DEBUG
import Foundation

/// Mock `SuggestionSetOut` bodies for AI suggestion blocks, shaped like the server's wire (`suggestions` +
/// `digest` beside the legacy `chips`). They feed three readers: the offline chat transport (UI tests and local
/// runs, `KRIA_SUGGESTION_CARDS`), the unit tests that decode them, and SwiftUI previews. Every key is one the
/// generated `openapi.yaml` names; a unit test pins that so the mocks cannot drift from the contract.
enum SuggestionCardsMocks {
    /// A UI test can make its prompts unique (`KRIA_SUGGESTION_CARDS_NONCE`): the offline server's chats outlive a run,
    /// so an old identical message would otherwise pass for the one the test just sent.
    static var promptSuffix: String {
        ProcessInfo.processInfo.environment["KRIA_SUGGESTION_CARDS_NONCE"].map { " \($0)" } ?? ""
    }

    /// The card whose tap the fixture server answers with the "no longer fits" reply, as `plan_pinned_suggestion_turn` does.
    static let staleCardID = "ss_fixture_stale"
    /// The server's English reply for a pin it will not run (`_NO_LONGER_FITS`).
    static let noLongerFitsReply = "This suggestion no longer fits your edit. Nothing was changed, and I'm refreshing the ideas."

    /// One card per kind the registry knows, each with the fields that kind really carries.
    static var everyKind: [[String: Any]] { [
        card("hook", "Add a hook", why: "The opening is a quiet shot", evidence: ["First 3 s have no motion"],
             prompt: "Add this hook to the opening", copy: "I almost missed this view", action: "copy_decision"),
        card("title", "Add a title", why: "Nothing says what this is", evidence: ["No text on screen"],
             prompt: "Add this title", copy: "Sunday in Lisbon", action: "copy_decision"),
        card("captions", "Add captions", why: "People talk in 4 clips", evidence: ["Speech in 4 of 6 clips"],
             prompt: "Add captions", action: "editor_ops"),
        card("music", "Turn the music down", why: "It covers the voices", evidence: ["Music is 9 dB above speech"],
             prompt: "Turn the music down", action: "editor_ops"),
        card("sfx", "Add sound effects", why: "The cuts land on beats", evidence: ["6 cuts"],
             prompt: "Add sound effects", action: "editor_ops"),
        card("transition", "Add transitions", why: "Hard cuts between locations", evidence: ["3 location changes"],
             prompt: "Add transitions between the clips", action: "editor_ops"),
        card("title_animation", "Animate the title", why: "The title just appears", evidence: ["Static title"],
             prompt: "Animate the title", action: "editor_ops"),
        card("pinned_text", "Pin the place name", why: "You named a place", evidence: ["Lisbon"],
             prompt: "Pin Lisbon in the corner", action: "editor_ops"),
        card("opener_clip", "Open on the best shot", why: "Clip 4 is the strongest", evidence: ["Clip 4 sharpest"],
             prompt: "Open on clip 4", action: "editor_ops"),
        card("mute_clip", "Mute clip 3", why: "Wind drowns the audio", evidence: ["Wind in clip 3"],
             prompt: "Mute clip 3, it has wind noise", action: "brief_update", anchor: ["clip_ordinal": 3, "media_id": "m3"]),
        card("filler_words", "Cut the filler words", why: "You say um a lot", evidence: ["14 fillers, 6 s"],
             prompt: "Cut the filler words", action: "item_setting"),
        card("duplicates", "Keep the sharper clip", why: "Clips 2 and 5 are nearly the same", evidence: ["2 and 5 match"],
             prompt: "Remove the blurrier of clips 2 and 5", action: "editor_ops"),
        card("generated_overlay", "Add a generated image", why: "The pasta is only described", evidence: ["Mentioned, never shown"],
             prompt: "Add a generated image of fresh pasta", action: "async_generate", cost: 0.04),
    ] }

    /// The set a finished video offers: five cards, one per interesting shape (exact text, measured fact, cost, plain).
    static func ready(setID: String = "sg_fixture_ready_v2") -> [String: Any] {
        set(setID, cards: [everyKind[0], everyKind[9], everyKind[10], everyKind[11], everyKind[12]])
    }

    /// Long Turkish text with the dotted and dotless i, in every field that wraps.
    static func turkish() -> [String: Any] {
        set("sg_fixture_tr", cards: [
            card("hook", "İlk saniyeye bir kanca ekle", why: "Açılış sakin, izleyici ilk üç saniyede kayabilir",
                 evidence: ["İlk 3 sn hareketsiz"], prompt: "Bu kancayı açılışa ekle",
                 copy: "Şu manzarayı kaçırıyordum neredeyse, ışıklar yanınca ıslak taşlar parladı", action: "copy_decision"),
            card("mute_clip", "3. klibi sessize al", why: "Rüzgâr sesi konuşmayı bastırıyor",
                 evidence: ["3. klipte rüzgâr"], prompt: "3. klibi sessize al, rüzgâr sesi var", action: "brief_update"),
        ])
    }

    /// A newer server: a kind and an action type this app has never heard of, plus keys it ignores, plus one broken
    /// card (no title) that must be dropped alone.
    static func unknown() -> [String: Any] {
        var future = card("color_grade_v9", "Warm up the grade", why: "The footage reads cold", evidence: ["Cool cast in 5 clips"],
                          prompt: "Warm up the colors", action: "grade_recipe")
        future["future_field"] = ["ignored": true]
        var broken = card("captions", "", prompt: "Add captions")
        broken["id"] = "ss_fixture_broken"
        return set("sg_fixture_unknown", cards: [future, broken])
    }

    /// A set whose first card the server will answer with "no longer fits" when tapped.
    static func stale() -> [String: Any] {
        set("sg_fixture_stale", cards: [
            card("mute_clip", "Mute clip 3", why: "Wind drowns the audio", evidence: ["Wind in clip 3"],
                 prompt: "Mute clip 3, it has wind noise", action: "brief_update", id: staleCardID),
            card("captions", "Add captions", why: "People talk in 4 clips", evidence: ["Speech in 4 of 6 clips"],
                 prompt: "Add captions", action: "editor_ops"),
        ])
    }

    /// What the server answers after it declined the stale card: a new `set_id` without it.
    static func refreshed() -> [String: Any] {
        set("sg_fixture_refreshed", cards: [
            card("captions", "Add captions", why: "People talk in 4 clips", evidence: ["Speech in 4 of 6 clips"],
                 prompt: "Add captions", action: "editor_ops"),
        ])
    }

    /// Ready, with nothing to suggest: the chips-free, card-free outcome of a clean edit.
    static func empty() -> [String: Any] { set("sg_fixture_empty", cards: []) }

    // MARK: Builders

    static func card(_ kind: String, _ title: String, why: String? = nil, evidence: [String] = [], prompt: String,
                     copy: String? = nil, action: String = "editor_ops", cost: Double? = nil,
                     anchor: [String: Any] = [:], id: String? = nil) -> [String: Any] {
        [
            "id": id ?? "ss_fixture_\(kind)", "kind": kind, "title": title, "why": why ?? "", "evidence": evidence,
            "prompt": prompt + promptSuffix, "copy_text": copy ?? NSNull(), "rank": 1, "anchor": anchor, "action_type": action,
            "cost_usd": cost ?? NSNull(), "requires_protocol": NSNull(),
        ]
    }

    /// Ranks are 1...n in the order given, like the server's `_ranked` validator.
    static func set(_ setID: String, cards: [[String: Any]], state: String = "ready") -> [String: Any] {
        let ranked = cards.enumerated().map { index, card -> [String: Any] in
            var card = card
            card["rank"] = index + 1
            return card
        }
        return [
            "schema_version": 1, "set_id": setID, "state": state, "status": "ok", "understanding": "done",
            "retry_after_s": NSNull(), "wording": "agent",
            "anchor": ["media_digest": "fixture", "manifest_hash": NSNull(), "render_generation_id": NSNull(), "variant_id": NSNull()],
            "chips": [] as [[String: Any]], "suggestions": ranked,
            "digest": ["media_digest": "fixture", "snapshot_hash": "snap", "brief_version": 1, "signals_version": "v1",
                       "state": state, "language": "en", "prompt_version": "fixture"],
        ]
    }
}
#endif
