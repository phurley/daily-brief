# Daily Brief for iPhone

Native iPhone wrapper for [Daily Brief](https://phurley.github.io/daily-brief/), plus two WidgetKit widgets:

- **Daily best bets** reads `widget-events.json`, the same public ranked recommendations as the web brief. The compact contract carries edition ID/date, original source time and publication time.
- Events adapt to light/dark appearance and display one event per card. Previous/next buttons browse in place; timeline entries rotate every five minutes (timing is controlled by iOS). WidgetKit does not support horizontal swipes or continuously scrolling text. The large widget includes additional summary, price, and registration details.
- Tapping an event hands its website URL to the system browser through the app, rather than loading the brief. Safari opens when configured as the default browser.
- **Photo memory** reads `photos.json` and shows the first photo for today in America/Detroit time.

The app icon is a custom sunrise, Detroit skyline, and newspaper illustration.

Open `DailyBrief.xcodeproj` in Xcode, select an iPhone simulator or device, and run. The widgets refresh from the hosted JSON independently of the app. Cached events
store edition date and last successful fetch time. Previous-day data is rejected,
and an explicit midnight timeline entry clears old events even if iOS delays a
refresh. Current-day cached results show Saved and their fetch time.

The web wrapper uses WKNavigationDelegate and WKUIDelegate to keep internal
navigation in the brief and dispatch HTTP(S) external links (including new-window
links) to the system browser. Unsupported schemes are rejected. Navigation
failures offer Retry.

Run simulator regression checks with:

```sh
xcodebuild test -project DailyBrief.xcodeproj -scheme DailyBrief \
  -destination 'platform=iOS Simulator,name=iPhone 17 Pro' CODE_SIGNING_ALLOWED=NO
```

The checked-in Xcode project includes DailyBriefTests; regenerate after target
changes with `xcodegen generate` from `project.yml`. Tests cover external link
dispatch, scheme policy, retry state and cache validity at Detroit midnight.
Physical-device browser handoff and VoiceOver are still manual acceptance checks.
See [mobile edition operations](docs/mobile-edition.md) for recurring publication
and offline cache behavior.
