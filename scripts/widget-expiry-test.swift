import Foundation

@main
struct WidgetExpiryCheck {
    static func main() throws {
        func event(start: String, end: String? = nil, expiry: String? = nil) throws -> BriefEvent {
            var object = ["id": "test", "title": "Verified performance", "start": start]
            if let end { object["end"] = end }
            if let expiry { object["reviewExpiresAt"] = expiry }
            return try JSONDecoder().decode(BriefEvent.self, from: JSONSerialization.data(withJSONObject: object))
        }
        let before = BriefEvent.parseTimestamp("2026-10-13T16:59:59-04:00")!
        let after = BriefEvent.parseTimestamp("2026-10-13T17:00:01-04:00")!
        let unknownEnd = try event(start: "2026-10-13T17:00:00.000-04:00")
        precondition(!unknownEnd.hasEnded(at: before))
        precondition(unknownEnd.hasEnded(at: after))
        let knownEnd = try event(start: "2026-10-13T16:00:00-04:00", end: "2026-10-13T17:00:00-04:00")
        precondition(!knownEnd.hasEnded(at: before))
        precondition(knownEnd.hasEnded(at: after))
        let staleReview = try event(start: "2026-10-14T20:00:00-04:00", expiry: "2026-10-13T17:00:00.123456-04:00")
        precondition(!staleReview.hasEnded(at: before))
        precondition(staleReview.hasEnded(at: after))
        precondition(staleReview.timeLabel(relativeTo: before) != "Time unavailable")
        print("Widget start/end and fractional review expiry passed")
    }
}
