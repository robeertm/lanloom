# Omada Network (controller API): read adapter

Lanloom reads an Omada controller (a software controller or a hardware controller) over its **Open API** with an **application made only for Lanloom, whose role only views**. Nothing is ever changed on the controller from Lanloom in this release: the adapter can send exactly one `POST` (the exchange of the application's client ID and secret for an access token) and `GET` requests on the paths listed below. A request outside that list is refused before it leaves Lanloom - no change, no restart, no firmware action, no block or disconnect of a client.

What you get:

- **Devices of the site:** gateway, switches and access points with model, firmware, address, state (connected, waiting to be adopted, offline), uplink and how many clients each carries. They feed the network view (who hangs on which device and port) and the inventory.
- **Clients:** wired clients with the switch (or gateway) and port, wireless clients with the access point, wireless network and band; fixed addresses become fixed-address entries; blocked and guest clients are marked.
- **Switch ports:** link, speed, power over Ethernet, the port profile and its native network, the device behind the port, and which port is the uplink - for the front panels of the switch cards. Switches that are offline are not asked.
- **Networks:** LAN networks and VLANs with address range, VLAN ID and whether the controller serves DHCP.
- **Wireless networks:** name, bands, security (open, personal, enterprise, per-user key), VLAN and whether the name is hidden or the network is a guest network.
- **Access rules** (gateway, switch and access point rules): shown in order with action and state, never written. The night report notices when one is added, removed, switched on or off, or acts differently.
- **Internet connections** of the gateway with their state.
- **Checks** in plain words: a device is not connected, an internet connection is down, a wireless network has no encryption (a guest network only as a note), access rules are switched off.
- **Night report:** the family contributes its own section (devices that went offline or came back, new or removed devices, firmware changes, wireless networks that appeared, vanished or changed, access rules that changed, and the checks above); it appears only when this connection exists.

Never kept: the passphrase and the per-user key settings of a wireless network, serial numbers, account data, the client secret. Lanloom never asks for the detail of a wireless network (the only reply that carries its passphrase); the list it reads does not contain one. Hardware addresses are only used to match devices in the topology; in the configuration view every object has an opaque id.

## Create an application for Lanloom

You need an Open API application with a role that only views, and its three values. The menu names below are those of controller 5.x; newer releases use similar names.

1. In the controller, open **Settings → Platform Integration → Open API** and choose **Add New App**.
2. Name it `Lanloom`, choose **Client Mode** (client credentials), and give it a **role that only views** for the site Lanloom should read (not an administrator). Save.
3. Copy the **Client ID** and the **Client Secret** (the eye icon shows the secret), and note the **Omada ID** shown with the application.
4. Make sure the computer Lanloom runs on can reach the controller's web port (`8043` on a software controller; hardware controllers use `443`).

Then in Lanloom: **Settings → Connections → + Omada Network (controller API)**.

The Open API does not tell Lanloom the role of an application, so Lanloom cannot check it the way it does for families whose API reports it - the role in step 2 is what keeps the application read only on the controller. Lanloom's own side cannot send a change in any case.

## Settings

| field | meaning |
|---|---|
| Address | `https://<controller>:8043` (a software controller) or the address of the hardware controller |
| Controller ID (Omada ID) | optional: the controller announces it by itself; enter it only if the connection test says the ID is not known |
| Client ID | of the application from step 3 |
| Client secret | its secret; stored encrypted, never shown again |
| Site | the name of the site; empty = the primary site (or the only one) |
| Accept a self-signed certificate | needed unless the controller has a certificate your system trusts (a fresh installation uses its own) |

## What Lanloom sends

Exactly one code path sends requests, through an allow-list that is checked before anything leaves the app:

- `POST /openapi/authorize/token?grant_type=client_credentials` with exactly the controller ID, the client ID and the client secret. The access token comes back; it lives two hours, is kept in memory only, and is renewed shortly before it ends. At most 6 exchanges per hour; after a refused exchange Lanloom waits 15 minutes, so a wrong secret cannot lock anything.
- `GET /api/info` (no sign-in; announces the controller version and ID) and `GET` on the lists of devices, clients, LAN networks, LAN profiles, wireless networks of every wireless group, the three access rule lists, the gateway's internet connections and the port list and port state of each switch. Nothing else can be sent - the paths are in `lanloom/integrations/omada/read.py` (`GET_PATHS`).

Cadence: every minute the devices and clients, every five minutes everything. At most 24 switches and 2 gateways are asked per poll.

## Releases

| release | reads |
|---|---|
| Controller 5.x with the Open API (v1) | everything above; field names and codes checked against the published API specification, not yet against a running controller |
| Controller 6.x | expected to read the same; not checked yet |
| Controllers before the Open API | not supported (the adapter needs the application login) |

An area that a release does not have, or that the role may not read, is left out and the others still show; the capability list of the connection shows which. Tested versions are listed in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Troubleshooting

| status | what to do |
|---|---|
| Account was rejected | client ID or secret wrong, or the application is not in *Client Mode*; Lanloom pauses for 15 minutes |
| The controller does not know this controller ID | copy the Omada ID shown with the application into *Controller ID* (or empty it, so the controller's own announcement is used) |
| Access denied for this account | the role may not read devices; give it a view role for the site (some areas, such as access rules, may be left out on purpose) |
| Settings incomplete or invalid | site name not found, or the controller did not announce its ID and none was entered |
| Not reachable | check address and port (`8043` software controller, `443` hardware controller) |
| TLS / certificate problem | tick *Accept a self-signed certificate* |
| Sign-in paused for a while | too many failed or repeated sign-ins; fix the secret, Lanloom tries again after the pause |
| Switch ports are missing | the switch is offline, or the role may not read switch details |

## Help us test

If you run Lanloom against a real controller, the most useful feedback is: the controller release, whether the devices, ports and wireless networks match what the controller shows, an area that is missing or looks wrong, and the status line of the connection. Before you share a screenshot or a log, black out public addresses, host names and names you consider private.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
