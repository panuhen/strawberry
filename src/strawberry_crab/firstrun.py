"""The first-run privacy note (PACKAGING.md step 6): said once, then never again.

On start, while the marker file is missing, the daemon logs what she reads from notifications
and where to change it. When the first body that shows text says hello (a v1 widget, or a v2
body with `speech.bubble`; server.py) she says a short version in her bubble to it, and once it
went out the marker is written, so the note appears once per user, not once per start.
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
    if config.learning.log_outcomes:
        learning = (f" The sentences you say or type to her are kept with how she routed them, for the "
                    f"learning loop, in {paths.outcomes_file()} ([learning] log_outcomes; private ones never; "
                    f"`strawberry outcomes --clear` deletes them); what she learns from them is kept in "
                    f"{paths.data_dir() / 'gate' / 'learned.json'} (`strawberry learning forget` deletes it).")
    else:
        learning = " The learning loop keeps none of your sentences ([learning] log_outcomes is off)."
    history = config.history
    if history.enabled and history.sentences:
        kept = (f" Your sentences and her replies are kept for {history.keep_days} days in {paths.history_db()}, "
                f"then only a short digest a day for {history.digest_months} months ([history]; the Brain UI's "
                f"History view forgets a row, a day or everything).")
    elif history.enabled:
        kept = (f" Her history in {paths.history_db()} keeps what happened, never what you said "
                f"([history] sentences = false).")
    else:
        kept = " She keeps no history ([history] enabled = false)."
    return (f"privacy: {reading}. Modes: off, react, glance; codes, sign-ins and bank alerts are "
            f"always dropped. Change [notifications] body in {where}. Everything runs on this "
            f"machine; no message text is logged.{learning}{kept}")


def bubble_text(config: Config) -> str:
    """The short line for her bubble: what she reads, what she keeps, and where to change it."""
    keeps = (f" I remember our chats for {config.history.keep_days} days; the Brain UI's History can forget them."
             if config.history.enabled and config.history.sentences else "")
    if _reads_bodies(config):
        return "I read your notification messages, as the settings file says. Nothing leaves this computer." + keeps
    return ("I only see who sent a notification, never the message. "
            "Right-click me, Settings file, to change that." + keeps)
