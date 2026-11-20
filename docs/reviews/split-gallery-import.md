# Split Video gallery import

On an iPhone 17 Pro simulator running iOS 26.5, the test library held 216 photos and one video. The split panel previously fetched the newest 200 assets. The shared upload gallery reached both test items beyond that cutoff.

| Selection in newest-first order | Result |
| --- | --- |
| Cancel without choosing | The middle panel stayed empty. |
| Photo at position 201 | The editor document gained one clip in the middle panel. |
| Video at position 211 | The editor document gained one clip in the middle panel. |

To repeat, seed a simulator with at least 211 photos and videos using `xcrun simctl addmedia`. Run `KriaUITests/SplitFlowGapsUITests/testSplitImportsFromTheUploadGallery` with `TEST_RUNNER_KRIA_SPLIT_PHOTO_INDEX=200`, then with `210`. The test bundle retains the gallery screenshot and editor document for each selection. This exercises the real Photos library and local timeline placement with a fixture upload service.

The original build failed the check for the shared gallery's confirmation button. The two final import runs passed. Xcode 27.2 required a temporary explicit `Task<Void, Never>` on the slide editor's paste action to compile the baseline and test builds. The picker branch excludes that workaround. Physical-device and TestFlight results are pending.
