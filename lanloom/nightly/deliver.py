"""Optional delivery of the nightly report summary (three lines and a link). Every channel is off by default.

Channels: e-mail (SMTP), webhook (JSON POST), push topic (ntfy-compatible server) and a chat bot (a messenger bot interface)
with the user's own token. Secrets come from the encrypted secret store and are only handed to the one request that
needs them; error results are reason keys, never exception texts (they could echo a URL with a token).
"""
import json
import re
import smtplib
import socket
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .. import __version__

TIMEOUT = 15
EMAIL_RE = re.compile(r"\A[^@\s,;<>]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}\Z")
HOST_RE = re.compile(r"\A[A-Za-z0-9.-]{1,253}\Z|\A\[?[0-9A-Fa-f:.]{2,45}\]?\Z")
URL_RE = re.compile(r"\Ahttps?://[^\s]{3,500}\Z")
TOPIC_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
BOT_TOKEN_RE = re.compile(r"\A\d{3,15}:[A-Za-z0-9_-]{20,80}\Z")
CHAT_RE = re.compile(r"\A(-?\d{1,20}|@[A-Za-z0-9_]{4,64})\Z")
BOT_API = "https://api.telegram.org"     # identifier: public chat bot API host (used only when the user sets up the bot)


def clean_channel(ch: str, cfg: dict) -> dict:
    """Plain (non-secret) settings of a channel, checked. Raises ValueError(reason key)."""
    out = {"enabled": bool(cfg.get("enabled"))}
    if ch == "email":
        host = str(cfg.get("host") or "").strip()
        if host and not HOST_RE.match(host):
            raise ValueError("report.err.email_host")
        try:
            port = int(cfg.get("port") or 587)
        except (TypeError, ValueError):
            raise ValueError("report.err.email_port")
        if not 1 <= port <= 65535:
            raise ValueError("report.err.email_port")
        security = cfg.get("security") if cfg.get("security") in ("starttls", "tls", "none") else "starttls"
        sender = str(cfg.get("sender") or "").strip()
        to = ", ".join(a.strip() for a in re.split(r"[,;\s]+", str(cfg.get("to") or "")) if a.strip())
        if sender and not EMAIL_RE.match(sender):
            raise ValueError("report.err.email_address")
        if to and (len(to.split(", ")) > 5 or not all(EMAIL_RE.match(a) for a in to.split(", "))):
            raise ValueError("report.err.email_address")
        out.update(host=host, port=port, security=security, username=str(cfg.get("username") or "").strip()[:128],
                   sender=sender, to=to)
    elif ch == "ntfy":
        server = str(cfg.get("server") or "").strip().rstrip("/")
        if server and not URL_RE.match(server):
            raise ValueError("report.err.url")
        out["server"] = server
    elif ch == "chatbot":
        chat = str(cfg.get("chat_id") or "").strip()
        if chat and not CHAT_RE.match(chat):
            raise ValueError("report.err.chat")
        out["chat_id"] = chat
    return out


def clean_secret(ch: str, field: str, value: str) -> str:
    value = value.strip()
    if ch == "webhook" and field == "url" and not URL_RE.match(value):
        raise ValueError("report.err.url")
    if ch == "ntfy" and field == "topic" and not TOPIC_RE.match(value):
        raise ValueError("report.err.topic")
    if ch == "chatbot" and field == "token" and not BOT_TOKEN_RE.match(value):
        raise ValueError("report.err.bot_token")
    if len(value) > 1000:
        raise ValueError("report.err.secret")
    return value


def missing(ch: str, cfg: dict, secrets: dict) -> bool:
    if ch == "email":
        return not (cfg.get("host") and cfg.get("sender") and cfg.get("to"))
    if ch == "webhook":
        return not secrets.get("url")
    if ch == "ntfy":
        return not (cfg.get("server") and secrets.get("topic"))
    if ch == "chatbot":
        return not (cfg.get("chat_id") and secrets.get("token"))
    return True


