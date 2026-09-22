"""Event producers that run beside the daemon, one process each (WIRING.md §4, §4b).

    python -m strawberryd.doorways.notify_watch     desktop notifications (D-Bus monitor)
    python -m strawberryd.doorways.mpris_watch      media players (MPRIS)

They speak D-Bus through jeepney (pure Python, so they run on the daemon's venv) and reach
the daemon over plain HTTP: a doorway that trips must never take the websocket down with it.
"""
