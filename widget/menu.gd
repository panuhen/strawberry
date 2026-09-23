extends PopupMenu
## Right-click menu on the crab (WIRING.md §13): type to her, mute, quiet hour, voice volume, skin,
## always-on-top, the settings file, and quit. Preferences persist in
## $XDG_CONFIG_HOME/strawberry/widget.cfg through the widget's save_settings(); daemon-side
## settings live in config.toml, which "Settings file…" opens in the desktop's text editor.
## Both that and "Apply settings" go through the `strawberry` CLI (paths.gd: STRAWBERRY_CLI
## from the tray, else PATH), never into a source checkout.

const Paths = preload("res://paths.gd")

# Explicit ids for every item: items added without one get their index as id, and a
# submenu row would then collide with a real id and take its check mark.
enum { MUTE, QUIET_HOUR, ALWAYS_ON_TOP, SETTINGS_FILE, APPLY_SETTINGS, VOICES_FOLDER, RESET_POSITION, QUIT, TOP_HAT = 20, RESTART_WIDGET = 21, VOLUME_MENU = 100, SKIN_MENU = 101, SLEEP_MENU = 102, SLEEP_NOW = 22, TYPE_BOX = 23 }
const SLEEP_MINUTES := [5.0, 1.0, 10.0, 30.0, 0.0]
const VOLUMES := [0.25, 0.5, 0.75, 1.0]
const QUIET_SECONDS := 3600.0

var widget: Node3D
var volume_menu: PopupMenu
var skin_menu: PopupMenu
var sleep_menu: PopupMenu
var opened := 0

func setup(owner: Node3D) -> void:
	widget = owner
	add_theme_font_size_override("font_size", 15)
	add_item("Chat with Strawberry…", TYPE_BOX)
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
	sleep_menu = PopupMenu.new()
	sleep_menu.add_theme_font_size_override("font_size", 15)
	for i in SLEEP_MINUTES.size():
		var minutes: float = SLEEP_MINUTES[i]
		sleep_menu.add_radio_check_item("Never" if minutes == 0 else "%d minutes" % int(minutes), i)
	sleep_menu.id_pressed.connect(func(id: int): widget.sleeper.set_delay(SLEEP_MINUTES[id]))
	add_submenu_node_item("Sleep after inactivity", sleep_menu, SLEEP_MENU)
	add_item("Sleep now", SLEEP_NOW)
	add_check_item("Top hat", TOP_HAT)
	add_check_item("Always on top", ALWAYS_ON_TOP)
	add_separator()
	add_item("Settings file…", SETTINGS_FILE)
	add_item("Apply settings (restart daemon)", APPLY_SETTINGS)
	add_item("Voices folder…", VOICES_FOLDER)
	add_separator()
	add_item("Reset position", RESET_POSITION)
	add_item("Restart widget", RESTART_WIDGET)
	add_item("Quit", QUIT)
	id_pressed.connect(_on_pressed)
	about_to_popup.connect(_refresh)

func open_at(at: Vector2) -> void:
	opened += 1
	popup(Rect2i(Vector2i(at), Vector2i.ZERO))

## Reflect the widget's current state in the check marks each time the menu opens.
func _refresh() -> void:
	set_item_disabled(get_item_index(SLEEP_NOW), widget.state != "idle" or widget.rest_state != "idle" or widget.one_shot != "" or widget.speech.playing or widget.bubble.speaking or widget.reactions.recipe != "")
	for i in SLEEP_MINUTES.size():
		sleep_menu.set_item_checked(i, is_equal_approx(widget.sleep_after_minutes, SLEEP_MINUTES[i]))
	set_item_checked(get_item_index(TOP_HAT), widget.top_hat_enabled)
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
		TYPE_BOX:
			hide()
			widget.call_deferred("open_type_box")
		SLEEP_NOW:
			hide()
			widget.sleeper.call_deferred("begin_sleep")
		TOP_HAT:
			widget.set_top_hat(not widget.top_hat_enabled)
		RESTART_WIDGET:
			widget.restart_widget()
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
			apply_settings()
		VOICES_FOLDER:
			var dir := Paths.voices_dir()
			DirAccess.make_dir_recursive_absolute(dir)
			open_path(dir)
		RESET_POSITION:
			widget.reset_position()
		QUIT:
			widget.get_tree().quit()

## The daemon's config.toml; a missing one is first written from the commented template by
## `strawberry config --init` (the CLI owns the template, WIRING.md §15).
func open_settings_file() -> void:
	var path := Paths.config_file()
	if not FileAccess.file_exists(path):
		var output: Array = []
		var code := OS.execute(Paths.cli(), ["config", "--init"], output, true)
		if code != 0 or not FileAccess.file_exists(path):
			push_warning("%s config --init failed (%d): %s" % [Paths.cli(), code, "".join(output)])
			widget.bubble.speak("I couldn't find the strawberry command to write my settings file.", "alert")
			return
	open_path(path)

## The desktop's own program for a file or folder: xdg-open's file:// URL on Linux. On Windows
## ShellExecute takes a plain path; a .toml has no program set for it on a fresh install, so
## what the shell cannot open goes to Notepad.
func open_path(path: String) -> void:
	if not Paths.windows():
		OS.shell_open("file://" + path)
		return
	var native := path.replace("/", "\\")
	if OS.shell_open(native) != OK and not DirAccess.dir_exists_absolute(path):
		OS.create_process("notepad.exe", [native])

## "Apply settings": `strawberry restart`, which restarts the tray's unit (and with it the daemon)
## when she runs under it, or the daemon on its own. Never a script inside a checkout.
func apply_settings() -> void:
	if OS.create_process(Paths.cli(), ["restart"]) == -1:
		widget.bubble.speak("I couldn't find the strawberry command to restart my daemon.", "alert")
