import SwiftUI
import WebKit

struct NativeResearchSourcesView: View {
    let document: EditorDocument
    @Environment(\.dismiss) private var dismiss
    private var cited: [(EditorTextElement, ResearchCitation)] {
        document.textElements.compactMap { text in ResearchCitation.from(text).map { (text, $0) } }
    }

    var body: some View {
        NavigationStack {
            List {
                ForEach(cited, id: \.1.id) { text, citation in
                    Section {
                        Text(text.text).font(.headline)
                        if !citation.matches(text, clips: document.clips) {
                            Label("Text or footage changed. Recheck these sources.", systemImage: "info.circle")
                                .font(.subheadline).foregroundStyle(.secondary)
                            Text("Original: \(citation.originalText)").font(.subheadline)
                        }
                        ForEach(citation.sources) { source in
                            Link(destination: source.url) {
                                HStack {
                                    Text(source.title)
                                    Spacer()
                                    Image(systemName: "arrow.up.right").font(.caption)
                                        .accessibilityHidden(true)
                                }
                            }
                        }
                        if !citation.retrievedAt.isEmpty {
                            Text("Researched \(String(citation.retrievedAt.prefix(10)))")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    } header: {
                        Text("\(citation.startS, specifier: "%.1f")–\(citation.endS, specifier: "%.1f") seconds")
                    }
                }
                ForEach(Array(Set(cited.map { $0.1.searchEntryHTML }.filter { !$0.isEmpty })).sorted(), id: \.self) { html in
                    Section("Search suggestions") {
                        ResearchSearchAttribution(html: html).frame(height: 220)
                    }
                }
            }
            .navigationTitle("Sources")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        }
        .accessibilityIdentifier("native-editor-research-sources")
    }
}

/// Provider-required attribution is isolated from app credentials and JavaScript.
struct ResearchSearchAttribution: UIViewRepresentable {
    let html: String
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.defaultWebpagePreferences.allowsContentJavaScript = false
        let view = WKWebView(frame: .zero, configuration: config)
        view.navigationDelegate = context.coordinator
        view.isOpaque = false
        view.backgroundColor = .clear
        let policy = "default-src 'none'; style-src 'unsafe-inline'; img-src data: https://www.gstatic.com https://www.google.com; base-uri 'none'; form-action 'none'; frame-src 'none'"
        view.loadHTMLString("<meta http-equiv=\"Content-Security-Policy\" content=\"\(policy)\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">" + html, baseURL: nil)
        return view
    }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
    final class Coordinator: NSObject, WKNavigationDelegate {
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if action.navigationType == .linkActivated, let address = action.request.url?.absoluteString,
               let url = ResearchCitation.publicURL(address) {
                UIApplication.shared.open(url)
                decisionHandler(.cancel)
            } else {
                decisionHandler(action.request.url?.scheme == "about" ? .allow : .cancel)
            }
        }
    }
}
