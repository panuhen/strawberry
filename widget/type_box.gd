extends PanelContainer
## Glass text field under the crab (WIRING.md §13): type to her instead of speaking.
##
## Hidden until "Type to her…" in the menu or the T key opens it. Enter sends the line to
## strawberryd as a heard sentence, so it takes the same path as speech and `strawberry talk`
## (gate, reflexes, Qwen) and she answers on the desktop. Escape closes it. The field greys
## out while she is listening or thinking, and when the daemon is not connected.
##
## The "glass" is a tint with a hairline rim: the desktop behind the window is not in
## Godot's viewport, so there is nothing to blur; this is what every transparent-window
## glass effect is.

signal submitted(text: String)

const HEIGHT := 42.0
const MARGIN := 14.0
const GLASS := Color(0.13, 0.07, 0.09, 0.40)
const RIM := Color(1.0, 0.96, 0.92, 0.55)
const INK := Color("fff7ec")
const BUSY_STATES := ["listening", "thinking"]

var widget: Node3D
var field: LineEdit
var opens := 0
var sent := 0

func setup(owner: Node3D) -> void:
	widget = owner
	name = "TypeBox"
	visible = false
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var h := float(ProjectSettings.get_setting("display/window/size/viewport_height"))
	position = Vector2(MARGIN, h - HEIGHT - MARGIN)
	custom_minimum_size = Vector2(w - 2.0 * MARGIN, HEIGHT)
	size = custom_minimum_size

	var glass := StyleBoxFlat.new()
	glass.bg_color = GLASS
	glass.set_corner_radius_all(int(HEIGHT / 2.0))
	glass.set_border_width_all(1)
	glass.border_color = RIM
	glass.anti_aliasing = true
	glass.shadow_color = Color(0, 0, 0, 0.22)
	glass.shadow_size = 6
	glass.content_margin_left = 16.0
	glass.content_margin_right = 16.0
	glass.content_margin_top = 4.0
	glass.content_margin_bottom = 4.0
	add_theme_stylebox_override("panel", glass)

	field = LineEdit.new()
	field.flat = true
	field.placeholder_text = "Type to her…"
	field.max_length = 400
	field.context_menu_enabled = false
	field.add_theme_font_size_override("font_size", 16)
	field.add_theme_color_override("font_color", INK)
	field.add_theme_color_override("font_placeholder_color", Color(INK, 0.5))
	field.add_theme_color_override("font_uneditable_color", Color(INK, 0.6))
	field.add_theme_color_override("selection_color", Color(1, 1, 1, 0.25))
	field.text_submitted.connect(submit)
	field.gui_input.connect(_on_field_input)
	add_child(field)
	apply_skin()

## Caret and selection in the skin's claw colour, so the box belongs to her.
func apply_skin() -> void:
	var skin: Dictionary = widget.SkinPalettes.SKINS.get(widget.skin_id, widget.SkinPalettes.SKINS.strawberry)
	field.add_theme_color_override("caret_color", Color.html(skin.claw).lightened(0.25))

func open() -> void:
	opens += 1
	visible = true
	apply_skin()
	_refresh()
	if not widget.is_headless():
		get_window().grab_focus()
	field.grab_focus()
	field.caret_column = field.text.length()
	widget.call_deferred("update_passthrough")

func close() -> void:
	if not visible:
		return
	visible = false
	field.release_focus()
	widget.call_deferred("update_passthrough")

func toggle() -> void:
	if visible:
		close()
	else:
		open()

## Send what is in the field (or `text`); an empty line does nothing.
func submit(text: String = "") -> void:
	var line := (text if text != "" else field.text).strip_edges()
	if line == "" or not field.editable:
		return
	sent += 1
	field.clear()
	submitted.emit(line)

func _on_field_input(event: InputEvent) -> void:
	if event is InputEventKey and event.pressed and event.keycode == KEY_ESCAPE:
		close()
		accept_event()

func _process(_delta: float) -> void:
	if visible:
		_refresh()

## Grey the field while she is busy or unreachable; the placeholder says which.
func _refresh() -> void:
	var connected: bool = widget.ws != null and widget.ws.is_open()
	var busy: bool = widget.state in BUSY_STATES
	field.editable = connected and not busy
	if not connected:
		field.placeholder_text = "Not connected to strawberryd"
	elif busy:
		field.placeholder_text = "She's on it…"
	else:
		field.placeholder_text = "Type to her…"
