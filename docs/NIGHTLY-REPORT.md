# Nightly report

Every morning Lanloom writes a short report about the night: what happened in your network since the previous report, in plain words. It is made from Lanloom's own data by fixed rules. No AI and no online service is involved in making it, and nothing leaves the host unless you switch on a delivery channel. (If you set up AI, a note can be explained on request, and an optional paragraph *In plain words* stands above the report – written after the report is made, never part of it; without an answer it is simply missing, and it goes into a delivery only if you allow it. See [integrations/ai.md](integrations/ai.md#nightly-report-in-plain-words).)

## When

- Every day at **06:30** in the report time zone (both can be changed under *Settings → Nightly report*). The report covers the time since the previous report and carries the date of that morning.
- **Time zone:** the one you set; if empty, the `TZ` of the container, otherwise the time zone of the browser you use Lanloom with.
- **Lanloom was not running at report time?** The report is made at the next start and says so in one line. If Lanloom was off for several mornings, one report covers the whole time. Nothing is lost and there is no message about it.
- The first report comes on the first morning after installation.

## What is in it

At the top one line: **All quiet**, or **N things need a look**. Below it one section per source. A section only appears when its source exists (no connections set up, no connection section). Times are shown in the report time zone, devices with the names you gave them.

| Section | What it reports | Needs a look when |
| --- | --- | --- |
| Internet | outages of the router's internet port (from SNMP counters of the gateway) with start, end and length; a switch to another uplink when an adapter reports one | an outage lasted a minute or longer, or the line is still down |
| Devices | new devices, devices not seen for a day, devices that went offline and came back (with times) | a new device appeared, a device is gone for a day, network equipment or a server is still offline |
| Connections | availability of each connection (hub, broker, SNMP, controller, adapters) over the night | a connection has not answered for 30 minutes and is still down |
| Availability | only once the history exists ([details](AVAILABILITY.md#in-the-nightly-report)): how many devices and connections had no gap in the last 7 days, the gaps in all, the typical repair time, and every wobbly device | a device or connection had three or more gaps in 7 days |
| Sign-ins | connections whose account is refused | the sign-in is refused right now |
| Counter check | only when hours were compared: the router's uplink against NetFlow and this host's interfaces against its connections ([details](PERFORMANCE.md#counter-check)) | traffic was counted twice or not at all in at least one hour |
| Data paths | data sent to the internet by country in the last 24 hours, and the calm hints of the [Data paths](DATA-PATHS.md) tab | every hint (new country, unusual upload, network that should not reach the internet) |
| Home automation | one section per hub connection: how many automations and scripts ran, the busiest ones, scripts that call each other in a loop, lookups the bridge paused after errors ([details](HUB-BRIDGE.md#nightly-report)); names of people-related things never appear | an automation or script ran 60 times or more within one hour, a script loop exists, or the bridge refused a message outside its allow-list |
| Controller configuration | changes to firewall rules, networks, wireless networks, port forwards and zones compared with the previous report; firmware versions that changed | every configuration change |
| Changes written | only with the external executor set up, or when a change ran in the window: what the executor changed on the controller (done, undone, refused before writing), with user, time and the devices ([details](FIREWALL-EDITOR.md#changing-a-device-network-fixed-address-name)); rehearsals on the sample network never appear | a change was put back on its own, or could not be put back |
| Device families | one section per connection of a device-family adapter that brings its own (for example the hypervisor: nodes offline, guests stopped, added or removed, a guest firewall switched off or on, network cards moved to another bridge or VLAN, guest rules changed, failed backups, findings; a zone firewall: rules added, removed, switched or acting differently, port forwarding added or removed, management open on the internet side) | what the family marks as needing a look |
| Certificates | TLS certificates of the addresses Lanloom connects to (hub, controller, broker, adapters) | a certificate expires within 14 days or has expired (under 30 days is a note) |
| AI | only when AI is set up: requests answered per day (local and cloud), the daily limit for cloud requests if it was reached, failed requests, new suggestions for unknown devices that wait for a decision | the limit was reached, or requests failed and none succeeded |
| Storage | only once a clean-up ran or a growth history exists: the night's clean-up (entries removed, space freed), the database size and its growth per day ([details](PERFORMANCE.md#storage-and-retention)) | the database could not be compacted for lack of space, or the data volume would be full within 30 days at the current growth |
| Inventory | only when there is news: a device became missing or probably gone, a retired device is back, a warranty or the end of a device's updates came within its warning time or passed, provisional entries taken off the list ([details](INVENTORY.md#nightly-report)) | every one of these except the removed provisional entries |
| Backup | the age of the newest backup, automatic backups written that night, a restore from a backup | automatic backups are on and the newest backup is older than twice the interval (at least 25 hours), or the last automatic backup failed |
| Notifications | only once anybody uses notification rules, push, planned maintenance or the heartbeat: messages sent, events held back by quiet hours, events during planned maintenance (with its names), flaps, repeats in the cool-down, events over the daily limit ([details](NOTIFICATIONS.md)) | a message could not be delivered, or the heartbeat does not get through |
| Lanloom | a new version, Lanloom's own updates and restarts (shown as maintenance, never as a fault), sources that stopped sending data, free space on the data volume | a new version is available, a source sent nothing for 30 minutes, less than 10 % or 1 GB is free |

Short interruptions, a phone that sleeps at night or a 40-second flap of the line are listed as notes, not as things that need a look.

## The Reports tab

- The newest report opens first; older ones are in the **archive** (kept for 30 days by default; 7, 14, 30, 90 or 365 days).
- Things that need a look can be **ticked off**. The marks are stored with the report and shown with the time.
- An unread report shows a **badge** on the Reports tab, on the map and in the phone tab bar. Opening the report marks it read.
- **Preview now** shows how the next report would look at this moment. It is neither saved nor sent.

## Delivery (optional)

The channels set up here are also the shared channels of the [notifications](NOTIFICATIONS.md): once a channel is complete, everybody can choose it in their own rules, even when the nightly delivery itself is off.

Off by default. Under *Settings → Nightly report → Delivery* you can switch on any of these channels. Each one sends a short version: a title, the headline, two lines with the most important points and, if you entered the address of your Lanloom, a link to the report. Device ids and other internal values are never sent.

| Channel | What you enter |
| --- | --- |
| E-mail | SMTP server, port, encryption (STARTTLS, TLS, or none inside your own network), optional user name and password, sender, up to five recipients |
| Webhook | the address; Lanloom sends a JSON `POST` with `title`, `headline`, `lines`, `points`, `day` and `link` |
| Push topic | an ntfy-compatible server (also your own), the topic and an optional access token |
| Chat bot | the token of your own bot for a common messenger's bot interface, and the chat id |

Passwords, tokens, the webhook address and the push topic are stored encrypted like all other credentials (see [SECURITY.md](../SECURITY.md)), never shown again and never logged. **Send test** sends a sample message. A failed delivery is retried by itself (up to three times, every 15 minutes) and shown quietly under the report.

## For developers: adding a section

Device-family adapters bring their section through their manifest (`report`, see [INTEGRATIONS.md](INTEGRATIONS.md)): a pure function of last night's and tonight's neutral state. The report calls it once per connection and keeps a compact copy of the state for the next morning.

Other features use the register directly:

Sections are contributions in a register (`lanloom/nightly/contrib.py`). A new feature adds its own:

```python
from lanloom.nightly import Contribution, register
from lanloom.nightly.contrib import item, section

register(Contribution("backups", 55, "report.sec.backups",
                      available=lambda ctx: ...,          # is the source there at all?
                      build=lambda ctx: section("backups", "report.sec.backups", "report.backups.calm", [
                          item("backup:%d" % t, "report.backups.failed", "warn", True, at_at=t)])))
```

`ctx` gives the time window (`since`, `until`), the event journal (`ctx.events(kind, ...)`), the previous report and the application core. Texts are translation keys; variables ending in `_at` are times, `_dur` durations, `_bytes` byte counts, `_cc` country codes and `_key` translation keys. Events come from the journal (`lanloom/nightly/journal.py`), which compares the live state once a minute and writes a row for every change.
