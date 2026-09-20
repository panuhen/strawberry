from strawberryd.contract import Performance
from strawberryd.events import Event
from strawberryd.reactions import Choice, choose, decorate, is_message


def note(app="", title="", body="", urgency="normal", category="", icon=""):
    return Event(source="notification", app=app, title=title, body=body, urgency=urgency, category=category, icon=icon)


def test_a_message_waves_and_hops():
    assert choose(note(app="WhatsApp", title="James"), "happy") == Choice(reaction="wave", hop=True)
    assert choose(note(app="Thunderbird", title="Invoice"), "neutral") == Choice(reaction="wave", hop=True)
    assert choose(note(app="Some App", category="im.received"), "neutral") == Choice(reaction="wave", hop=True)


def test_is_message_is_case_insensitive_and_substring():
    assert is_message(note(app="org.telegram.desktop"))
    assert is_message(note(app="SLACK"))
    assert not is_message(note(app="Power"))


def test_critical_or_angry_shivers_with_alert_snap():
    assert choose(note(app="Power", title="Battery low", urgency="critical"), "alert") == Choice(anim="alert_snap", reaction="shiver")
    assert choose(note(app="GitHub", title="CI failed"), "angry") == Choice(anim="alert_snap", reaction="shiver")
    # critical beats message: a critical WhatsApp is still a shiver
    assert choose(note(app="WhatsApp", urgency="critical"), "alert").reaction == "shiver"


def test_a_burst_double_hops():
    assert choose(note(app="Slack", title="7 notifications from Slack"), "alert") == Choice(reaction="double_hop")
    assert choose(note(app="several apps", title="3 notifications"), "neutral") == Choice(reaction="double_hop")


def test_other_notifications_by_emotion():
    assert choose(note(app="Calendar", title="Standup"), "alert") == Choice(reaction="peek")
    assert choose(note(app="GitHub", title="CI passed"), "happy") == Choice(anim="notify_perk")
    assert choose(note(app="Updater", title="Updates"), "neutral") == Choice(reaction="nod")


def test_media_and_voice_nod_without_breaking_the_dance():
    assert choose(Event(source="media", app="Spotify", title="X — Y"), "happy") == Choice(reaction="nod")
    assert choose(Event(source="voice", body="skip"), "neutral") == Choice(reaction="nod")


def test_git_keeps_the_hop_for_commits_and_nods_for_pushes():
    assert choose(Event(source="git", app="post-commit", title="r", body="s"), "happy") == Choice(anim="notify_perk")
    assert choose(Event(source="git", app="post-commit", title="r", body="s"), "angry") == Choice(anim="alert_snap")
    assert choose(Event(source="git", app="pre-push", title="r", body="s"), "happy") == Choice(reaction="nod")


def test_decorate_attaches_reaction_and_existing_icon_only(tmp_path):
    icon = tmp_path / "whatsapp.png"
    icon.write_bytes(b"\x89PNG")
    said = Performance(state="talking", text="James wrote!", emotion="happy", anim="notify_perk")
    out = decorate(note(app="WhatsApp", title="James", icon=str(icon)), said)
    assert out.text == "James wrote!" and out.emotion == "happy"
    assert out.anim is None and out.reaction == "wave" and out.hop is True
    assert out.icon == str(icon)
    gone = decorate(note(app="WhatsApp", icon=str(tmp_path / "missing.png")), said)
    assert gone.icon is None
    assert gone.to_dict() == {"state": "talking", "emotion": "happy", "text": "James wrote!", "reaction": "wave", "hop": True}
