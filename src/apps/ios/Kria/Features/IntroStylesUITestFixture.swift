#if DEBUG
import Foundation

/// KRI-1633 (`-ui-testing-editor-intro-styles`, `KRIA_EDITOR_INTRO_STYLES=1`, `KRIA_EDITOR_INTRO_STYLES_FIXTURE=<path>`):
/// the intro styles panel end to end, in the editor, with no account.
///
/// The editor variant (the capability and the plain title rows) and the offers come from
/// `Tests/Fixtures/KRI1633IntroStyles.json`, which the backend writes from the real offer builder and drift-checks. The
/// block is picked by launch argument (`-intro-styles-tr` = the montage Turkish offer, `-intro-styles-narrated`;
/// default = Talking English). The route answers the offer with `replace_ids` set to the ids of the rows it was SENT,
/// like the server's. Every editor-commit body is appended, one JSON object per line, to
/// `KRIA_EDITOR_INTRO_STYLES_CAPTURE` so the UI test (another process) can read what Save sent; the commit itself is
/// answered by `NativeEditorUITestCommitTransport`.
enum IntroStylesUITestFixture {
    private static let env = ProcessInfo.processInfo.environment
    private static let args = ProcessInfo.processInfo.arguments

    static var isEnabled: Bool { args.contains("-ui-testing-editor-intro-styles") && env["KRIA_EDITOR_INTRO_STYLES"] == "1" }

    /// The suffix of the `editor_*` / `offer_*` blocks this launch uses.
    static var block: String {
        if args.contains("-ui-testing-editor-intro-styles-tr") { return "montage_tr" }
        if args.contains("-ui-testing-editor-intro-styles-narrated") { return "narrated_en" }
        return "talking_en"
    }

    nonisolated(unsafe) private static let fixture: [String: JSONValue] = {
        guard let path = env["KRIA_EDITOR_INTRO_STYLES_FIXTURE"], let data = FileManager.default.contents(atPath: path),
              let object = try? JSONDecoder().decode(JSONValue.self, from: data).objectValue else {
            preconditionFailure("KRIA_EDITOR_INTRO_STYLES needs KRIA_EDITOR_INTRO_STYLES_FIXTURE (Tests/Fixtures/KRI1633IntroStyles.json)")
        }
        return object
    }()

    /// The editor variant block: `editor_capabilities` (with `intro_styles`) and the plain title rows.
    static var editorVariant: [String: JSONValue] { fixture["editor_" + block]?.objectValue ?? [:] }

    static func respond(_ request: URLRequest) -> (status: Int, data: Data)? {
        guard isEnabled, let path = request.url?.path, request.httpMethod == "POST" else { return nil }
        let body = bodyData(request)
        if path.hasSuffix("/editor-commit") {
            capture(body)
            return nil
        }
        guard path.hasSuffix("/intro-styles"), let sent = try? JSONDecoder().decode(JSONValue.self, from: body).objectValue,
              sent["version"] == .number(1), let rows = sent["title_rows"]?.arrayValue, !rows.isEmpty else { return nil }
        var offer = args.contains("-ui-testing-editor-intro-styles-unavailable")
            ? fixture["unavailable_nothing_fits"]?.objectValue
            : fixture["offer_" + block]?.objectValue
        if offer?["available"] == .bool(true) { offer?["replace_ids"] = .array(rows.compactMap { $0.objectValue?["id"] }) }
        guard let offer, let data = try? JSONEncoder().encode(JSONValue.object(offer)) else { return nil }
        return (200, data)
    }

    private static func capture(_ body: Data) {
        guard let path = env["KRIA_EDITOR_INTRO_STYLES_CAPTURE"], !body.isEmpty else { return }
        // One compact JSON object per line.
        guard let value = try? JSONDecoder().decode(JSONValue.self, from: body), let line = try? JSONEncoder().encode(value) else { return }
        let url = URL(fileURLWithPath: path)
        let existing = (try? Data(contentsOf: url)) ?? Data()
        try? (existing + line + Data("\n".utf8)).write(to: url)
    }

    private static func bodyData(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var data = Data(), buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
#endif
