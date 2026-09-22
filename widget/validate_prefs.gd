extends SceneTree
## Where the preferences live and the one-time move out of Godot's user dir (WIRING.md §13).
## Only throwaway files: XDG_CONFIG_HOME is pointed at a scratch dir for this process, and the
## "old" file is a scratch copy, never the real user://widget.cfg.
##
## godot --headless --path widget --script res://validate_prefs.gd
## strawberry-widget --headless -- --acceptance=res://validate_prefs.gd

const Paths = preload("res://paths.gd")

var failures: Array[String] = []

func _initialize() -> void:
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

func run() -> void:
	var scratch := OS.get_user_data_dir().path_join("prefs_test_%d" % OS.get_process_id())
	DirAccess.make_dir_recursive_absolute(scratch)
	var saved_config := OS.get_environment("XDG_CONFIG_HOME")
	OS.set_environment("XDG_CONFIG_HOME", scratch.path_join("config"))

	# The XDG path, and the rule that a relative XDG variable falls back to the default.
	check(Paths.prefs_file() == scratch.path_join("config/strawberry/widget.cfg"), "prefs should follow XDG_CONFIG_HOME, got " + Paths.prefs_file())
	OS.set_environment("XDG_CONFIG_HOME", "relative/dir")
	check(Paths.prefs_file() == OS.get_environment("HOME").path_join(".config/strawberry/widget.cfg"), "a relative XDG_CONFIG_HOME should fall back to ~/.config")
	OS.set_environment("XDG_CONFIG_HOME", scratch.path_join("config"))

	# The widget itself uses that path when it has a display; headless it keeps its own file.
	var widget: Node3D = (load("res://widget.tscn") as PackedScene).instantiate()
	check(widget.settings_path() == widget.HEADLESS_SETTINGS_PATH, "headless runs must keep their own settings file")
	widget.free()

	# The move: copied when the new file is missing, the old one left alone, never overwritten.
	var old := scratch.path_join("old_widget.cfg")
	var target := Paths.prefs_file()
	FileAccess.open(old, FileAccess.WRITE).store_string("[appearance]\n\nskin=\"mint\"\n")
	check(Paths.migrate_prefs(target, old), "the old prefs should be copied when there are no new ones")
	check(FileAccess.get_file_as_string(target).contains("mint"), "the copy should carry the old preferences")
	check(FileAccess.file_exists(old), "the old file must stay where it was")
	FileAccess.open(target, FileAccess.WRITE).store_string("[appearance]\n\nskin=\"midnight\"\n")
	check(not Paths.migrate_prefs(target, old), "a second run must not copy again")
	check(FileAccess.get_file_as_string(target).contains("midnight"), "existing new prefs must never be overwritten")
	check(not Paths.migrate_prefs(scratch.path_join("config/other.cfg"), scratch.path_join("missing.cfg")), "nothing to move is not an error")

	# "Settings file…" writes a missing config.toml through the CLI (scripts/check_phase1.sh sets
	# STRAWBERRY_CLI to the checkout's; without it this step is skipped, not failed).
	var config_file := Paths.config_file()
	if OS.get_environment("STRAWBERRY_CLI") != "":
		var output: Array = []
		var code := OS.execute(Paths.cli(), ["config", "--init"], output, true)
		check(code == 0 and FileAccess.file_exists(config_file), "%s config --init should write %s (exit %d: %s)" % [Paths.cli(), config_file, code, "".join(output)])
		check(FileAccess.get_file_as_string(config_file).begins_with("#"), "the config template should be the commented one")
	else:
		print("prefs checks: STRAWBERRY_CLI unset, skipping the config --init step")

	for path in [target, old, config_file]:
		DirAccess.remove_absolute(path)
	for dir in [scratch.path_join("config/strawberry"), scratch.path_join("config"), scratch]:
		DirAccess.remove_absolute(dir)
	OS.set_environment("XDG_CONFIG_HOME", saved_config)
	print("prefs checks: ", "PASSED" if failures.is_empty() else "FAILED %s" % str(failures))
	quit(0 if failures.is_empty() else 1)
