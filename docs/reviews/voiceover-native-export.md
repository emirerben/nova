# Voiceover native export verification

## Scope

The phone preview receives a per-slot `source_audio_volume` marker (0 or 1)
and one final, ducked `source_audio_bed_gain`. The marker selects the camera
source; `AudioMixRecipe.originalVolume` supplies the gain once.

## Executed checks

On 2026-10-22, the following host macOS AVFoundation test passed:

```sh
cd src/apps/ios/Packages/KriaMediaEngine
swift test --filter SourceAudioOverlapParityTests/testVoiceoverSelectedSourceAudioMatchesPreviewAndExport
```

Result: 1 test passed in 3.230 seconds.

The test creates its own media in a temporary directory:

- 431 Hz camera tone for an unselected slot;
- 719 Hz camera tone for the selected slot;
- 997 Hz narration tone;
- source-bed gain `0.0875`;
- a `20 dk` text layer from 1.2 to 1.8 seconds.

It decodes PCM from both `LivePreviewComposition` and the final exported MP4.
For each stereo channel it verifies:

- 431 Hz is below 0.01 in both windows;
- 719 Hz is below 0.01 before the selected slot and above 0.025 in it;
- 997 Hz is above 0.2 in both windows;
- the decoded peak stays below 1;
- `20 dk` has fewer than 10 bright pixels one 30 fps frame before 1.2 seconds,
  and more than 30 one frame after it.

The focused app-side simulator command also completed successfully on simulator
`9869B5F5-B545-420A-8254-270EC45565CE`:

```sh
cd src/apps/ios
xcodebuild -project Kria.xcodeproj -scheme Kria \
  -destination 'platform=iOS Simulator,id=9869B5F5-B545-420A-8254-270EC45565CE' \
  -derivedDataPath /private/tmp/nova-ios-derived \
  -only-testing:KriaTests/NativeEditorSessionTests \
  -only-testing:KriaTests/NativeEditorRenderCompilerTests \
  -only-testing:KriaTests/KriaTests \
  -only-testing:KriaTests/SuggestionChipsTests test
```

`git diff --check` passed.
