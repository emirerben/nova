"""Every suggestion kind executes on tap through the ONE draft path, or refuses honestly.

Failure catalogue, written before any seam code (the seam so far ran only editor-ops cards). Every
row is a fixture below that drives submit -> claim -> plan -> row-locked commit on real Postgres
through ``run_kria_turn``; none restates an implementation detail. The rule each row defends: a tap
runs the action the card was proven with through ``_complete_draft_turn`` (a new draft revision, a
typed brief version, receipts, Undo to the parent), or says plainly that it no longer fits and
tells the phone so in a machine-readable way. It never reaches the free planner and never writes
half a change.

Per kind (hook, duplicates, mute_clip, filler_words; captions/sfx/transition/music are pinned by
``test_kria_pinned_suggestion`` and re-run here in the all-kinds table)
  K1  stale base. The editor moved since the card was served (index-addressed ``remove_clip``
      lands on another clip), the head draft moved (a strategy restate would replace newer work),
      the brief moved (``expected_version`` of a typed update), a title appeared, the analysis row
      was replaced. Rebuild from the registry and the facts as they are now, or "no longer fits".
  K2  unsaved editor state. A creator's hand edits must survive: editor-ops cards rebuild against
      them (their edits become the parent revision); a strategy restate would re-plan over them, so
      it refuses instead.
  K3  lost lease / requeue by a newer message. Nothing is written for the stale run; the second
      run applies exactly once (the generic fixtures FM13/FM14, re-proven here for a copy card and
      a strategy restate).
  K4  double tap. One ``client_event_id`` replays one turn; a late second tap on an applied card
      (title now present, ask now live, setting now on, clip now gone) is "no longer fits" and
      writes nothing, never a second change.
  K5  consent missing (KRI-506). An open creative-copy question blocks the hook tap inside the lock
      with the existing reply. The tap's own decision never blocks itself (the consent gate does
      not fold ``source="suggestion"``) and never reads as copy the creator wrote.
  K6  KRI-129. A live title requirement hides the hook card; a clip a request names is never the
      one a duplicates card removes; a second mute of a clip the brief already turns off is "no
      longer fits".
  K7  ``expected_version`` conflict. The brief moves between the plan and the row lock: a refusal
      reply, not a "please retry" crash, and no brief version is written.
  K8  ``analysis_redone``. The scheduler re-opens the cleanup analysis on the tap: the setting is
      not flipped, but the re-queued analysis row is committed and published (rolling back would
      orphan it), and the creator gets "no longer fits".
  K9  duplicate keeper already removed / pair changed. The rebuilt card must be exactly the offer:
      the closed prompt rendered from the rebuilt pair equals the stored prompt, else refuse.
  K10 flag off. A kind switched off after serve refuses at the tap; a kill switch stops a card
      that was served before it was pulled. Flag-off ``TurnAccepted`` and the assistant event
      carry no new field (byte-identical).
  K11 old iOS ignoring the new ack field. The field is additive and omitted when absent.
  K12 an old build (no ``suggestion`` field in the ack) still reads the refusal from the reply.
  K13 hook copy laundering. The creator's chat message is the closed card sentence, never the copy
      (``creator_quoted`` bypass); the applied title equals the card's text exactly; Gemini
      metadata can never become the title (the copy gate re-runs at the tap with the clip texts);
      text-carrying ops on an editor-ops card stay refused (FM12) while a hook card may run only
      its own single ``set_title``.
  K14 mute: ``PINNED_TAP_APPLIES_CLIP_SOUND`` stays False, so nothing is served; the seam is proven
      to be able to run the typed update (the constant is patched on only inside the fixture that
      builds the card), and an unproven surface never reaches a creator.
  K15 filler: the setting flip is atomic with the draft (a later raise rolls it back), is refused
      when the capability closed, and hands the scheduled analysis to the publisher after commit.

Refusals
  R1  every refusal (planner-side or at the commit) answers with the same typed signal on the
      assistant event (``suggestion_pin``: set_id, id, outcome, reason); a transient refusal
      (a render in flight) keeps the card tappable and is not marked.
  R2  a refused card is marked on its ring entry in the refusal's own transaction; the GET hides
      it, mints a NEW set_id (so the phone's "hidden until the set changes" ledger releases and the
      "refreshing" claim is true) and stores the derived set so a tap on a card in it resolves.
  R3  an applied card is hidden by id on the GET even with the editor triggers flag off.
  R4  ``ready_context`` carries the kinds' inputs (the shared loader), so a rebuild sees what the
      serve saw: the hook's clip texts, the brief, the filler evidence, the duplicates' ``needed``.

Database (collection): same CI-aware guard as ``test_kria_pinned_suggestion`` (FM24).
"""
