extends PopupMenu
## Right-click menu on the crab (WIRING.md §13): mute, quiet hour, voice volume, skin,
## always-on-top, the settings file, and quit. Preferences persist in user://widget.cfg
## through the widget's save_settings(); daemon-side settings live in config.toml, which
## "Settings file…" opens in the desktop's text editor.

# Explicit ids for every item: items added without one get their index as id, and a
# submenu row would then collide with a real id and take its check mark.
enum { MUTE, QUIET_HOUR, ALWAYS_ON_TOP, SETTINGS_FILE, APPLY_SETTINGS, VOICES_FOLDER, RESET_POSITION, QUIT, VOLUME_MENU = 100, SKIN_MENU = 101 }
const VOLUMES := [0.25, 0.5, 0.75, 1.0]
const QUIET_SECONDS := 3600.0

var widget: Node3D
var volume_menu: PopupMenu
var skin_menu: PopupMenu
var opened := 0

func setup(owner: Node3D) -> void:
	widget = owner
	add_theme_font_size_override("font_size", 15)
	add_check_item("Mute her voice", MUTE)
	add_check_item("Quiet for an hour", QUIET_HOUR)
	volume_menu = PopupMenu.new()
	volume_menu.add_theme_font_size_override("font_size", 15)
	for i in VOLUMES.size():
		volume_menu.add_radio_check_item("%d%%" % int(VOLUMES[i] * 100), i)
	volume_menu.id_pressed.connect(func(id: int): widget.set_voice_volume(VOLUMES[id]))
	add_submenu_node_item("Voice volume", volume_menu, VOLUME_MENU)
	skin_menu = PopupMenu.new()
	skin_menu.add_theme_font_size_override("font_size", 15)
	for i in widget.SkinPalettes.ORDER.size():
		skin_menu.add_radio_check_item(widget.SkinPalettes.display_name(widget.SkinPalettes.ORDER[i]), i)
	skin_menu.id_pressed.connect(func(id: int): widget.set_skin(widget.SkinPalettes.ORDER[id]))
	add_submenu_node_item("Skin", skin_menu, SKIN_MENU)
	add_check_item("Always on top", ALWAYS_ON_TOP)
	add_separator()
	add_item("Settings file…", SETTINGS_FILE)
	add_item("Apply settings (restart daemon)", APPLY_SETTINGS)
	add_item("Voices folder…", VOICES_FOLDER)
	add_separator()
	add_item("Reset position", RESET_POSITION)
	add_item("Quit", QUIT)
	id_pressed.connect(_on_pressed)
	about_to_popup.connect(_refresh)

func open_at(at: Vector2) -> void:
	opened += 1
	popup(Rect2i(Vector2i(at), Vector2i.ZERO))

## Reflect the widget's current state in the check marks each time the menu opens.
func _refresh() -> void:
	set_item_checked(get_item_index(MUTE), widget.muted)
	var quiet_left: float = widget.quiet_until - Time.get_unix_time_from_system()
	var quiet_index := get_item_index(QUIET_HOUR)
	set_item_checked(quiet_index, quiet_left > 0.0)
	set_item_text(quiet_index, "Quiet for an hour" if quiet_left <= 0.0 else "Quiet (%d min left)" % ceili(quiet_left / 60.0))
	set_item_checked(get_item_index(ALWAYS_ON_TOP), widget.always_on_top)
	for i in VOLUMES.size():
		volume_menu.set_item_checked(i, is_equal_approx(widget.voice_volume, VOLUMES[i]))
	for i in widget.SkinPalettes.ORDER.size():
		skin_menu.set_item_checked(i, widget.SkinPalettes.ORDER[i] == widget.skin_id)

func _on_pressed(id: int) -> void:
	match id:
		MUTE:
			widget.set_muted(not widget.muted)
		QUIET_HOUR:
			var quiet_left: float = widget.quiet_until - Time.get_unix_time_from_system()
			widget.set_quiet_until(0.0 if quiet_left > 0.0 else Time.get_unix_time_from_system() + QUIET_SECONDS)
		ALWAYS_ON_TOP:
			widget.set_always_on_top(not widget.always_on_top)
		SETTINGS_FILE:
			open_settings_file()
		APPLY_SETTINGS:
			OS.create_process(repo_root().path_join("bin/strawberry"), ["restart"])
		VOICES_FOLDER:
			var dir := data_home().path_join("strawberry/voices")
			DirAccess.make_dir_recursive_absolute(dir)
			OS.shell_open("file://" + dir)
		RESET_POSITION:
			widget.reset_position()
		QUIT:
			widget.get_tree().quit()

func open_settings_file() -> void:
	var path := config_home().path_join("strawberry/config.toml")
	if not FileAccess.file_exists(path):
		# Same as `bin/strawberry config`: write the commented template first.
		OS.execute(repo_root().path_join("strawberryd/.venv/bin/strawberryd"), ["--init-config"])
	OS.shell_open("file://" + path)

static func repo_root() -> String:
	return ProjectSettings.globalize_path("res://").rstrip("/").get_base_dir()

static func config_home() -> String:
	var xdg := OS.get_environment("XDG_CONFIG_HOME")
	return xdg if xdg != "" else OS.get_environment("HOME").path_join(".config")

static func data_home() -> String:
	var xdg := OS.get_environment("XDG_DATA_HOME")
	return xdg if xdg != "" else OS.get_environment("HOME").path_join(".local/share")
