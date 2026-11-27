import Foundation

/// What the Intro styles panel shows.
enum IntroStylesState: Equatable {
    case idle
    case loading
    case ready(IntroStylesOffer)
    /// The route says there is nothing to offer (`reason` is its closed code), or this build cannot use the answer.
    case unavailable(reason: String?)
    /// The request failed; tapping the line tries again.
    case failed
}

// KRI-1633 intro styles on tap. Contract: docs/pipelines/intro-scene.md ("Intro styles on tap").
//
// The route computes and stores nothing. A tap on a look swaps the title rows in ONE undoable document change and
// the normal Save sends them, so the pick goes through every check an edit does (owner, baseline compare, 426 gates,
// the render-contract rebind). Her own edits after a pick are ordinary row edits and win over it.
extension NativeEditorSession {
    // MARK: Availability

    /// The title stack the editor holds right now: the rows of ONE title family (a row is a title line when its id is
    /// the family id or `<family>-line-N`), base line first. Read-only rows (the server's kill-switch shape), captions
    /// and removed rows never count.
    var introStyleTitleRows: [EditorTextElement] {
        let candidates = document.textElements.filter {
            !$0.isCaption && !$0.isReadOnly && $0.raw["removed"] != .bool(true) && IntroStyleFamily.family(of: $0.id) != nil
        }
        guard let family = IntroStyleFamily.ids.first(where: { family in candidates.contains { IntroStyleFamily.family(of: $0.id) == family } })
        else { return [] }
        return candidates.filter { IntroStyleFamily.family(of: $0.id) == family }
            .sorted { IntroStyleFamily.line(of: $0.id) < IntroStyleFamily.line(of: $1.id) }
    }

    /// The entry shows only when the server opened the capability for this build and variant (the key is present only
    /// when on), the Text lane is editable, and there is a title to restyle.
    var canOfferIntroStyles: Bool {
        guard !introStylesHidden, document.capabilities["intro_styles"]?.editable == true,
              cloudEditorUnavailableMessage == nil, canEdit(.text) else { return false }
        let rows = introStyleTitleRows
        guard !rows.isEmpty, rows.count <= IntroStyleFamily.maxRows else { return false }
        return rows.reduce(0) { $0 + $1.text.count } <= IntroStyleFamily.maxCharacters
    }

    /// True when `id` is a row the Style tab may offer intro styles for.
    func isIntroStyleTitleRow(_ id: String) -> Bool {
        canOfferIntroStyles && IntroStyleFamily.family(of: id) != nil
    }

    // MARK: Offer

    /// Identifies the title the offer was built for: the baseline and her words and window. An offer for anything
    /// else is dropped, never shown.
    func introStylesContext() -> String {
        let rows = introStyleTitleRows.map { "\($0.id)\u{1}\($0.text)\u{1}\($0.startS)\u{1}\($0.endS)" }.joined(separator: "\u{2}")
        return document.revision.baseGeneration + "\u{3}" + rows
    }

    /// Asks the route for the looks. Nothing is stored; an answer for a title that changed meanwhile is discarded.
    func loadIntroStyles(force: Bool = false) async {
        guard canOfferIntroStyles else { introStylesState = .idle; return }
        guard let transport = introStylesAPI else { introStylesState = .failed; return }
        let context = introStylesContext()
        if !force, case .ready = introStylesState, introStylesOfferContext == context { return }
        if case .loading = introStylesState, introStylesLoadContext == context { return }
        let rows = introStyleTitleRows.map { document.textRowJSON($0) }
        let request = IntroStylesRequest(baseGeneration: document.revision.baseGeneration, titleRows: rows)
        introStylesLoadContext = context
        introStylesState = .loading
        do {
            let response = try await transport(request)
            guard introStylesLoadContext == context, introStylesContext() == context else { return }
            switch IntroStylesOffer.parse(response.json) {
            case .offer(let offer):
                introStylesOfferContext = context
                introStylesState = .ready(offer)
            case .unavailable(let reason):
                introStylesOfferContext = nil
                introStylesState = .unavailable(reason: reason)
            }
        } catch {
            guard introStylesLoadContext == context else { return }
            // The panel went away (or the title changed) mid-request: nothing to show.
            if Task.isCancelled || error is CancellationError {
                introStylesLoadContext = nil
                introStylesState = .idle
                return
            }
            introStylesOfferContext = nil
            switch IntroStylesFailure.from(error) {
            case .hidden:
                introStylesHidden = true
                introStylesState = .idle
            case .unavailable:
                introStylesState = .unavailable(reason: nil)
            case .newerVersion:
                // A newer version was saved elsewhere: load it (her unsaved edits stay on top); the panel offers a retry.
                introStylesState = .failed
                _ = reloadLatestAfterPreviewConflict(error)
            case .retry:
                introStylesState = .failed
            }
        }
        if introStylesLoadContext == context { introStylesLoadContext = nil }
    }

