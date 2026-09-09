import SwiftUI
import WidgetKit
import UIKit

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
}

struct EventsProvider: TimelineProvider {
    func placeholder(in context: Context) -> EventsEntry { .init(date: .now, events: []) }
    func getSnapshot(in context: Context, completion: @escaping (EventsEntry) -> Void) {
        completion(.init(date: .now, events: []))
    }
    func getTimeline(in context: Context, completion: @escaping (Timeline<EventsEntry>) -> Void) {
        Task {
            let events = (try? await BriefData.loadEvents()) ?? []
            let entry = EventsEntry(date: .now, events: events)
            completion(Timeline(entries: [entry], policy: .after(.now.addingTimeInterval(30 * 60))))
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
        .supportedFamilies([.systemSmall, .systemMedium])
    }
}

struct EventsWidgetView: View {
    let entry: EventsEntry
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Label("Nearby & notable", systemImage: "calendar")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            if entry.events.isEmpty {
                Spacer()
                Text("No events on today’s brief.").font(.headline)
                Spacer()
            } else {
                ForEach(entry.events.prefix(2)) { event in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(event.title).font(.headline).lineLimit(1)
                        Text([event.time, event.place].filter { !$0.isEmpty }.joined(separator: " · "))
                            .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                if entry.events.count > 2 { Text("+\(entry.events.count - 2) more today").font(.caption2).foregroundStyle(.secondary) }
            }
        }
        .containerBackground(for: .widget) { Color(red: 0.91, green: 0.95, blue: 0.94) }
        .widgetURL(BriefData.baseURL)
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
