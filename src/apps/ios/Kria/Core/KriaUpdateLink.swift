import UIKit

/// Where every "Update Kria" button goes. Kria ships through TestFlight, so the update is
/// in the TestFlight app: open it when it is installed (`itms-beta://`, declared in
/// `LSApplicationQueriesSchemes` so `canOpenURL` can see it), else the App Store page that
/// installs TestFlight, from where the creator gets the update.
enum KriaUpdateLink {
    static let testFlight = URL(string: "itms-beta://")!
    /// TestFlight's App Store listing.
    static let appStore = URL(string: "https://apps.apple.com/app/testflight/id899247664")!

    @MainActor static var destination: URL {
        UIApplication.shared.canOpenURL(testFlight) ? testFlight : appStore
    }

    static func isUpdate(_ url: URL) -> Bool { url == testFlight || url == appStore }
}
