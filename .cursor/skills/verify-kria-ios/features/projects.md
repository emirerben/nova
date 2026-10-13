# Projects and new chats

Creators open recent projects or their gallery, start another chat, and manage
a project's name without losing work when they cancel or a save fails.

## Sub-features

- `projects-new-chat`: new chat reaches the format chooser and composer.
- `projects-drawer`: button and edge-swipe open/close the same drawer.
- `projects-gallery`: Gallery reaches videos/posts with All and Ready filters.
- `projects-actions`: rename/delete cancellation preserves the selected project;
  invalid names and failed saves retain the input.

## How to get to it (user POV)

- Tap `Open projects` (`workspace-menu-toggle`) or swipe from the left edge.
- Tap New chat (`drawer-new-chat`) in the drawer.
- Tap the row whose name begins `Gallery`.
- Open `Project actions for…`, then Rename project or Delete project.

## Driving it with XCUITest

Preconditions: a successful Launch and Doctor, with a fresh run name.
The selected tests use `-ui-testing-chat`; the poster check uses a brand fixture.

```sh
"$KRIA_VERIFY_CONTROL" drive --run "$KRIA_VERIFY_RUN" --feature projects \
  CreationUITests/testLaunchEntersChatFirstWorkspace \
  ProjectsUITests/testProjectActionsCanBeCancelledWithoutChangingProject \
  ProjectsUITests/testRenameValidatesNameAndRetainsInputAfterFailedSaveAndRetry
```

- **Start fresh.** Tap Open projects → New chat. `What are we making?`,
  `Message Kria` and format cards appear. Without a format, Attach footage is
  disabled.
- **Drawer and gallery.** Open/close by button and edge-swipe; format-carousel
  swipes leave the drawer closed. Gallery shows `Your videos and posts`, All,
  and Ready. The launch test records the swipe-open workspace.
- **Cancel actions.** Cancel Rename and Delete; `workspace-project-title` stays
  unchanged. Whitespace/121-character names cannot submit; 120 characters can.
- **Failed save.** Enter `  warm   café  ` and retry the deliberately failing
  rename request. The alert retains that exact input and Cancel leaves the
  original title.

## Gotchas

- Rename intentionally fails at the fixture API boundary. This proves input
  preservation/cancellation, not successful server persistence or deletion.
- Poster states have a separate fixture entry point:
  `ProjectsUITests/testGalleryPosterStatesNeverSubstituteSamplePhotos`. It checks
  `project-poster-unavailable`, `project-poster-loading` and `project-poster-image`;
  it does not prove that live poster URLs load.
- The project fixture never reads the developer's Keychain. A restored real
  account or production gallery remains outside this offline proof.
