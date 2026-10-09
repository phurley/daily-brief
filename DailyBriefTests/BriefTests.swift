import XCTest
import WebKit
import SwiftUI
@testable import DailyBrief

@MainActor
final class BriefTests: XCTestCase {
    func testCacheExpiresAtDetroitMidnightAndRejectsWrongSchema() throws {
        let json = #"{"schemaVersion":1,"editionId":"test","editionDate":"2026-10-09","generatedAt":"2026-10-09T15:00:00Z","sourceGeneratedAt":"2026-10-09T14:00:00Z","events":[]}"#.data(using: .utf8)!
        let document = try JSONDecoder().decode(EventsDocument.self, from: json)
        let cache = CachedEvents(document: document, fetchedAt: Date())
        let parse = ISO8601DateFormatter()
        XCTAssertTrue(cache.usable(on: parse.date(from: "2026-10-10T03:59:59Z")!))
        XCTAssertFalse(cache.usable(on: parse.date(from: "2026-10-10T04:00:00Z")!))
        XCTAssertEqual(try JSONDecoder().decode(CachedEvents.self, from: JSONEncoder().encode(cache)).document.editionDate, "2026-10-09")
        let unsupported = EventsDocument(schemaVersion: 2, editionId: "test", editionDate: "2026-10-09", generatedAt: "", sourceGeneratedAt: "", events: [])
        XCTAssertFalse(CachedEvents(document: unsupported, fetchedAt: Date()).usable(on: parse.date(from: "2026-10-09T12:00:00Z")!))
    }
    func testURLPolicyAndRetryErrorState() {
        var failure: String?
        let coordinator = BriefWebView.Coordinator(baseURL: URL(string:"https://phurley.github.io/daily-brief/")!, failure: Binding(get: {failure}, set: {failure=$0}))
        XCTAssertTrue(coordinator.isInternal(URL(string:"https://phurley.github.io/daily-brief/?date=2026-10-08#events-section")!))
        XCTAssertFalse(coordinator.isInternal(URL(string:"https://phurley.github.io/other/")!))
        XCTAssertFalse(DailyBriefApp.isWebURL(URL(string:"javascript:alert(1)")!))
        XCTAssertFalse(DailyBriefApp.isWebURL(URL(string:"file:///etc/passwd")!))
        let view = WKWebView()
        coordinator.webView(view, didFailProvisionalNavigation: nil, withError: URLError(.notConnectedToInternet))
        XCTAssertNotNil(failure)
        coordinator.webView(view, didFinish: nil)
        XCTAssertNil(failure)
    }
    func testNormalAndNewWindowLinksDispatchOnceAndRejectUnsafeSchemes() async throws {
        let view = WKWebView()
        let loaded = expectation(description: "HTML loaded")
        let loader = LoadDelegate { loaded.fulfill() }
        view.navigationDelegate = loader
        view.loadHTMLString("<a id='event' href='https://example.com/event' target='_blank'>Event</a><a id='article' href='https://example.com/article'>Article</a><a id='unsafe' href='file:///etc/passwd' target='_blank'>Bad</a>", baseURL: URL(string:"https://phurley.github.io/daily-brief/"))
        await fulfillment(of:[loaded], timeout:10)
        let coordinator = BriefWebView.Coordinator(baseURL: URL(string:"https://phurley.github.io/daily-brief/")!, failure:.constant(nil))
        view.navigationDelegate = coordinator
        view.uiDelegate = coordinator
        var urls:[URL] = []
        let opened = expectation(description:"Two external links")
        opened.expectedFulfillmentCount=2
        coordinator.openExternal = {url in urls.append(url); opened.fulfill()}
        try await view.evaluateJavaScript("document.getElementById('event').click()")
        try await view.evaluateJavaScript("document.getElementById('article').click()")
        try await view.evaluateJavaScript("document.getElementById('unsafe').click()")
        await fulfillment(of:[opened],timeout:10)
        XCTAssertEqual(urls.map(\.absoluteString),["https://example.com/event","https://example.com/article"])
    }
}
@MainActor
private class LoadDelegate: NSObject, WKNavigationDelegate {
    let loaded: ()->Void
    init(_ loaded: @escaping ()->Void) {self.loaded=loaded}
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {loaded()}
}