    /// The looks, only while they are for the title on screen.
    var introStylesCurrentOffer: IntroStylesOffer? {
        guard case .ready(let offer) = introStylesState, introStylesOfferContext == introStylesContext() else { return nil }
        return offer
    }

    /// The option whose rows are the title right now (a pick, or the title as it was when no pick has happened):
    /// compared on the decoded rows, so her edit to any line leaves nothing selected.
    func introStyleSelectedKey(in offer: IntroStylesOffer) -> String? {
        let now = introStyleTitleRows
        return offer.options.first { EditorDocument.textElements(fromRows: $0.rows) == now }?.key
    }

    // MARK: Apply

    /// Puts `option.rows` in the title's place as ONE undoable change, raw: no `source_params` key is dropped on the
    /// way. Removes every row of the offer's title family that is in the document now (the stack on screen, whether it
    /// is the title as loaded or an earlier pick, even one made before the panel was closed and opened again), never a
    /// protected row, and clears any pending deletion of an id the look puts back (Save would drop that line from the
    /// lane). Returns false, changing nothing, when the offer no longer fits the document.
    @discardableResult
    func applyIntroStyle(_ option: IntroStylesOffer.Option) -> Bool {
        guard canOfferIntroStyles, let offer = introStylesCurrentOffer, offer.options.contains(option), !option.isCardLook else { return false }
        let incoming = EditorDocument.textElements(fromRows: option.rows)
        guard incoming.count == option.rows.count, !incoming.isEmpty,
              incoming.allSatisfy({ IntroStyleFamily.family(of: $0.id) == offer.titleFamily }) else { return false }
        let incomingIDs = Set(incoming.map(\.id))
        let onScreen = introStyleTitleRows.map(\.id)
        let removable = Set(offer.replaceIDs).union(onScreen).union(incomingIDs)
            .filter { IntroStyleFamily.family(of: $0) == offer.titleFamily }
        // A protected title row cannot be replaced: refuse the whole change rather than half apply it.
        if document.textElements.contains(where: { removable.contains($0.id) && $0.isReadOnly }) { return false }
        let applied = transactDocument(sections: [.text]) { doc in
            let first = doc.textElements.firstIndex { removable.contains($0.id) }
            let position = first.map { doc.textElements[..<$0].filter { !removable.contains($0.id) }.count } ?? doc.textElements.count
            doc.textElements.removeAll { removable.contains($0.id) }
            doc.textElements.insert(contentsOf: incoming, at: min(position, doc.textElements.count))
            doc.deletions.removeAll { $0.kind == "text" && incomingIDs.contains($0.id) }
        }
        guard applied else { return false }
        introStylesOfferContext = introStylesContext()
        replayIntroStyle(titleID: incoming[0].id)
        return true
    }

    /// The tapped look plays from the title's start, so she sees it in the editor's own player.
    private func replayIntroStyle(titleID: String) {
        guard let start = introStyleStartTime(titleID: titleID) else { return }
        seek(to: start)
        if !isPlaying { togglePlayback() }
    }
}
