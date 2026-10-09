import Foundation

enum BriefData {
    static let baseURL = URL(string: "https://phurley.github.io/daily-brief/")!
    static let eventsURL = baseURL.appending(path: "recommendations.json")
    static let photosURL = baseURL.appending(path: "photos.json")

    static func dayKey(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(identifier: "America/Detroit")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }

    static var today: String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(identifier: "America/Detroit")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: .now)
    }

    static func loadEvents() async throws -> [BriefEvent] {
        let (data, _) = try await URLSession.shared.data(from: eventsURL)
        let document = try JSONDecoder().decode(EventsDocument.self, from: data)
        return (document.days.first(where: { $0.date == today })?.events ?? []).filter { event in
            let parser = ISO8601DateFormatter()
            guard let start = parser.date(from: event.start) else { return false }
            let end = event.end.flatMap { parser.date(from: $0) }
            guard dayKey(start) <= today && dayKey(end ?? start) >= today else { return false }
            return end.map { $0 > .now } ?? true
        }
    }

    static func loadPhoto() async throws -> BriefPhoto? {
        let (data, _) = try await URLSession.shared.data(from: photosURL)
        let document = try JSONDecoder().decode(PhotosDocument.self, from: data)
        return document.days.first(where: { $0.date == today })?.photos.first
    }
}

struct EventsDocument: Decodable { let days: [RecommendationDay] }
struct RecommendationDay: Decodable { let date: String; let events: [BriefEvent] }
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

    var time: String {
        guard let date = ISO8601DateFormatter().date(from: start) else { return "Today" }
        let formatter = DateFormatter()
        formatter.timeZone = TimeZone(identifier: "America/Detroit")
        formatter.timeStyle = .short
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
