"""The optional local AI service next to Lanloom: one place for its container settings.

Used by three parties so they always agree:

* the updater sidecar (``updater/localai.py``) creates the service when the app asks for it,
* the installer (``deploy/install.sh --with-ai``) asks the image for a compose snippet
  (``docker exec lanloom python -m lanloom.ai.runtime compose``) and writes it to ``compose.override.yaml``,
* the app (``lanloom/ai/manager.py``) knows where to find the service afterwards.

App <-> sidecar protocol (files on the shared data volume, like the update request):

    <data>/update/ai-request.json   written by the app   {"action": "install"|"remove", "nonce": <32 hex>, "requested": <unix time>}
    <data>/update/ai-status.json    written by sidecar   {"state", "message_key", "url", "nonce", "t", "log": [...]}

The request is a trigger, not an instruction: only ``action`` (one of ``ACTIONS``), ``nonce`` and ``requested`` are
read, and nothing from the file ever reaches a command, an image name or a container setting. Everything the sidecar
does is fixed here. The app never gets access to the container runtime.
"""
import os
import sys

IMAGE_REPO = "ollama/ollama"                     # official image of the local model server (amd64 + arm64)
IMAGE_TAG = "latest"
CONTAINER = "lanloom-ai"
VOLUME = "lanloom-ai"                            # models stay in this volume when the service is removed
MODELS_DIR = "/root/.ollama"
PORT = 11434
ROLE = "local-ai"                                # value of the org.lanloom.role label
ACTIONS = ("install", "remove")
STATES = ("idle", "pulling", "creating", "starting", "running", "removing", "removed", "failed")
TERMINAL = ("idle", "running", "removed", "failed")
REQUEST_FILE = "ai-request.json"
STATUS_FILE = "ai-status.json"
KEEP_ALIVE = "30m"                               # keep a loaded model in memory for 30 minutes (saves the reload)


def image_ref() -> str:
    """Image of the service. ``LANLOOM_AI_IMAGE`` (sidecar environment, never the request file) may pin a version."""
    ref = (os.environ.get("LANLOOM_AI_IMAGE") or "").strip()
    return ref or "%s:%s" % (IMAGE_REPO, IMAGE_TAG)


def service_url(host_network: bool) -> str:
    """Where the app reaches the service: on the host's loopback with host networking, else by container name."""
    return "http://127.0.0.1:%d" % PORT if host_network else "http://%s:%d" % (CONTAINER, PORT)


def container_env(host_network: bool) -> list:
    # With host networking the server listens on the loopback only: reachable for Lanloom on the same host, not for
    # the rest of the network (the model server has no password).
    bind = "127.0.0.1:%d" % PORT if host_network else "0.0.0.0:%d" % PORT
    return ["OLLAMA_HOST=%s" % bind, "OLLAMA_KEEP_ALIVE=%s" % KEEP_ALIVE]


def compose_snippet() -> str:
    """compose.override.yaml content that adds the service to an installer-made Lanloom (host networking)."""
    env = "\n".join("      - %s" % e for e in container_env(True))
    return (
        "# Optional local AI service for Lanloom (added by the installer with --with-ai). Safe to delete:\n"
        "# docker compose down the service, then remove this file. Models live in the volume %(vol)s.\n"
        "services:\n"
        "  ai:\n"
        "    image: %(img)s\n"
        "    container_name: %(name)s\n"
        "    restart: unless-stopped\n"
        "    network_mode: host\n"
        "    security_opt: [\"no-new-privileges:true\"]\n"
        "    environment:\n%(env)s\n"
        "    volumes:\n"
        "      - %(vol)s:%(dir)s\n"
        "    labels:\n"
        "      org.lanloom.role: %(role)s\n"
        "volumes:\n"
        "  %(vol)s:\n"
        "    name: %(vol)s\n"
    ) % {"img": image_ref(), "name": CONTAINER, "env": env, "vol": VOLUME, "dir": MODELS_DIR, "role": ROLE}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["compose"]:
        sys.stdout.write(compose_snippet())
        return 0
    sys.stderr.write("usage: python -m lanloom.ai.runtime compose\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
