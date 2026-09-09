import Foundation

enum BriefData {
    static let baseURL = URL(string: "https://phurley.github.io/daily-brief/")!
    static let eventsURL = baseURL.appending(path: "events.json")
    static let photosURL = baseURL.appending(path: "photos.json")

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
        return document.today.sorted { $0.start < $1.start }
    }

    static func loadPhoto() async throws -> BriefPhoto? {
        let (data, _) = try await URLSession.shared.data(from: photosURL)
        let document = try JSONDecoder().decode(PhotosDocument.self, from: data)
        return document.days.first(where: { $0.date == today })?.photos.first
    }
}

struct EventsDocument: Decodable { let today: [BriefEvent] }
struct PhotosDocument: Decodable { let days: [PhotoDay] }
struct PhotoDay: Decodable { let date: String; let photos: [BriefPhoto] }

struct BriefEvent: Decodable, Identifiable {
    let id: String
    let title: String
    let start: String
    let venue: String?
    let city: String?

    var time: String {
        guard let date = ISO8601DateFormatter().date(from: start) else { return "Today" }
        return date.formatted(date: .omitted, time: .shortened)
    }

    var place: String {
        [venue, city].compactMap { $0 }.joined(separator: " · ")
    }
}

struct BriefPhoto: Decodable {
    let imageUrl: URL
    let takenDate: String
    let location: String?
    let description: String?
}
