import SwiftUI
import WebKit

@main
struct DailyBriefApp: App {
    var body: some Scene {
        WindowGroup {
            BriefWebView(url: URL(string: "https://phurley.github.io/daily-brief/")!)
                .ignoresSafeArea(edges: .bottom)
        }
    }
}

struct BriefWebView: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.allowsBackForwardNavigationGestures = true
        view.load(URLRequest(url: url, cachePolicy: .reloadRevalidatingCacheData))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) { }
}
