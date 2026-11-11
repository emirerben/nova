#if DEBUG
import Foundation

/// KRI-1239: Qendresa's held Talking draft from the Q2 run 2 (prod thread, 2026-11-08), replayed offline.
/// TestFlight 1926 was killed by the iOS watchdog (0x8BADF00D, 10 s) about 26 s after "Make it without these",
/// while Kria was still thinking, with the main thread inside the transcript's `ForEach`, hashing strings.
///
/// `KRIA_CHAT_REPLAY=held-draft` makes a new chat open on that thread's real events (sequence 0-11, copied verbatim
/// from the events export and mapped to the API shape): her prompt, the first hold with 12 requirement receipts, her
/// first "Make it without these", the wording question, the second hold. Tapping "Make it without these" again starts a
/// turn that thinks for `KRIA_CHAT_REPLAY_POLLS` delta polls (default 30, ~1 s each; prod took 100 s) and then lands the
/// real `draft_applied` reply (event 13, 11 receipts).
///
/// The earlier holds never reproduced the freeze because they had no thinking text. Production streams Kria's
/// thoughts while a turn runs (`THOUGHT_SUMMARIES_ENABLED` is on): every earlier turn has completed thoughts and the
/// running one streams new text every second. The replay serves `KRIA_CHAT_REPLAY_THOUGHTS` summaries per turn
/// (default 3, one per model attempt) of `KRIA_CHAT_REPLAY_THOUGHT_KB` KB each (default 24 KB, the size of an 8k-token
/// Gemini thought), written like Gemini's (bold step headings, prose with curly quotes and dashes).
enum HeldDraftReplayFixture {
    private static let env = ProcessInfo.processInfo.environment
    static var isEnabled: Bool { env["KRIA_CHAT_REPLAY"] == "held-draft" }
    static var polls: Int { Int(env["KRIA_CHAT_REPLAY_POLLS"] ?? "") ?? 30 }
    static var thoughtsPerTurn: Int { Int(env["KRIA_CHAT_REPLAY_THOUGHTS"] ?? "") ?? 3 }
    static var thoughtKB: Int { Int(env["KRIA_CHAT_REPLAY_THOUGHT_KB"] ?? "") ?? 24 }

    /// Events 0-11 and the thread state they leave (her talking take attached, Talk to camera chosen).
    static func seededThread() -> (events: [[String: Any]], state: [String: Any]) {
        let events = recorded["history"] as? [[String: Any]] ?? []
        let media = events.first { $0["event_type"] as? String == "media_added" }
            .flatMap { ($0["payload"] as? [String: Any])?["media"] as? [[String: Any]] } ?? []
        return (events, ["format": "talking_to_camera", "media": media])
    }

    /// Event 13, the reply to the second "Make it without these", re-stamped onto the replayed turn.
    static func answer(turnID: String, sequence: Int, revision: Int) -> [String: Any] {
        var event = recorded["answer"] as? [String: Any] ?? [:]
        var payload = event["payload"] as? [String: Any] ?? [:]
        payload["turn_id"] = turnID
        event["payload"] = payload
        event["id"] = UUID().uuidString
        event["sequence"] = sequence
        event["revision"] = revision
        return event
    }

    /// Client request ids of the turns that already finished before the tap (her prompt and two answers).
    static var finishedRequestIDs: [String] {
        (recorded["history"] as? [[String: Any]] ?? [])
            .filter { $0["event_type"] as? String == "user_message" }
            .compactMap { $0["client_event_id"] as? String }
    }

    /// One turn's thoughts. `progress` 0...1 is how much of a running turn's text has streamed so far.
    static func thoughts(requestID: String, turn: Int, running: Bool, progress: Double = 1) -> [[String: Any]] {
        (0..<thoughtsPerTurn).map { attempt in
            let full = thoughtText(seed: turn * 10 + attempt, bytes: thoughtKB * 1024)
            // A running turn finishes its earlier attempts first; the last one is still streaming.
            let streaming = running && attempt == thoughtsPerTurn - 1
            let text = streaming ? String(full.prefix(max(200, Int(Double(full.count) * progress)))) : full
            var row: [String: Any] = [
                "id": "replay-thought-\(turn)-\(attempt)", "client_request_id": requestID,
                "status": streaming ? "streaming" : "completed", "text": text,
                "started_at": "2026-11-08T13:5\(attempt):00Z",
            ]
            if !streaming { row["completed_at"] = "2026-11-08T13:5\(attempt):40Z"; row["duration_ms"] = 40_000 }
            return row
        }
    }

