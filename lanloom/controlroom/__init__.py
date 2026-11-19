"""The control room: one page for the blocks of the network that keep a home or a small office running (S9-S12).

``ControlRoom(core)`` (``room.py``) owns the parts and the one loop that feeds them:

* ``wan.py``       - internet watch with two axes (what the gateway says, what a probe measures), flap guard, history, reserve line
* ``cellular.py``  - generic mobile-router card (bearer/band table, signal quality thresholds at one place)
* ``ups.py``       - battery backup (NUT and UPS-MIB readings, state log)
* ``nas.py``       - storage box card (volumes, containers, virtual machines, backups, battery backup)
* ``backups.py``   - backup chains with "unknown instead of ok", progress instead of duration, learned throughput
* ``room.py``      - the blocks of the page and the "needs a look" list; ``report.py`` the nightly section

Everything here reads what the sources already deliver; the only network traffic of its own is the optional internet probe
(off until an administrator picks targets). Nothing here writes to a device.
"""
