extends PanelContainer
## Text field under the crab (WIRING.md §13): type to her instead of speaking.
##
## Hidden until "Chat with Strawberry…" in the menu or the T key opens it. Enter sends the line to
## strawberryd as a heard sentence, so it takes the same path as speech and `strawberry talk`
## (gate, reflexes, Qwen) and she answers on the desktop. Escape closes it. The field greys
## out while she is listening or thinking, and when the daemon is not connected.
##
## A plain field in the bubble's cream and ink, nothing behind it: a translucent "glass"
## panel was tried first and looked odd over the desktop (Panu, 2026-09-22).

signal submitted(text: String)

const HEIGHT := 42.0
const MARGIN := 14.0
const CREAM := Color("fff7ec")
const INK := Color("201318")
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

	add_theme_stylebox_override("panel", StyleBoxEmpty.new())

	field = LineEdit.new()
	field.placeholder_text = "Chat with Strawberry…"
	field.max_length = 400
	field.context_menu_enabled = false
	field.add_theme_font_size_override("font_size", 16)
	field.add_theme_color_override("font_color", INK)
	field.add_theme_color_override("font_placeholder_color", Color(INK, 0.45))
	field.add_theme_color_override("font_uneditable_color", Color(INK, 0.5))
	field.add_theme_color_override("selection_color", Color(INK, 0.18))
	for state_name in ["normal", "focus", "read_only"]:
		field.add_theme_stylebox_override(state_name, field_style(state_name))
	field.text_submitted.connect(submit)
	field.gui_input.connect(_on_field_input)
	add_child(field)
	apply_skin()

## The field itself: cream, ink border, rounded. Focus thickens the border in the skin's claw colour.
func field_style(state_name: String) -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = CREAM if state_name != "read_only" else CREAM.darkened(0.06)
	style.set_border_width_all(2 if state_name == "focus" else 1)
	style.border_color = Color(INK, 0.35)
	style.anti_aliasing = true
	style.content_margin_left = 12.0
	style.content_margin_right = 12.0
	style.content_margin_top = 6.0
	style.content_margin_bottom = 6.0
	return style

## Caret and focus ring in the skin's claw colour, so the field belongs to her.
func apply_skin() -> void:
	var skin: Dictionary = widget.SkinPalettes.SKINS.get(widget.skin_id, widget.SkinPalettes.SKINS.strawberry)
	var claw := Color.html(skin.claw)
	field.add_theme_color_override("caret_color", claw)
	var focus := field_style("focus")
	focus.border_color = claw
	field.add_theme_stylebox_override("focus", focus)

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
		field.placeholder_text = "Chat with Strawberry…"
