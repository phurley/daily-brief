import SwiftUI
import WidgetKit
import UIKit
import AppIntents

@main
struct DailyBriefWidgets: WidgetBundle {
    var body: some Widget {
        DailyEventsWidget()
        DailyPhotoWidget()
    }
}

struct EventsEntry: TimelineEntry {
    let date: Date
    let events: [BriefEvent]
    var index: Int = 0
    var unavailable: Bool = false
    var event: BriefEvent? { events.isEmpty ? nil : events[((index % events.count) + events.count) % events.count] }
}

struct SelectEventIntent: AppIntent {
    static var title: LocalizedStringResource = "Show another event"
    static var openAppWhenRun = false
    @Parameter(title: "Event index") var index: Int
    init() {}
    init(index: Int) { self.index = index }
    func perform() async throws -> some IntentResult {
        UserDefaults.standard.set(index, forKey: "eventIndex")
        UserDefaults.standard.set(Date().timeIntervalSince1970, forKey: "eventSelectionTime")
        WidgetCenter.shared.reloadTimelines(ofKind: "DailyEventsWidget")
        return .result()
    }
}

struct EventsProvider: TimelineProvider {
    func placeholder(in context: Context) -> EventsEntry { .init(date: .now, events: []) }
    func getSnapshot(in context: Context, completion: @escaping (EventsEntry) -> Void) {
        completion(.init(date: .now, events: []))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<EventsEntry>) -> Void) {
        Task {
            let now = Date()
            var events: [BriefEvent] = []
            var unavailable = false
            do {
                events = try await BriefData.loadEvents()
                UserDefaults.standard.set(try JSONEncoder().encode(events), forKey: "cachedEvents")
            } catch {
                unavailable = true
                if let data = UserDefaults.standard.data(forKey: "cachedEvents") {
                    events = (try? JSONDecoder().decode([BriefEvent].self, from: data)) ?? []
                }
            }
            let selectionTime = UserDefaults.standard.double(forKey: "eventSelectionTime")
            if selectionTime == 0 {
                UserDefaults.standard.set(now.timeIntervalSince1970, forKey: "eventSelectionTime")
            }
            let elapsed = selectionTime == 0 ? 0 : max(0, Int((now.timeIntervalSince1970 - selectionTime) / 300))
            let index = UserDefaults.standard.integer(forKey: "eventIndex") + elapsed
            // WidgetKit schedules these snapshots; it does not run animation timers.
            let entries = (0..<6).map { step in
                EventsEntry(date: now.addingTimeInterval(Double(step) * 300),
                            events: events, index: events.isEmpty ? 0 : ((index + step) % events.count + events.count) % events.count, unavailable: unavailable)
            }
            completion(Timeline(entries: entries, policy: .after(now.addingTimeInterval(1800))))
        }
    }
}

struct DailyEventsWidget: Widget {
    let kind = "DailyEventsWidget"
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: EventsProvider()) { entry in
            EventsWidgetView(entry: entry)
        }
        .configurationDisplayName("Today’s events")
        .description("A quick look at nearby events from Daily Brief.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge])
    }
}

struct EventsWidgetView: View {
    let entry: EventsEntry
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.widgetFamily) private var family
    private var ink: Color { colorScheme == .dark ? .white : Color(red: 0.08, green: 0.18, blue: 0.21) }
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text("NEARBY & NOTABLE").font(.system(size: 10, weight: .bold))
                Spacer(minLength: 0)
                if entry.unavailable { Image(systemName: "wifi.slash").font(.caption2).accessibilityLabel("Offline, showing saved events") }
            }.foregroundStyle(ink.opacity(0.75))
            if let event = entry.event {
                VStack(alignment: .leading, spacing: 4) {
                    Text(event.title).font(.system(family == .systemSmall ? .subheadline : .headline, design: .rounded, weight: .bold))
                        .lineLimit(2)
                    Text(event.time + " · " + event.place)
                        .font(.caption).lineLimit(family == .systemLarge ? 3 : 1)
                    if family != .systemSmall {
                        if let summary = event.summary {
                            Text(summary).font(.caption).lineLimit(family == .systemLarge ? 7 : 2)
                        }
                        if family == .systemLarge {
                            if let price = event.price { Label(price, systemImage: "ticket").font(.caption) }
                            if let registration = event.registration { Text(registration).font(.caption).lineLimit(3) }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
                Spacer(minLength: 0)
                HStack {
                    Button(intent: SelectEventIntent(index: entry.index - 1)) {
                        Image(systemName: "chevron.left").frame(width: 30, height: 26)
                    }.accessibilityLabel("Previous event")
                    Spacer(minLength: 0)
                    Text("\(entry.index % entry.events.count + 1) / \(entry.events.count)")
                        .font(.caption2.monospacedDigit())
                    Spacer(minLength: 0)
                    Button(intent: SelectEventIntent(index: entry.index + 1)) {
                        Image(systemName: "chevron.right").frame(width: 30, height: 26)
                    }.accessibilityLabel("Next event")
                }.buttonStyle(.plain)
            } else {
                Spacer()
                Text(entry.unavailable ? "Events couldn’t be loaded." : "No events on today’s brief.").font(.headline)
                Spacer()
            }
        }
        .foregroundStyle(ink)
        .containerBackground(for: .widget) {
            colorScheme == .dark ? Color(red: 0.08, green: 0.16, blue: 0.19) : Color(red: 0.91, green: 0.95, blue: 0.94)
        }
        .widgetURL(entry.event?.destination)
    }
}

struct PhotoEntry: TimelineEntry {
    let date: Date
    let photo: BriefPhoto?
    let image: UIImage?
}

struct PhotoProvider: TimelineProvider {
    func placeholder(in context: Context) -> PhotoEntry { .init(date: .now, photo: nil, image: nil) }
    func getSnapshot(in context: Context, completion: @escaping (PhotoEntry) -> Void) {
        completion(.init(date: .now, photo: nil, image: nil))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<PhotoEntry>) -> Void) {
        Task {
            let photo = try? await BriefData.loadPhoto()
            let image: UIImage?
            if let url = photo?.imageUrl, let (data, _) = try? await URLSession.shared.data(from: url) { image = UIImage(data: data) } else { image = nil }
            let entry = PhotoEntry(date: .now, photo: photo ?? nil, image: image)
            completion(Timeline(entries: [entry], policy: .after(.now.addingTimeInterval(6 * 60 * 60))))
        }
    }
}

struct DailyPhotoWidget: Widget {
    let kind = "DailyPhotoWidget"
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: kind, provider: PhotoProvider()) { entry in
            PhotoWidgetView(entry: entry)
        }
        .configurationDisplayName("Photo memory")
        .description("A photo from this day, from Daily Brief.")
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct PhotoWidgetView: View {
    let entry: PhotoEntry
    var body: some View {
        ZStack(alignment: .bottomLeading) {
            if let image = entry.image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                Color(red: 0.13, green: 0.25, blue: 0.29)
                Image(systemName: "photo.on.rectangle.angled").font(.largeTitle).foregroundStyle(.white.opacity(0.8))
            }
            VStack(alignment: .leading, spacing: 2) {
                Text("On this day").font(.caption.weight(.bold))
                Text(entry.photo?.location ?? "Daily Brief photo memory").font(.caption2).lineLimit(1)
            }
            .foregroundStyle(.white).padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(.black.opacity(0.45))
        }
        .containerBackground(for: .widget) { Color.black }
        .widgetURL(BriefData.baseURL)
    }
}
