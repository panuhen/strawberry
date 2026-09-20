import pytest

from strawberryd import persona
from strawberryd.config import ConfigError, default_toml, load


def test_defaults_when_no_file(tmp_path):
    config = load(tmp_path / "missing.toml", env={})
    assert config.path is None
    assert config.daemon.port == 8770
    assert config.brain.reaction_model == "gemma3:1b"
    assert config.brain.examples == persona.EXAMPLES
    assert config.media.only == []


def test_file_overrides_only_what_it_names(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[brain]\nreaction_model = "qwen3.5:0.8b"\ntimeout_s = 2\n\n[media]\nignore = ["firefox"]\n')
    config = load(path, env={})
    assert config.path == path
    assert config.brain.reaction_model == "qwen3.5:0.8b"
    assert config.brain.timeout_s == 2.0  # TOML int accepted for a float
    assert config.brain.temperature == 0.8  # untouched default
    assert config.media.ignore == ["firefox"]


def test_examples_replace_the_defaults(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[brain.examples]]\nevent = "source: git"\nline = "Ooh."\nemotion = "happy"\n')
    config = load(path, env={})
    assert config.brain.examples == [{"event": "source: git", "line": "Ooh.", "emotion": "happy"}]


def test_wrong_type_is_fatal_and_named(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[daemon]\nport = "eight"\n')
    with pytest.raises(ConfigError, match="daemon.port must be int"):
        load(path, env={})


def test_bad_example_emotion_is_fatal(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[[brain.examples]]\nevent = "x"\nline = "y"\nemotion = "smug"\n')
    with pytest.raises(ConfigError, match="smug"):
        load(path, env={})


def test_unknown_keys_warn_but_load(tmp_path, caplog):
    path = tmp_path / "config.toml"
    path.write_text('[brain]\nmood = "sunny"\n\n[widget]\nskin = "mint"\n')
    config = load(path, env={})
    assert config.brain.reaction_model == "gemma3:1b"
    assert "unknown key brain.mood" in caplog.text
    assert "unknown section [widget]" in caplog.text


def test_env_overrides_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[daemon]\nport = 9000\n")
    config = load(path, env={"STRAWBERRYD_PORT": "9100"})
    assert config.daemon.port == 9100


def test_default_template_round_trips(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(default_toml())
    config = load(path, env={})
    assert config.brain.reaction_model == "gemma3:1b"
    assert config.brain.keep_alive == -1


def test_keep_alive_accepts_seconds_or_duration_string(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[brain]\nkeep_alive = "10m"\n')
    assert load(path, env={}).brain.keep_alive == "10m"
    path.write_text("[brain]\nkeep_alive = 600\n")
    assert load(path, env={}).brain.keep_alive == 600
    path.write_text("[brain]\nkeep_alive = true\n")
    with pytest.raises(ConfigError, match="keep_alive"):
        load(path, env={})
