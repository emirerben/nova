# KRI-1264: zoom and animation on a phone Talking card

Parent: KRI-1258 (talk-to-camera founder test, 2026-11-07). The founder reported that on a phone-rendered
talk-to-camera edit, photo and overlay zoom does nothing. Only rotation works on an added photo or card.

## Acceptance

- In the iPhone editor, a photo card on top of the speaker offers Zoom (1x to 4x) and the Animation tab (entrance,
  exit, loop, speed) once the server offers it.
- Save keeps both. The exported video draws the card at the chosen zoom about its own centre, and plays the chosen
  animation on the card's own time.
- A 2x card is twice as wide as the same plain card. 0.1 s into a 0.4 s Pop entrance the card is smaller and fainter
  than once settled. Outside its window it is not drawn.
- The editor preview and the export draw a zoomed, animated, tilted card on the same pixels.
- Nothing changes for a card the creator never zoomed or animated, and nothing changes for an app build that cannot
  draw the new fields.

## Failure modes (written before the code)

1. Old build + new server, pinned recipe. A build below 44 cannot decode a placement `editor_style` (its
   `VisualEditorStyle` misses `rotation_deg` under the recipe decoder), so the whole recipe would fail. Such a build
   must get 426 `client_update_required` (`card_style`, minimum 44) for a recipe that carries a styled card, and none
   for a plain one.
2. Old build + new server, editor. A build below 44 never gets `media_overlay_style`, so its Zoom and Animation stay
   closed and its capability map is unchanged. A Save from it that resends a stored style keeps the style. A new
   style from it is a 422.
3. New build + old server. The old server never offers `media_overlay_style` and never pins a placement style, so the
   new build keeps Zoom and Animation closed on a Talking card and decodes every recipe it did before.
4. Zoom at 1x and 4x. 1x writes no style anywhere. 4x saves, compiles and renders (cropped by the frame, not
   refused). 0.5, 4.5 and NaN are refused, never clamped.
5. Animation and rotation together. A popped, tilted card keeps its angle on the clip transform and its animation on
   the placement, and the device draws it on the same pixels as the editor preview.
6. Card at the screen edge. A 2x card at x 0.95 compiles and renders, cropped at the frame edge.
7. Save without edits. An unstyled card compiles to the same clip bytes as before, the card-tilt parity fixture is
   unchanged, and resending a styled card unchanged pins the same card clip.
8. Undo and redo. One undo step per zoom or animation change, redo puts it back, and zoom 1 with no animation leaves
   the card exactly as loaded.
9. Full-screen card. It cannot be zoomed or animated, going full screen drops the style, and the server refuses a
   style on a full-screen card.
10. Chat on a styled card. A chat tilt or straighten keeps the card's zoom and animation.
11. Read back without saved lanes. A recipe-derived projection keeps zoom and animation.
12. Gates closed. The capability is absent and a new style is a 422. A stored style survives a Save after the token
    is removed, and a reset to plain is always allowed.
13. Visual blocks on a Talking edit stay plain: the capability never opens zoom or animation on an added visual block.
14. A video card takes the same style as a photo card.
