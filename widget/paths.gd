extends RefCounted
## Where the widget finds things outside its own pack (WIRING.md §13, §15). The same XDG rules as
## src/strawberry_crab/paths.py: an unset, empty or relative variable falls back to the default.
## Nothing here points into a source checkout, so the exported binary and a source run agree.
##
##   preferences   $XDG_CONFIG_HOME/strawberry/widget.cfg      (the tray reads it back, §14)
##   config        $XDG_CONFIG_HOME/strawberry/config.toml     (the daemon's; "Settings file…")
##   voices        $XDG_DATA_HOME/strawberry/voices/            ("Voices folder…")
##   extracted     $XDG_CACHE_HOME/strawberry/widget/           (files the pack carries, copied out to run)

const APP := "strawberry"
const PREFS_FILE := "widget.cfg"
const LEGACY_PREFS := "user://widget.cfg"   # before PACKAGING.md step 4: Godot's own user dir

static func _xdg(variable: String, default: String) -> String:
	var value := OS.get_environment(variable)
	if value.begins_with("/"):
		return value
	return OS.get_environment("HOME").path_join(default)

static func config_home() -> String:
	return _xdg("XDG_CONFIG_HOME", ".config")

static func data_home() -> String:
	return _xdg("XDG_DATA_HOME", ".local/share")

static func cache_home() -> String:
	return _xdg("XDG_CACHE_HOME", ".cache")

static func config_dir() -> String:
	return config_home().path_join(APP)

static func config_file() -> String:
	return config_dir().path_join("config.toml")

static func prefs_file() -> String:
	return config_dir().path_join(PREFS_FILE)

static func voices_dir() -> String:
	return data_home().path_join(APP).path_join("voices")

## The `strawberry` CLI: the tray puts its absolute path in STRAWBERRY_CLI for the widget child;
## otherwise whatever `strawberry` is on PATH (OS.execute and create_process search PATH).
static func cli() -> String:
	var cli := OS.get_environment("STRAWBERRY_CLI")
	return cli if cli != "" else APP

## One-time move of the preferences out of Godot's user dir: copy user://widget.cfg to the XDG
## path when that does not exist yet. The old file is left where it is. True if it copied.
## (The arguments are for validate_prefs.gd, which must not touch the real files.)
static func migrate_prefs(target := "", source := LEGACY_PREFS) -> bool:
	if target == "":
		target = prefs_file()
	if FileAccess.file_exists(target) or not FileAccess.file_exists(source):
		return false
	DirAccess.make_dir_recursive_absolute(target.get_base_dir())
	var err := DirAccess.copy_absolute(ProjectSettings.globalize_path(source), target)
	if err != OK:
		push_warning("could not copy %s to %s: %s" % [source, target, error_string(err)])
		return false
	print("widget preferences copied to ", target, " (the old file stays at ", ProjectSettings.globalize_path(source), ")")
	return true

## A real file for a res:// path, for things run or read outside Godot (the idle helper runs on
## the system Python; the daemon checks an icon path exists). A source run has the file on disk
## already; an exported binary carries it in its pack, so it is copied out to the cache dir,
## rewritten only when its bytes changed. Imported images are saved back out as PNG.
static func on_disk(res_path: String) -> String:
	if not OS.has_feature("template"):
		return ProjectSettings.globalize_path(res_path)
	var dest := cache_home().path_join(APP).path_join("widget").path_join(res_path.get_file())
	DirAccess.make_dir_recursive_absolute(dest.get_base_dir())
	if res_path.get_extension().to_lower() in ["png", "jpg", "jpeg", "svg", "webp"]:
		if not FileAccess.file_exists(dest):
			var texture := load(res_path) as Texture2D
			if texture:
				texture.get_image().save_png(dest)
		return dest
	var bytes := FileAccess.get_file_as_bytes(res_path)
	if bytes.is_empty():
		push_warning("%s is not in the pack" % res_path)
		return dest
	if not FileAccess.file_exists(dest) or FileAccess.get_file_as_bytes(dest) != bytes:
		var file := FileAccess.open(dest, FileAccess.WRITE)
		if file:
			file.store_buffer(bytes)
			file.close()
	return dest

## The widget's version: application/config/version as scripts/build_widget.sh stamps it into
## the export; a source run has none and reports "dev", which the daemon always accepts (§1).
static func version() -> String:
	var stamped := str(ProjectSettings.get_setting("application/config/version", ""))
	return stamped if stamped != "" else "dev"