    /// Text shaped like a Gemini thought summary: a bold step heading, then prose that quotes her asks with curly
    /// quotes and dashes (not ASCII, so Swift hashes it the slow, normalising way, as on the phone).
    static func thoughtText(seed: Int, bytes: Int) -> String {
        let headings = ["Weighing the GitHub logo", "Checking the brief against the draft", "Planning the simpler version",
                        "Timing the repo labels", "Placing the speaker frame", "Confirming the caption style"]
        let asks = recordedAsks
        var text = ""
        var index = seed
        while text.utf8.count < bytes {
            let heading = headings[index % headings.count]
            let ask = asks.isEmpty ? "the request" : asks[index % asks.count]
            text += "**\(heading)**\n\nI’m looking at “\(ask)” again — the creator’s brief says it, and the draft’s receipts "
                + "say it’s \(index.isMultiple(of: 3) ? "met" : "only partly met"). I’ll keep what’s done, leave out what isn’t "
                + "possible, and say so plainly in the reply; nothing here should change her words or her take’s timing (step \(index)).\n\n"
            index += 1
        }
        return text
    }

    /// The brief at version 2: every ask the receipts name, worded as in the reply's done list.
    static func brief(threadID: String) -> [String: Any] {
        let answer = recorded["answer"] as? [String: Any] ?? [:]
        let ids = ((answer["payload"] as? [String: Any])?["requirement_receipts"] as? [[String: Any]] ?? [])
            .compactMap { $0["requirement_id"] as? String }
        let requirements = (["r2"] + ids).enumerated().map { index, id -> [String: Any] in
            ["id": id, "kind": "other", "description": index < recordedAsks.count ? recordedAsks[index] : "Request \(index + 1)"]
        }
        return ["thread_id": threadID, "version": 2, "requirements": requirements, "requirement_receipts": [Any]()]
    }

