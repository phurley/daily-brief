import SwiftUI
import WebKit

@main
struct DailyBriefApp: App {
    var body: some Scene {
        WindowGroup {
            BriefScreen()
                .ignoresSafeArea(edges: .bottom)
                .onOpenURL { incoming in
                    guard incoming.scheme == "dailybrief", incoming.host == "event",
                          let components = URLComponents(url: incoming, resolvingAgainstBaseURL: false),
                          let value = components.queryItems?.first(where: { $0.name == "url" })?.value,
                          let url = URL(string: value), Self.isWebURL(url) else { return }
                    UIApplication.shared.open(url)
                }
        }
    }
    static func isWebURL(_ url: URL) -> Bool {
        ["https", "http"].contains(url.scheme?.lowercased() ?? "") && url.host != nil
    }
}

struct BriefScreen: View {
    @State private var failure: String?
    @State private var retry = UUID()
    var body: some View {
        ZStack {
            BriefWebView(url: URL(string: "https://phurley.github.io/daily-brief/")!, failure: $failure).id(retry)
            if let failure {
                VStack(spacing: 16) {
                    Text("The brief couldn’t be opened").font(.headline)
                    Text(failure).font(.body)
                    Button("Retry") { self.failure = nil; retry = UUID() }
                        .buttonStyle(.borderedProminent)
                }.padding().background(.regularMaterial).cornerRadius(16).padding()
            }
        }
    }
}

struct BriefWebView: UIViewRepresentable {
    let url: URL
    @Binding var failure: String?
    func makeCoordinator() -> Coordinator { Coordinator(baseURL: url, failure: $failure) }
    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true
        view.load(URLRequest(url: url, cachePolicy: .reloadRevalidatingCacheData, timeoutInterval: 15))
        return view
    }
    func updateUIView(_ view: WKWebView, context: Context) { }

    class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let baseURL: URL
        let failure: Binding<String?>
        var openExternal: (URL) -> Void = { UIApplication.shared.open($0) }
        init(baseURL: URL, failure: Binding<String?>) { self.baseURL = baseURL; self.failure = failure }
        func isInternal(_ url: URL) -> Bool {
            url.scheme == baseURL.scheme && url.host == baseURL.host && url.port == baseURL.port && url.path.hasPrefix(baseURL.path)
        }
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url, DailyBriefApp.isWebURL(url) else { decisionHandler(.cancel); return }
            if isInternal(url) {
                if action.targetFrame == nil { webView.load(action.request); decisionHandler(.cancel) }
                else { decisionHandler(.allow) }
            } else {
                if action.navigationType == .linkActivated || action.targetFrame == nil { openExternal(url) }
                decisionHandler(.cancel)
            }
        }
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            guard action.targetFrame == nil, let url = action.request.url, DailyBriefApp.isWebURL(url) else { return nil }
            if isInternal(url) { webView.load(action.request) } else { openExternal(url) }
            return nil
        }
        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { failure.wrappedValue = nil }
        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { show(error) }
        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { show(error) }
        private func show(_ error: Error) {
            if (error as NSError).code != NSURLErrorCancelled { failure.wrappedValue = error.localizedDescription }
        }
        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { failure.wrappedValue = "Please retry to reload the brief." }
    }
}
