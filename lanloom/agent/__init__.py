"""Lanloom host agent (optional): metadata of another host for one Lanloom.

Runs from the same signed image as Lanloom (``python -m lanloom.agent run``) and uses only the Python standard
library. It reports, every few seconds and only to the Lanloom it was paired with: bytes per TCP conversation,
listening services, load, memory and disks, failed services and timer results, the containers of the host's
container runtime and - when available - the peers of an overlay VPN. Never contents, environment variables or
command lines (``scrub``). It opens no port and takes no commands. See docs/HOST-AGENT.md.
"""
PROTO = 1
REPORT_S = 10
