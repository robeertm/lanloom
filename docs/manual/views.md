# Around the views

This page explains the parts of the app that surround the views: the *Right now* rail, the *About this view* rail, the
live state in the header, the deep links and the keys. Each view has its own page, listed in the
[manual](README.md):

[Overview](overview.md) · [Map](map.md) · [Network](network.md) · [Devices](devices.md) · [Inventory](inventory.md) ·
[Firewall](firewall.md) · [Data paths](data-paths.md) · [Nightly report](nightly-reports.md) · [Automations](automations.md) · [Running](running.md) · [Updating](updating.md) · [Settings](settings.md) ·
[AI](ai.md)

- [Right now](#right-now) · [About this view](#about-this-view) · [Live state and live grade](#live-state-and-live-grade) · [Deep links and the back key](#deep-links-and-the-back-key) · [Keys](#keys)

<a id="right-now"></a>
## Right now

Key `L`, the pulse button in the header, or the command "Show or hide Right now" in the search (`Ctrl K`). Four
sections, each foldable:

- **Noteworthy** – the same list as on the [start page](overview.md);
- **Active conversations** – the traces with measured traffic right now, busiest first, with their rate;
- **Changes** – what the event journal recorded in the last six hours: devices that stopped answering or came back, new
  devices, connections that failed or recovered, internet outages, Lanloom's own start;
- **New devices** – first seen since yesterday, with the origin of the address.

The rail refreshes every five seconds while it is shown and rests while the tab is in the background.

![Right now rail](../screenshots/overview-rail-dark.png)

<a id="about-this-view"></a>
## About this view

Key `I`, the *i* button, or the command in the search. A short explanation of the open view and a link to its page in
the [manual](README.md). On screens of 1900 pixels and wider it is open until you close it; Lanloom remembers your choice
in this browser.

On screens narrower than 1280 pixels both rails slide over the page instead of sitting beside it; on a phone they fill
the screen and close with the ✕ button or `Esc`.

<a id="live-state"></a>
## Live state and live grade

The pill in the header shows the state of the live feed:

| Text | Meaning |
|---|---|
| Live · 14:32:05 | updates arrive; the time of the last one (on narrow screens only the time) |
| Updated 12 s ago | connected, but nothing arrived for more than five seconds |
| Disconnected | the connection to Lanloom is lost; it is retried on its own, or press **Reconnect** |
| Paused | the tab is in the background; the feed rests and resumes when you come back |

The dot is the overall live grade. Clicking the pill opens the live grade dialog: the grade, rhythm and age of every
source, what each grade means, and the **clock offset** between this browser and the server. When the clocks differ by
two seconds or more, times such as "seen 5 s ago" may look shifted by that much.

<a id="deep-links"></a>
## Deep links and the back key

| Address | Opens |
|---|---|
| `#/overview` | the [start page](overview.md) |
| `#/map`, `#/network` | the board ([map](map.md), [network](network.md)) |
| `#/map/<component>` | the board with the card of that component (the page switches to the other board if needed) |
| `#/map?zone=<area>`, `#/map?only=problems` | the board filtered |
| `#/devices/<device>` | the [device list](devices.md) with that device's card |
| `#/devices?show=new` | the device list filtered (`new`, `unsure`, `offline`) |
| `#/devices?view=stability` | the stability list |
| `#/inventory`, `#/inventory?state=missing` | the [inventory](inventory.md), optionally filtered |
| `#/reports/<day>` | that [nightly report](nightly-reports.md) |
| `#/automations?view=<view>` | the [Automations](automations.md) tab (`list`, `who`, `timeline`, `plan`, `rooms`, `health`) |
| `#/firewall/device/<address>` | the [firewall editor](firewall.md) in change mode with the sheet of that device |
| `#/firewall/pick` | the firewall editor with a searchable list of the devices that can be changed |
| `#/running` | the [Running](running.md) view |
| `#/settings/<page>` | a [settings](settings.md) page |

Opening a card, a report or a filter changes the address, so the back key closes the card or returns to the report or
filter you had before.

<a id="keys"></a>
## Keys

| Key | Action |
|---|---|
| `L` | show or hide *Right now* |
| `I` | show or hide *About this view* |
| `/`, `Ctrl K`, `⌘ K` | search and commands |
| `?` | the keyboard help (every key, also for the tree view of the Network view) |
| `Esc` | close a card, a dialog or a rail that lies over the page; the focus returns to where you came from |

Keys do nothing while you type in a field. Everything can be done with the keyboard; see [Accessibility](../ACCESSIBILITY.md).
