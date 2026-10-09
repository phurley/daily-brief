import Foundation

enum BriefData {
    static let baseURL = URL(string: "https://phurley.github.io/daily-brief/")!
    static let eventsURL = baseURL.appending(path: "widget-events.json")
    static let photosURL = baseURL.appending(path: "photos.json")

    static var today: String { dateKey(.now) }
    static func dateKey(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "America/Detroit")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }
    static var nextMidnight: Date {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Detroit")!
        return calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: .now))!
    }
    static func loadEvents() async throws -> EventsDocument {
        let request = URLRequest(url: eventsURL, cachePolicy: .reloadRevalidatingCacheData, timeoutInterval: 10)
        let (data, response) = try await URLSession.shared.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        let document = try JSONDecoder().decode(EventsDocument.self, from: data)
        guard document.schemaVersion == 1, document.editionDate == today else { throw URLError(.cannotParseResponse) }
        return document
    }

    static func loadPhoto() async throws -> BriefPhoto? {
        let (data, _) = try await URLSession.shared.data(from: photosURL)
        let document = try JSONDecoder().decode(PhotosDocument.self, from: data)
        return document.days.first(where: { $0.date == today })?.photos.first
    }
}

struct EventsDocument: Codable {
    let schemaVersion: Int
    let editionId: String
    let editionDate: String
    let generatedAt: String
    let sourceGeneratedAt: String
    let events: [BriefEvent]
}
struct CachedEvents: Codable {
    let document: EventsDocument
    let fetchedAt: Date
    func usable(on date: Date) -> Bool { document.schemaVersion == 1 && document.editionDate == BriefData.dateKey(date) }
}
struct PhotosDocument: Decodable { let days: [PhotoDay] }
struct PhotoDay: Decodable { let date: String; let photos: [BriefPhoto] }

struct BriefEvent: Codable, Identifiable {
    let id: String
    let title: String
    let start: String
    let end: String?
    let venue: String?
    let city: String?
    let url: URL?
    let summary: String?
    let price: String?
    let registration: String?
    let timePrecision: String?

    func hasEnded(at date: Date) -> Bool {
        guard let end, let closing = ISO8601DateFormatter().date(from: end) else { return false }
        return closing <= date
    }
    var time: String { timeLabel(relativeTo: .now) }
    func timeLabel(relativeTo reference: Date) -> String {
        guard let date = ISO8601DateFormatter().date(from: start) else { return "Time unavailable" }
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(identifier: "America/Detroit")
        if ["date", "unknown"].contains(timePrecision ?? "") {
            formatter.dateStyle = .short
            let day = BriefData.dateKey(date) == BriefData.dateKey(reference) ? "Today" : formatter.string(from: date)
            return day + " · Time not confirmed"
        }
        formatter.timeStyle = .short
        if BriefData.dateKey(date) != BriefData.dateKey(reference) { formatter.dateStyle = .short }
        return formatter.string(from: date)
    }

    var place: String {
        [venue, city].compactMap { $0 }.joined(separator: " · ")
    }

    var destination: URL? {
        guard let url, ["https", "http"].contains(url.scheme?.lowercased() ?? "") else { return nil }
        var components = URLComponents()
        components.scheme = "dailybrief"
        components.host = "event"
        components.queryItems = [URLQueryItem(name: "url", value: url.absoluteString)]
        return components.url
    }
}

struct BriefPhoto: Decodable {
    let imageUrl: URL
    let takenDate: String
    let location: String?
    let description: String?
}