    /// Her asks, as the done list of event 13 words them ("Done: …", "Will do at render: …"), GitHub logo first.
    private static let recordedAsks: [String] = {
        let content = (recorded["answer"] as? [String: Any])?["content"] as? String ?? ""
        let lines = content.split(separator: "\n").map(String.init).filter { $0.hasPrefix("- ") }
        let asks = lines.compactMap { line -> String? in
            guard let colon = line.firstIndex(of: ":") else { return nil }
            return line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        guard let last = asks.last else { return [] }
        return [last] + asks.dropLast()
    }()

    private static var recorded: [String: Any] {
        (try? JSONSerialization.jsonObject(with: Data(recordedJSON.utf8))) as? [String: Any] ?? [:]
    }

    private static let recordedJSON = #"""
{
 "history": [
  {
   "id": "76084f09-e97c-44b4-a5b7-60474b3d7a10",
   "sequence": 0,
   "revision": 1,
   "role": "system",
   "event_type": "thread_created",
   "content": "",
   "payload": {},
   "created_at": "2026-11-08T13:38:07Z",
   "client_event_id": "9278261B-78A6-4C6B-9880-65BADE71FB98"
  },
  {
   "id": "5166c62f-5bc4-4720-a725-c3b2de9339d6",
   "sequence": 1,
   "revision": 2,
   "role": "assistant",
   "event_type": "format_prompt",
   "content": "What are we making? Pick a format and we’ll shape it together.",
   "payload": {
    "kind": "select_format",
    "formats": {
     "lipsync": "montage",
     "montage": "montage",
     "narrated": "narrated_planned",
     "talking_to_camera": "subtitled"
    }
   },
   "created_at": "2026-11-08T13:38:07Z"
  },
  {
   "id": "562d3947-e194-49d4-907b-ed66d3485ace",
   "sequence": 2,
   "revision": 3,
   "role": "user",
   "event_type": "action_select_format",
   "content": "",
   "payload": {
    "action": "select_format",
    "format": "talking_to_camera"
   },
   "created_at": "2026-11-08T13:38:10Z",
   "client_event_id": "602C1C89-CE11-4A8C-BC2E-FFC6E91AB717"
  },
  {
   "id": "4a1e672c-3369-42aa-b949-f2ff86ab8076",
   "sequence": 3,
   "revision": 4,
   "role": "user",
   "event_type": "media_added",
   "content": "",
   "payload": {
    "media": [
     {
      "kind": "video",
      "filename": "DF18E689-8096-46B5-89E7-4F0B010B62C4-Talking take.mp4",
      "media_id": "analysis-proxy-ios-6792FC08-0B3D-4E13-AE29-3FF7C43A9AFE.mp4",
      "duration_s": 48.8,
      "size_bytes": 5106029,
      "content_type": "video/mp4",
      "upload_contract": {
       "proxy": {
        "width": 320,
        "height": 568,
        "original": {
         "kind": "video",
         "width": 720,
         "height": 1280,
         "sha256": "f364f6b54825f895bad193e1fd32dea8858b1cf2276e830aa84b1652fd28e7cc",
         "has_audio": true,
         "byte_count": 18891743,
         "duration_s": 48.8,
         "orientation_degrees": 0
        },
        "duration_s": 48.8,
        "frame_rate": 30.0,
        "timing_version": 1,
        "orientation_degrees": 0
       },
       "purpose": "analysis_proxy"
      },
      "library_asset_id": "04e904e1-c476-453a-9ab3-057a3d74b833",
      "library_source_id": "b70d9ba8-4c10-4b97-8891-2030905fc0e8"
     }
    ],
    "media_count": 1
   },
   "created_at": "2026-11-08T13:41:03Z",
   "client_event_id": "ios-attach-6792FC08-0B3D-4E13-AE29-3FF7C43A9AFE"
  },
  {
   "id": "a44dc145-0217-4f0d-9dcd-bf9412fe265a",
   "sequence": 4,
   "revision": 5,
   "role": "user",
   "event_type": "user_message",
   "content": "Start with me full screen and the title \"5 PROJECTS / the AI builder community / can't stop talking about!!!!\" in bold caps, then serif, then handwritten, with the GitHub logo above it. After that, put my screen recordings full screen and me cut out (no background) small in the bottom right corner while I talk. Zoom into the recordings so the text is readable, and show the right one when I talk about each repo. Show the trending page when I talk about what's hot this week. Add a label when each one starts: \"1. Spec-kit\", \"2. Impeccable\", \"3. TradingAgents\", \"4. Agents\", \"5. Claude code game studio\". Captions word by word in a light grey box near the bottom. Cut all my pauses. End with \"comment REPO\" and an arrow. Only my voice.",
   "payload": {
    "turn_id": "d7f545fe-c2f1-4751-b84b-24cb3b6b0009",
    "turn_status": "pending",
    "request_digest": "8e9bb14a49ea5cd5d6a7c6541064587984ddd2c38a40ed27f3e1918869ad42a4",
    "client_protocol": 45,
    "runtime_version": 2,
    "request_outcome_link": {
     "relation": "original",
     "link_status": "known",
     "client_build": "1926",
     "client_version": "0.1.0",
     "original_known": true,
     "schema_version": 1,
     "client_protocol": 45,
     "request_turn_id": "d7f545fe-c2f1-4751-b84b-24cb3b6b0009",
     "server_revision": "5b97e9f55a339e54071c6f76a5e19bc6aa1af9d3",
     "intent_revision_turn_id": "d7f545fe-c2f1-4751-b84b-24cb3b6b0009"
    },
    "marks_protected_regions": true,
    "answers_moment_questions": true,
    "answers_batched_questions": true
   },
   "created_at": "2026-11-08T13:45:26Z",
   "client_event_id": "1E587BC8-E05B-4E88-A84A-31D05ABFE582"
  },
  {
   "id": "aeca2a51-91b4-419e-88f4-873e1460fcad",
   "sequence": 5,
   "revision": 6,
   "role": "system",
   "event_type": "thread_title_generated",
   "content": "",
   "payload": {
    "title": "5 AI Builder Community Projects"
   },
   "created_at": "2026-11-08T13:45:27Z"
  },
  {
   "id": "5d33d3a4-0d0c-4611-b527-54a2af30dc74",
   "sequence": 6,
   "revision": 7,
   "role": "assistant",
   "event_type": "assistant_response",
   "content": "I can't make this yet:\n- GitHub logo above the title (\"GitHub logo\"): That exact text isn't in this draft.\nI held this first draft back. The simpler version leaves out: GitHub logo above the title (\"GitHub logo\"). Should I make it without these, or do you want to change your request?",
   "payload": {
    "turn_id": "d7f545fe-c2f1-4751-b84b-24cb3b6b0009",
    "turn_value": "question",
    "receipt_ids": [],
    "next_actions": [],
    "brief_coverage": {
     "stage": "draft",
     "reason": "approval_requirements_not_ready",
     "enforced_ids": [],
     "retrieved_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "applicable_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "unresolved_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "approval_readiness": {
      "ready": false,
      "digest": "f46cdd63fb557f03127e2cee1dc31581ba7bf5fe16b45b5c4aaeed560dfdea0d",
      "brief_digest": "462b64db9d2f6d2ffea9a8f579901fb3c706232a7e5474edc19558cc517573db",
      "media_digest": "4c630510f9442821f44b8ece1f405a1fd79920b8ad99d6409478ccfbd59a0328",
      "requirements": [
       {
        "reason": null,
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r1"
       },
       {
        "reason": "That exact text isn't in this draft.",
        "disposition": "needs_choice",
        "evidence_owner": "brief_checks",
        "requirement_id": "r2"
       },
       {
        "reason": "5 photos or videos take turns behind you; you're small at the bottom right. I'll zoom in on the text when it's made; I can't confirm it before then. You'll start full screen in your own room, then you're small in the bottom-right corner when you start on your first recording.",
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r3"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r4"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r5"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r6"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r7"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r8"
       },
       {
        "reason": null,
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r9"
       },
       {
        "reason": "If Clean up speech is offered when you approve, choose it and every pause is cut down to about 0.3 s",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r10"
       },
       {
        "reason": "I'll draw a curly arrow next to your ending text",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r11"
       },
       {
        "reason": "No music, as asked.",
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r12"
       }
      ],
      "brief_version": 1,
      "policy_problem": null,
      "schema_version": 1,
      "protected_marks": true,
      "strategy_digest": "7564b2b35ad0e346cce9c081f7078831bd48994bc910c52d8bd053f866031c90",
      "capabilities_digest": "191471b3b995a7fcc6764459c7763a6f9497907f6bc57d4a054b6f3196040426",
      "prepared_evidence_digest": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
     }
    },
    "schema_version": 2,
    "choice_question": {
     "options": [
      {
       "key": "change_request",
       "label": "Change my request",
       "aliases": [
        "Change my request",
        "İsteğimi değiştir",
        "Try a different approach",
        "Farklı bir yol dene"
       ],
       "description": "Nothing is made yet; tell me what to do differently.",
       "recommended": false
      },
      {
       "key": "simpler_version",
       "label": "Make it without these",
       "aliases": [
        "simpler version",
        "make it more basic",
        "skip, decide for me",
        "Make it without these",
        "Bunlar olmadan hazırla",
        "Make this simpler version",
        "Daha basit sürümü hazırla"
       ],
       "description": "Leaves out: GitHub logo above the title (\"GitHub logo\")",
       "recommended": false
      }
     ],
     "version": 1,
     "conflict": "simplification_requires_choice",
     "question_id": "4e569a98-ebb8-4479-93ea-61a69a8b2845",
     "brief_version": 1,
     "allow_free_text": true,
     "omitted_requirement_ids": [
      "r2"
     ]
    },
    "requirement_receipts": [
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r1",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "That exact text isn't in this draft.",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r2",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r3",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r4",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r5",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r6",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r7",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r8",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r9",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "Choose Clean up speech when you approve and every pause is cut down to about 0.3 s",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r10",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll draw a curly arrow next to your ending text",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r11",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r12",
      "inferred_labels": [],
      "target_media_ids": []
     }
    ]
   },
   "created_at": "2026-11-08T13:47:25Z"
  },
  {
   "id": "d7838494-3939-44ce-a7a6-c43c26f34a07",
   "sequence": 7,
   "revision": 8,
   "role": "user",
   "event_type": "user_message",
   "content": "Make it without these",
   "payload": {
    "turn_id": "7d1e88ff-24a1-4883-9fc2-d5fe1cb514a5",
    "turn_status": "pending",
    "request_digest": "97cb86f2533007a481cfdbc5bf09a41675d6de4df47a3406a14aea1f4aa44b95",
    "client_protocol": 45,
    "runtime_version": 2,
    "choice_selection": {
     "option_key": "simpler_version",
     "question_id": "4e569a98-ebb8-4479-93ea-61a69a8b2845"
    },
    "request_outcome_link": {
     "relation": "recovery",
     "link_status": "known",
     "client_build": "1926",
     "client_version": "0.1.0",
     "original_known": true,
     "parent_turn_id": "d7f545fe-c2f1-4751-b84b-24cb3b6b0009",
     "schema_version": 1,
     "client_protocol": 45,
     "request_turn_id": "d7f545fe-c2f1-4751-b84b-24cb3b6b0009",
     "server_revision": "5b97e9f55a339e54071c6f76a5e19bc6aa1af9d3",
     "intent_revision_turn_id": "7d1e88ff-24a1-4883-9fc2-d5fe1cb514a5"
    },
    "marks_protected_regions": true,
    "answers_moment_questions": true,
    "answers_batched_questions": true
   },
   "created_at": "2026-11-08T13:51:07Z",
   "client_event_id": "BADDA80D-B4EC-43FA-9E35-72F3CE75F31A"
  },
  {
   "id": "3ded29c3-1bd7-4fad-b860-7d359cb68f68",
   "sequence": 8,
   "revision": 9,
   "role": "assistant",
   "event_type": "assistant_response",
   "content": "I’d try “comment REPO ➔”. Does this wording work?\nUse this wording · Revise it · Skip it",
   "payload": {
    "turn_id": "7d1e88ff-24a1-4883-9fc2-d5fe1cb514a5",
    "turn_value": "question",
    "receipt_ids": [],
    "next_actions": [],
    "brief_coverage": {
     "enforced_ids": [],
     "retrieved_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "applicable_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "unresolved_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ]
    },
    "schema_version": 2,
    "choice_question": {
     "kind": "creative_copy_wording",
     "options": [
      {
       "key": "approve",
       "label": "Use this wording",
       "recommended": false
      },
      {
       "key": "revise",
       "label": "Revise it",
       "recommended": false
      },
      {
       "key": "cancel",
       "label": "Skip it",
       "aliases": [
        "no",
        "none",
        "nope",
        "skip",
        "skip title",
        "no title",
        "no title please",
        "don't add a title",
        "without title",
        "without a title",
        "leave it off",
        "başlık olmasın",
        "başlıksız",
        "continue without a title",
        "hayır",
        "hayır teşekkürler",
        "atla",
        "geç",
        "başlık yok",
        "başlık istemiyorum",
        "başlık istemem",
        "başlığa gerek yok",
        "başlık koyma",
        "başlık ekleme",
        "başlıksız devam et",
        "gerek yok"
       ],
       "recommended": false
      }
     ],
     "version": 1,
     "conflict": "creative_copy",
     "candidate": "comment REPO ➔",
     "question_id": "b6c0535d-b862-4bec-93f6-09e294631db2",
     "allow_free_text": true,
     "creative_target": "closing_title",
     "candidate_digest": "02890523a650bc5e35dbdf64",
     "dependency_digest": "4c630510f9442821f44b8ece"
    }
   },
   "created_at": "2026-11-08T13:52:46Z"
  },
  {
   "id": "cecd7ac8-f439-41c7-8167-46da6793e56b",
   "sequence": 9,
   "revision": 10,
   "role": "user",
   "event_type": "user_message",
   "content": "Use this wording",
   "payload": {
    "turn_id": "ad7391d2-1846-4725-a8d2-310853c283b2",
    "turn_status": "pending",
    "request_digest": "47420f0357f37bd0cde0f29a0b2dbba0c5ddfa19db56a140e11f4f71e5167685",
    "client_protocol": 45,
    "runtime_version": 2,
    "choice_selection": {
     "option_key": "approve",
     "question_id": "b6c0535d-b862-4bec-93f6-09e294631db2"
    },
    "request_outcome_link": {
     "relation": "continuation",
     "link_status": "known",
     "client_build": "1926",
     "client_version": "0.1.0",
     "original_known": true,
     "parent_turn_id": "7d1e88ff-24a1-4883-9fc2-d5fe1cb514a5",
     "schema_version": 1,
     "client_protocol": 45,
     "request_turn_id": "d7f545fe-c2f1-4751-b84b-24cb3b6b0009",
     "server_revision": "5b97e9f55a339e54071c6f76a5e19bc6aa1af9d3",
     "intent_revision_turn_id": "7d1e88ff-24a1-4883-9fc2-d5fe1cb514a5"
    },
    "marks_protected_regions": true,
    "answers_moment_questions": true,
    "answers_batched_questions": true
   },
   "created_at": "2026-11-08T13:53:55Z",
   "client_event_id": "01D45F32-9316-433F-B20A-934D9156FBDB"
  },
  {
   "id": "58f31f3e-863d-479b-af81-4bc50d54661b",
   "sequence": 10,
   "revision": 11,
   "role": "assistant",
   "event_type": "assistant_response",
   "content": "I can't make this yet:\n- GitHub logo above the title (\"GitHub logo\"): That exact text isn't in this draft.\nI held this first draft back. The simpler version leaves out: GitHub logo above the title (\"GitHub logo\"). Should I make it without these, or do you want to change your request?",
   "payload": {
    "turn_id": "ad7391d2-1846-4725-a8d2-310853c283b2",
    "turn_value": "question",
    "receipt_ids": [],
    "next_actions": [],
    "brief_coverage": {
     "stage": "draft",
     "reason": "approval_requirements_not_ready",
     "enforced_ids": [],
     "retrieved_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "applicable_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "unresolved_ids": [
      "r1",
      "r2",
      "r3",
      "r4",
      "r5",
      "r6",
      "r7",
      "r8",
      "r9",
      "r10",
      "r11",
      "r12"
     ],
     "approval_readiness": {
      "ready": false,
      "digest": "c9b43dc1d6a24f30423690860de761bdb732366439d2cd7b0fda56df64ef836e",
      "brief_digest": "462b64db9d2f6d2ffea9a8f579901fb3c706232a7e5474edc19558cc517573db",
      "media_digest": "4c630510f9442821f44b8ece1f405a1fd79920b8ad99d6409478ccfbd59a0328",
      "requirements": [
       {
        "reason": null,
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r1"
       },
       {
        "reason": "That exact text isn't in this draft.",
        "disposition": "needs_choice",
        "evidence_owner": "brief_checks",
        "requirement_id": "r2"
       },
       {
        "reason": "5 photos or videos take turns behind you; you're small at the bottom right. I'll zoom in on the text when it's made; I can't confirm it before then. You'll start full screen in your own room, then you're small in the bottom-right corner when you start on your first recording.",
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r3"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r4"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r5"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r6"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r7"
       },
       {
        "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r8"
       },
       {
        "reason": null,
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r9"
       },
       {
        "reason": "If Clean up speech is offered when you approve, choose it and every pause is cut down to about 0.3 s",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r10"
       },
       {
        "reason": "I'll draw a curly arrow next to your ending text",
        "disposition": "unverified",
        "evidence_owner": "brief_checks",
        "requirement_id": "r11"
       },
       {
        "reason": "No music, as asked.",
        "disposition": "executable",
        "evidence_owner": "brief_checks",
        "requirement_id": "r12"
       }
      ],
      "brief_version": 1,
      "policy_problem": null,
      "schema_version": 1,
      "protected_marks": true,
      "strategy_digest": "5ac5f2f97e068551615f92e4287e00e8edac3c1de6a01a6c37111023c237f5fd",
      "capabilities_digest": "191471b3b995a7fcc6764459c7763a6f9497907f6bc57d4a054b6f3196040426",
      "prepared_evidence_digest": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b"
     }
    },
    "schema_version": 2,
    "choice_question": {
     "options": [
      {
       "key": "change_request",
       "label": "Change my request",
       "aliases": [
        "Change my request",
        "İsteğimi değiştir",
        "Try a different approach",
        "Farklı bir yol dene"
       ],
       "description": "Nothing is made yet; tell me what to do differently.",
       "recommended": false
      },
      {
       "key": "simpler_version",
       "label": "Make it without these",
       "aliases": [
        "simpler version",
        "make it more basic",
        "skip, decide for me",
        "Make it without these",
        "Bunlar olmadan hazırla",
        "Make this simpler version",
        "Daha basit sürümü hazırla"
       ],
       "description": "Leaves out: GitHub logo above the title (\"GitHub logo\")",
       "recommended": false
      }
     ],
     "version": 1,
     "conflict": "simplification_requires_choice",
     "question_id": "796cdd17-6485-4481-934c-3ab05aab782b",
     "brief_version": 1,
     "allow_free_text": true,
     "omitted_requirement_ids": [
      "r2"
     ]
    },
    "requirement_receipts": [
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r1",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "That exact text isn't in this draft.",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r2",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r3",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r4",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r5",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r6",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r7",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r8",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r9",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "Choose Clean up speech when you approve and every pause is cut down to about 0.3 s",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r10",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "checked",
      "reason": "I'll draw a curly arrow next to your ending text",
      "status": "partial",
      "inferred": [],
      "verification": "checked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r11",
      "inferred_labels": [],
      "target_media_ids": []
     },
     {
      "stage": "understood",
      "reason": "Not made yet: the draft is on hold.",
      "status": "partial",
      "inferred": [],
      "verification": "unchecked",
      "brief_version": 1,
      "generation_id": null,
      "requirement_id": "r12",
      "inferred_labels": [],
      "target_media_ids": []
     }
    ]
   },
   "created_at": "2026-11-08T13:55:35Z"
  },
  {
   "id": "dc10c961-bf39-4474-913f-a85f0d48448e",
   "sequence": 11,
   "revision": 12,
   "role": "system",
   "event_type": "thread_renamed",
   "content": "",
   "payload": {
    "title": "qe2"
   },
   "created_at": "2026-11-08T13:56:20Z",
   "client_event_id": "2C13162A-A7C5-4D32-819D-13DB582D94AB"
  }
 ],
 "answer": {
  "id": "20b7638f-9eab-40b4-ae90-100ee9da1475",
  "sequence": 13,
  "revision": 14,
  "role": "assistant",
  "event_type": "draft_applied",
  "content": "Here's the simpler version.\n- Done: bold caps, then serif, then handwritten (\"5 PROJECTS / the AI builder community / can't stop talking about!!!!\")\n- Done: Start full screen, then put my screen recordings full screen and me cut out small in the bottom right corner while I talk, zoom into the recordings, show the right one when I talk about each repo, show trending page when I talk about what's hot (5 photos or videos…)\n- Will do at render: Add a label when each one starts (\"1. Spec-kit\") (I'll show it the…)\n- Will do at render: Add a label when each one starts (\"2. Impeccable\") (I'll show it the…)\n- Will do at render: Add a label when each one starts (\"3. TradingAgents\") (I'll show it the…)\n- Will do at render: Add a label when each one starts (\"4. Agents\") (I'll show it the…)\n- Will do at render: Add a label when each one starts (\"5. Claude code game studio\") (I'll show it the…)\n- Done: Captions word by word in a light grey box near the bottom.\n- Will do at render: Cut all my pauses. (Choose Clean up…)\n- Will do at render: End with comment REPO and an arrow. (\"comment REPO\") (I'll draw a curly…)\n- Done: Only my voice. (No music, as asked)\n…\n- Left out as agreed: GitHub logo above the title (\"GitHub logo\")",
  "payload": {
   "changes": [
    "Subtitled format"
   ],
   "turn_id": "b3178714-691a-46ce-96df-c43f417d2c8f",
   "can_undo": false,
   "draft_id": "8c5e06ef-c3df-453e-b491-6f50137f2422",
   "receipt_ids": [
    "ef096d60-f345-412a-9eea-e727d9e995a0"
   ],
   "snapshot_hash": "776cb312bff699513d07b3dfdf09b99faceadb0cf99f4d3fdc3cc506298e8e38",
   "draft_revision": 0,
   "requirement_receipts": [
    {
     "stage": "checked",
     "reason": null,
     "status": "met",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r1",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "5 photos or videos take turns behind you; you're small at the bottom right. I'll zoom in on the text when it's made; I can't confirm it before then. You'll start full screen in your own room, then you're small in the bottom-right corner when you start on your first recording.",
     "status": "met",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r3",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
     "status": "partial",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r4",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
     "status": "partial",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r5",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
     "status": "partial",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r6",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
     "status": "partial",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r7",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear",
     "status": "partial",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r8",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": null,
     "status": "met",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r9",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "Choose Clean up speech when you approve and every pause is cut down to about 0.3 s",
     "status": "partial",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r10",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "I'll draw a curly arrow next to your ending text",
     "status": "partial",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r11",
     "inferred_labels": [],
     "target_media_ids": []
    },
    {
     "stage": "checked",
     "reason": "No music, as asked.",
     "status": "met",
     "inferred": [],
     "verification": "checked",
     "brief_version": 2,
     "generation_id": null,
     "requirement_id": "r12",
     "inferred_labels": [],
     "target_media_ids": []
    }
   ]
  },
  "created_at": "2026-11-08T13:58:39Z"
 }
}
"""#
}
#endif
