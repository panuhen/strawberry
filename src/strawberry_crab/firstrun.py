"""The first-run privacy note (PACKAGING.md step 6): said once, then never again.

On start, while the marker file is missing, the daemon logs what she reads from notifications
and where to change it. When the first widget says hello she says a short version in her
bubble and the marker is written, so the note appears once per user, not once per start.
Deleting the marker (`paths.privacy_notice_marker()`) brings it back.
"""

from __future__ import annotations

from . import paths
from .config import Config


def pending() -> bool:
    return not paths.privacy_notice_marker().exists()


def mark_shown() -> None:
    marker = paths.privacy_notice_marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("the first-run privacy note was shown; delete this file to see it again\n")


def _reads_bodies(config: Config) -> bool:
    notifications = config.notifications
    return notifications.body != "off" or any(mode != "off" for mode in notifications.body_apps.values())


def log_text(config: Config) -> str:
    """The line for the log (journal or the daemon's logfile)."""
    where = config.path or paths.config_file()
    if _reads_bodies(config):
        reading = f'notification bodies are read (body = "{config.notifications.body}", body_apps set per app)'
    else:
        reading = "notification bodies are off by default: she reads only the app and the sender"
    return (f"privacy: {reading}. Modes: off, react, glance; codes, sign-ins and bank alerts are "
            f"always dropped. Change [notifications] body in {where}. Everything runs on this "
            f"machine; no message text is logged.")


def bubble_text(config: Config) -> str:
    """The short line for her bubble: what she reads, and where to change it."""
    if _reads_bodies(config):
        return "I read your notification messages, as the settings file says. Nothing leaves this computer."
    return ("I only see who sent a notification, never the message. "
            "Right-click me, Settings file, to change that.")
