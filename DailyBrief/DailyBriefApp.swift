import SwiftUI
import WebKit

@main
struct DailyBriefApp: App {
    var body: some Scene {
        WindowGroup {
            BriefWebView(url: URL(string: "https://phurley.github.io/daily-brief/")!)
                .ignoresSafeArea(edges: .bottom)
                .onOpenURL { incoming in
                    guard incoming.scheme == "dailybrief", incoming.host == "event",
                          let components = URLComponents(url: incoming, resolvingAgainstBaseURL: false),
                          let value = components.queryItems?.first(where: { $0.name == "url" })?.value,
                          let url = URL(string: value),
                          ["https", "http"].contains(url.scheme?.lowercased() ?? "") else { return }
                    UIApplication.shared.open(url)
                }
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
