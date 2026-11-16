# AI: local or cloud

Lanloom can let an AI explain things in plain words: a device on the map, a note of the nightly report, a firewall rule. An **Explain** button appears at those places once an AI is set up. The answer is short and comes in the language of the app. With an AI set up there is more (see [What the AI can do](#what-the-ai-can-do)): **Ask your network** answers questions and links to the devices and views it talks about, the nightly report gets a paragraph **In plain words**, the AI can **suggest what an unknown device is**, and the firewall editor can show **suggestions as a draft**.

The AI only reads and explains. It never changes a setting, never writes configuration and never calls the executor; every change still goes through the preview and your confirmation (see [FIREWALL-EDITOR.md](../FIREWALL-EDITOR.md)).

AI is **off by default**. Nothing leaves the host until you set up a service and switch it on. Setup is under **Settings → AI** (the second sub-tab next to *General*), in two cards: *Local AI* and *Cloud AI*.

Supported services: [COMPATIBILITY.md](../COMPATIBILITY.md#ai-services).

## Local or cloud

| | Local AI | Cloud AI |
|---|---|---|
| Where it runs | a computer in your network | a service on the internet |
| What it receives | the real text | placeholders instead of addresses and names |
| Key | none (optional for compatible servers) | your own key |
| Cost | your own electricity | whatever the service charges you |
| Speed | depends on the computer, see [Hardware](#hardware) | fast |
| Permission | none needed, it stays inside the network | each function needs its own permission, plus a daily limit |

You can set up both. The **order** decides who is asked first: *Local first, cloud only if needed* (default) or *Cloud first, local as fallback*. If the first service does not answer, the next one is tried; the cloud is only used for functions you allowed.

## Way A: on this computer (one click)

Needs the updater sidecar, because the app itself has no rights over the container runtime:

```sh
docker compose --profile updater up -d
```

Then **Settings → AI → Local AI → Install local AI here**. What happens:

1. The app writes a small trigger file (`update/ai-request.json` in the data volume). Only the action, a random nonce and the time are read from it; nothing from the file ever reaches a command.
2. The sidecar downloads the image of the local model server, creates the container `lanloom-ai` and starts it. The first download can take several minutes.
3. Lanloom checks the computer (see [Hardware](#hardware)), downloads the recommended model with a progress bar and asks it a test question.
4. Only when the model has **answered** does the card say *Ready*. A service that merely started does not count.

The model server listens on `127.0.0.1:11434` only (Lanloom uses host networking), so the rest of your network cannot reach it. Models live in the volume `lanloom-ai`. **Remove the local AI service** removes the container and keeps the volume, so a later install does not download the models again. Delete the volume yourself if you want the space back:

```sh
docker volume rm lanloom-ai
```

If a container named `lanloom-ai` exists that Lanloom did not make, it is left alone and the setup stops with a message. If something else already uses port 11434 on this computer, use **Look for local AI** to take that service over instead.

## Way B: the installer option

```sh
curl -fsSL https://raw.githubusercontent.com/robeertm/lanloom/main/deploy/install.sh | sh -s -- --with-ai
```

The installer writes `compose.override.yaml` next to `compose.yaml` and starts the service. It never overwrites an existing override file. Models are downloaded later, when you pick one in **Settings → AI**. If you manage the installation by hand, put this into `compose.override.yaml` (it is the output of `docker exec lanloom python -m lanloom.ai.runtime compose`):

```yaml
# Optional local AI service for Lanloom (added by the installer with --with-ai). Safe to delete:
# docker compose down the service, then remove this file. Models live in the volume lanloom-ai.
services:
  ai:
    image: ollama/ollama:latest
    container_name: lanloom-ai
    restart: unless-stopped
    network_mode: host
    security_opt: ["no-new-privileges:true"]
    environment:
      - OLLAMA_HOST=127.0.0.1:11434
      - OLLAMA_KEEP_ALIVE=30m
    volumes:
      - lanloom-ai:/root/.ollama
    labels:
      org.lanloom.role: local-ai
volumes:
  lanloom-ai:
    name: lanloom-ai
```

Then run `docker compose up -d` and use **Look for local AI** in the settings.

## Way C: another computer

Use this when the host is small. A stronger computer that is always on runs the model.

1. **Settings → AI → Local AI → Another computer → Create a setup code.** The code looks like `XXXX-XXXX`, is valid for **4 hours** and works **once**. Lanloom suggests good candidates from your inventory: servers, storage and computers that are on.
2. Run the command for the operating system of the other computer. The page shows all three; `<lanloom>` is the address of this page.

   Linux:

   ```sh
   curl -fsSL "http://<lanloom>:7380/api/ai/installer/linux?code=XXXX-XXXX" | sh
   ```

   macOS:

   ```sh
   curl -fsSL "http://<lanloom>:7380/api/ai/installer/mac?code=XXXX-XXXX" | sh
   ```

   Windows (PowerShell):

   ```powershell
   & ([scriptblock]::Create((irm "http://<lanloom>:7380/api/ai/installer/windows?code=XXXX-XXXX")))
   ```

   You can also download the script and run it there. This page notices when the pairing has worked.

What the script does, in order:

1. Measures the memory, cores and graphics unit and asks Lanloom for the recommended model. Too little memory stops it, a slow computer gets a warning and a question.
2. Installs the local model server if it is missing (it asks first).
3. Says clearly: **the model server has no password: every device in your network can use it. Only continue in a network you trust.** and asks before it lets the server listen in the network.
4. Makes the server listen on port 11434 and keeps it that way after a restart: on Linux a systemd drop-in (`ollama.service.d/lanloom.conf`), on macOS a LaunchAgent (`org.lanloom.ai`), on Windows a user environment variable plus a firewall rule for private networks only (named *Lanloom local AI*).
5. Waits for the server, downloads the model and pairs with Lanloom. Lanloom itself checks, from the Lanloom server, that the other computer answers on the AI port and that the model answers a question. Only then the pairing counts.

Options: `--yes` and `--model <id>` (shell), `-Yes` and `-Model <id>` (PowerShell). `--yes` answers every question with yes, including the one about the network, so use it only where you have read the warning.

Exit codes of the shell script:

| Code | Meaning |
|---|---|
| 0 | paired, the model answered |
| 1 | wrong option, unsupported system or a model name with forbidden characters |
| 2 | the code is wrong or expired, too many attempts, or Lanloom cannot be reached |
| 3 | stopped: too little memory, or you declined a question |
| 4 | the model server is missing and could not be installed |
| 5 | the server did not start, or pairing or the model test failed |

The pairing endpoints work without a session (the script has none): they need the setup code, are rate limited and accept only private peer addresses. The code is stored only as a hash and is used up after a successful pairing.

## Way D: an existing server

If you already run a model server, choose its kind under *Local AI → Service*, enter the address (with or without `/v1`; Lanloom finds the right form), and use **Load models** and **Test the connection**. **Look for local AI** asks this host and the devices Lanloom already knows, once each, on the AI port. No address ranges are swept, and the check runs on the Lanloom server, not in your browser.

## Cloud services

1. **Settings → AI → Cloud AI**: choose the service, paste your key. The key is stored encrypted, never shown again and never put in answers, logs, exports or reports. *Remove the key* deletes it and switches the service off.
2. **Load models**: the list comes from the service itself and the recommended (small, cheap) model is pre-selected. You can pick another.
3. **Test the connection**: asks a real question and shows the answer and the response time.
4. **The cloud service may be used for**: tick the functions that may use it (devices, notes of the nightly report, firewall rules). Nothing is allowed by default.
5. **Cloud requests per day**: the daily limit (default 50, `0` switches cloud requests off). The usage of today is shown.
6. Switch the card on.

A "local" address that leads to the internet counts as a cloud service, with placeholders, permissions and limit.

## The privacy layer

For cloud services every text passes through the privacy layer before it leaves:

| Hidden | Placeholder |
|---|---|
| IP addresses (IPv4 and IPv6, also with a prefix) | `[IP-1]` |
| hardware addresses | `[MAC-1]` |
| host names, device names and names under private suffixes such as `.local` or `.lan` | `[HOST-1]` |
| wireless network names | `[SSID-1]` |
| serial numbers | `[SERIAL-1]` |
| site names and coordinates | `[PLACE-1]` |

The same value always gets the same placeholder within one request, so the model can still tell devices apart. Lanloom hides what it knows (your device names, wireless networks, serial numbers, sites) and what it can recognise by pattern. The answer is translated back on your host before you see it.

Example. What Lanloom has:

```
Device nas-kitchen at 192.168.1.20 (hardware address aa:bb:cc:dd:ee:01), network Home-Net, serial: SN12345678.
It talks to 192.168.1.1 on port 443.
```

What the cloud service receives:

```
Device [HOST-1] at [IP-1] (hardware address [MAC-1]), network [SSID-1], serial: [SERIAL-1].
It talks to [IP-2] on port 443.
```

If the answer says "[HOST-1] answers on [IP-1]", you see the real name and address.

**Fail closed:** before sending, a final check looks for any address pattern. If one survived, nothing is sent and the error says so. **Preview what is sent** (next to the Explain button) shows the exact text and instructions that would go out, how many values are hidden, and whether the service is inside or outside your network. A local service gets the real values.

## Time limits

Each request has three limits: connecting (a host that is off fails fast, so "down" is never mistaken for "busy"), the first answer, and the rest. A local model on a small computer may take a while to load and to read the question, so local limits are generous (up to 150 seconds to the first answer); cloud limits are shorter (45 seconds). A request returns at once with a job; the page shows the progress ("the model is answering") and fetches the result.

## Hardware

Lanloom measures memory, cores and graphics unit of the computer that runs the model and advises honestly:

| Memory | Recommended model | Download | Note |
|---|---|---|---|
| under 4 GB | none | – | no local AI is offered; use another computer or a cloud service |
| 4 to 8 GB | `llama3.2:3b` | about 2 GB | small model, a warning is shown |
| 8 GB and more | `llama3.1:8b` | about 4.9 GB | |
| 24 GB and more, with a graphics unit | `qwen2.5:14b` | about 9 GB | |

Without a graphics unit the number of cores is the limit: with few cores, answers are slow (reading the question takes most of the time) and Lanloom says so and points to the other ways.

## Troubleshooting

The message in the app is the plain-language text of one of these keys.

| Key | Meaning and what to do |
|---|---|
| `lan_unreachable` | Lanloom cannot reach the other computer on the AI port. Usually a firewall there: allow port 11434 for your private network (the Windows script prints the command). |
| `unreachable` | The service cannot be reached from the Lanloom server. Check the address; a browser on your own computer is not the test, the Lanloom host is. |
| `port_busy` | Something on this computer already uses the AI port. Use *Look for local AI* to take it over. |
| `no_sidecar` | One-click setup needs the updater sidecar: `docker compose --profile updater up -d`, or use Way B or C. |
| `no_runtime`, `no_target` | The sidecar cannot reach the container runtime or cannot find the Lanloom container. Check its mounts and labels. |
| `not_ours` | A container with the same name exists and was not made by Lanloom. Rename or remove it yourself. |
| `pull_failed`, `pull_incomplete` | The download of the service or of the model failed or did not finish. Check disk space and internet access, then try again. |
| `key_missing`, `key_format` | The service needs a key, or the key contains characters a key cannot have. |
| `auth` | The service refused the key. Check it and its permissions. |
| `daily_limit` | The daily limit for cloud requests is reached. Raise it or wait until tomorrow. |
| `rate` | The service says too many requests. Try again later. |
| `privacy` | Not sent: an address would have left the network unhidden. Nothing was lost; use a local service for this item. |
| `not_ready` | No AI may be used for this yet: pick a model, or allow the function for the cloud service. |
| `host_too_small`, `memory` | Too little memory for this model. Choose a smaller model or another computer. |
| `timeout`, `no_answer` | The answer took too long or did not start. A local model may need to load; try again. |
| `code` | The setup code is wrong or expired. Create a new one. |
| `peer` | Pairing only works from a computer in your own network. |

## What the AI can do

Every feature below uses the same services, order, privacy layer and daily limit as *Explain*, and each one needs its own permission before a cloud service may be used for it (Settings → AI, *The cloud service may be used for*). A local service may always be used. Operators and admins see the features; viewers do not. Every request is written to the audit log (the action, never the question or the answer).

### Ask your network

The **Ask** button in the top bar (an icon on narrow screens; on a phone the panel fills the screen) opens a conversation about your network: "Which devices are offline?", "What sends the most to the internet?", "Is anything unusual today?". The Lanloom server builds the facts for each question from its own data – the devices with kind, address, status, maker and how sure the identification is, the availability changes of the last 24 hours, the data paths of the last day, the firewall configuration it has read and the latest nightly report – and sends them with your question. The browser only sends the question and the last few turns of the conversation, which it keeps for this tab only.

The answer names devices with a reference such as `[D3]` and views such as `[view:paths]`; Lanloom turns them into links (a device opens its card in the device list, a view opens that tab) and drops any reference that does not exist. *Preview what is sent* shows the exact text for your question. The conversation can be started over at any time. The answer can only read: it cannot change anything, and it says so when a change could help.

### Nightly report: In plain words

With an AI set up, every nightly report gets a short paragraph *In plain words* above the report: whether something needs a look and the most important points. The rule-based report below stays the reference; if the AI does not answer within four minutes, or no AI may be used, the paragraph is simply missing. An operator can write it (again) for any saved report. Settings → AI, *What the AI may do*:

- *Write a paragraph "In plain words" for each nightly report* (on by default once an AI is set up);
- *Put the paragraph into the delivered report* (off by default): only then does the paragraph go into the e-mail, push message, chat message or webhook (field `plain`).

### Suggestions for unknown devices

On the card of a device Lanloom is not sure about, *Ask AI what this is* sends the clues Lanloom collected (ports, announcements, maker from the hardware address, model, the reasons it already used) and asks for one of Lanloom's kinds with a short reason. The suggestion waits on the card: **Confirm** or **Dismiss**. A confirmed suggestion counts as a weak clue (confidence 0.3, reason "AI hint"): it only fills in a kind where Lanloom has nothing better, and a kind you choose yourself always wins. *Forget suggestion* removes it. With *At night, suggest a kind for up to 5 devices Lanloom is unsure about* (off by default) the AI makes such suggestions for devices it was never asked about; the nightly report then says how many suggestions wait for you. Nothing changes until you confirm.

### Suggestions for networks and the firewall

In the firewall editor (simulation or executor), *AI suggestions* looks at which device sits in which network and suggests moves – for example a TV or a speaker into the smart home network. Only moves the editor itself would allow are shown (never a protected device, never into the management network). Pick the ones you like and choose *Take over as draft*: they join the draft bar, and from there the usual preview, confirmation and – for real changes – passkey signature apply. Asking for suggestions never writes the draft; only your click does.

## What never happens

- The AI never writes configuration, never calls the executor and never changes a setting. Firewall suggestions reach the draft only by your click; device suggestions count only after you confirm them, and then only as a weak clue.
- Nothing is sent to a cloud service without a key, an allowed function and the privacy layer.
- A key is never shown again, logged, exported or put into a report.
- No address range is scanned to find a service.
- A removed service keeps your downloaded models until you delete the volume.

---

Product names are used only to identify compatible products. No affiliation or endorsement: Lanloom is not affiliated with, endorsed or sponsored by their owners; all trademarks belong to their owners.
