"""Firewall editor: move devices between networks as a draft, preview the consequences in plain words, then hand a
signed plan to a separate executor.

* The web app never writes to a network device. It keeps drafts, computes previews and builds plans.
* The default target is a built-in simulation of a sample network. It writes nothing anywhere.
* Real changes only run in the optional executor service (``python -m lanloom.executor``). It verifies the passkey
  signature over the plan hash itself, applies its own gate, backs up, writes through a controller adapter, watches
  the result and rolls back on its own. Real writes stay off until the operator arms them explicitly.
"""
