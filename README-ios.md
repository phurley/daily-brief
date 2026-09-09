# Daily Brief for iPhone

Native iPhone wrapper for [Daily Brief](https://phurley.github.io/daily-brief/), plus two WidgetKit widgets:

- **Today’s events** reads `events.json`.
- Events adapt to light/dark appearance and display one event per card. Previous/next buttons browse in place; timeline entries rotate every five minutes (timing is controlled by iOS). WidgetKit does not support horizontal swipes or continuously scrolling text. The large widget includes additional summary, price, and registration details.
- Tapping an event hands its website URL to the system browser through the app, rather than loading the brief. Safari opens when configured as the default browser.
- **Photo memory** reads `photos.json` and shows the first photo for today in America/Detroit time.

The app icon is a custom sunrise, Detroit skyline, and newspaper illustration.

Open `DailyBrief.xcodeproj` in Xcode, select an iPhone simulator or device, and run. The widgets refresh from the hosted JSON independently of the app.