def send(ch: str, cfg: dict, secrets: dict, msg: dict) -> tuple:
    """(ok, reason key). Never raises."""
    try:
        if ch == "email":
            _email(cfg, secrets, msg)
        elif ch == "webhook" and msg.get("kind") == "notification":
            _post(secrets["url"], json.dumps({"type": "notification", "event": msg.get("event") or "", "severity": msg.get("severity") or "info",
                                              "title": msg["title"], "text": msg["lines"][0] if msg.get("lines") else msg["text"],
                                              "link": msg.get("url") or None, "time": int(msg.get("time") or 0)}).encode(),
                  {"Content-Type": "application/json"})
        elif ch == "webhook":
            _post(secrets["url"], json.dumps({"title": msg["title"], "headline": msg["headline"], "lines": msg["lines"],
                                              "points": msg["points"], "day": msg["day"], "link": msg.get("url") or None,
                                              **({"plain": msg["plain"]} if msg.get("plain") else {})}).encode(),
                  {"Content-Type": "application/json"})
        elif ch == "ntfy":
            headers = {"Title": msg["title"].encode("utf-8").decode("latin-1", "replace"), "Tags": "lanloom"}
            if msg.get("url"):
                headers["Click"] = msg["url"]
            if secrets.get("token"):
                headers["Authorization"] = "Bearer " + secrets["token"]
            _post("%s/%s" % (cfg["server"], secrets["topic"]), msg["text"].encode("utf-8"), headers)
        elif ch == "chatbot":
            _post("%s/bot%s/sendMessage" % (BOT_API, secrets["token"]),
                  json.dumps({"chat_id": cfg["chat_id"], "text": msg["title"] + "\n" + msg["text"],
                              "disable_web_page_preview": True}).encode(), {"Content-Type": "application/json"})
        else:
            return False, "report.send.error"
    except KeyError:
        return False, "report.err.channel_incomplete"
    except smtplib.SMTPAuthenticationError:
        return False, "report.send.auth"
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused):
        return False, "report.send.rejected"
    except urllib.error.HTTPError as exc:
        return False, "report.send.auth" if exc.code in (401, 403) else "report.send.rejected"
    except (ssl.SSLError, ssl.CertificateError):
        return False, "report.send.tls"
    except (socket.timeout, TimeoutError):
        return False, "report.send.timeout"
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), (ssl.SSLError, ssl.CertificateError)):
            return False, "report.send.tls"
        return False, "report.send.unreachable"
    except (OSError, smtplib.SMTPException):
        return False, "report.send.unreachable"
    except Exception:
        return False, "report.send.error"
    return True, "report.send.ok"


def _post(url: str, body: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers=dict(headers, **{"User-Agent": "lanloom/%s" % __version__}))
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
        resp.read(4096)


def _email(cfg: dict, secrets: dict, msg: dict) -> None:
    m = EmailMessage()
    m["Subject"] = msg["title"]
    m["From"] = cfg["sender"]
    m["To"] = cfg["to"]
    m["Date"] = formatdate(localtime=False)
    m["Message-ID"] = make_msgid(domain=cfg["sender"].split("@")[-1])
    m.set_content(msg["text"] + "\n")
    ctx = ssl.create_default_context()
    if cfg.get("security") == "tls":
        server = smtplib.SMTP_SSL(cfg["host"], int(cfg.get("port") or 465), timeout=TIMEOUT, context=ctx)
    else:
        server = smtplib.SMTP(cfg["host"], int(cfg.get("port") or 587), timeout=TIMEOUT)
    with server:
        if cfg.get("security") == "starttls":
            server.starttls(context=ctx)
        if cfg.get("username") and secrets.get("password"):
            server.login(cfg["username"], secrets["password"])
        server.send_message(m, to_addrs=[a.strip() for a in cfg["to"].split(",") if a.strip()])
