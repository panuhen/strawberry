extends PanelContainer
## The step chip under her bubble (WIRING.md §13, §18): while a run is busy it says what she is
## doing in plain words ("thinking…", "searching the web…", "Spotify: next"), and its ✕ stops the
## run (`run.cancel`, protocol v2). It goes when the run ends.
##
## Fed by the daemon's run events (PROTOCOL.md §11). Only the daemon's own names reach it: the
## tool's label (the server and a short tool name), never an argument or a result. A run that ends
## within SHOW_AFTER (a reflex is ~0.25 s) never shows it, so a skip does not flash a chip.
##
## The same smoked glass as the type box: a dark tint with a hairline rim, cream text, square
## corners. The ✕ is a 40 px square, big enough for a fingertip on a touch screen.

const GLASS := Color(0.13, 0.07, 0.09, 0.40)
const RIM := Color(1.0, 0.96, 0.92, 0.55)
const INK := Color("fff7ec")
const HEIGHT := 40.0
const GAP := 8.0                 # below the bubble's anchor
const SHOW_AFTER := 0.35         # seconds of a run before the chip shows
const STALE_S := 60.0            # no event for this long: the run is over (PROTOCOL §11)
const THINKING := "thinking…"
const WAITING := "waiting for you…"   # an approval card is up (approval_card.gd)

var widget: Node3D
var label: Label
var stop_button: Button
var run_id := ""
var step := ""
var thinker := false             # the run reached the thinker: between tools it is "thinking…"
var started_at := 0.0
var last_event_at := 0.0
var can_cancel := false          # the daemon's welcome said this body may send run.cancel
var stopping := false
var runs_seen := 0               # runs whose events reached it (validate_widget.gd)
var runs_ended := 0
var shown_count := 0
var cancels_sent := 0
var refused := ""                # the run id the daemon last refused to cancel (input.refused)

func setup(owner: Node3D) -> void:
	widget = owner
	name = "StepChip"
	visible = false
	mouse_filter = Control.MOUSE_FILTER_STOP
	add_theme_stylebox_override("panel", glass())
	var row := HBoxContainer.new()
	row.add_theme_constant_override("separation", 0)
	add_child(row)
	label = Label.new()
	label.add_theme_font_size_override("font_size", 15)
	label.add_theme_color_override("font_color", INK)
	label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	label.custom_minimum_size = Vector2(0, HEIGHT)
	label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(label)
	stop_button = Button.new()
	stop_button.text = "✕"
	stop_button.tooltip_text = "Stop"
	stop_button.focus_mode = Control.FOCUS_NONE
	stop_button.custom_minimum_size = Vector2(HEIGHT, HEIGHT)
	stop_button.add_theme_font_size_override("font_size", 16)
	for state_name in ["font_color", "font_hover_color", "font_pressed_color", "font_focus_color"]:
		stop_button.add_theme_color_override(state_name, INK)
	stop_button.add_theme_color_override("font_disabled_color", Color(INK, 0.4))
	stop_button.add_theme_stylebox_override("normal", button_style(0.0))
	stop_button.add_theme_stylebox_override("hover", button_style(0.12))
	stop_button.add_theme_stylebox_override("pressed", button_style(0.22))
	stop_button.add_theme_stylebox_override("disabled", button_style(0.0))
	stop_button.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
	stop_button.pressed.connect(stop)
	row.add_child(stop_button)

## The glass itself: dark tint, hairline rim, square corners; the text inset from the rim.
func glass() -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = GLASS
	style.set_border_width_all(1)
	style.border_color = RIM
	style.content_margin_left = 12.0
	style.content_margin_right = 0.0
	style.content_margin_top = 0.0
	style.content_margin_bottom = 0.0
	return style

## The ✕: no box of its own, a hairline to its left and a brighter wash under the pointer.
func button_style(wash: float) -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = Color(1.0, 0.96, 0.92, wash)
	style.border_width_left = 1
	style.border_color = Color(RIM, 0.35)
	return style

## The welcome's `accepted`: whether the ✕ may be offered at all.
func welcomed(accepted: Dictionary) -> void:
	can_cancel = bool(accepted.get("cancel", false))

## One run event from the daemon.
func on_phase(data: Dictionary) -> void:
	var kind := str(data.get("type", ""))
	var id := str(data.get("run_id", ""))
	if id == "":
		return
	if id != run_id:
		if kind.begins_with("run."):
			return     # the end of a run this chip is not showing (a notification's, a stale one)
		begin(id)
	last_event_at = now()
	match kind:
		"routing":
			pass
		"thinking":
			thinker = true
			step = THINKING
		"tool.started":
			var named := str(data.get("label", data.get("tool", "")))
			step = named if named != "" else THINKING
		"tool.completed":
			if thinker:
				step = THINKING
		"speaking":
			pass
		"approval.request":
			step = WAITING
		"approval.resolved":
			step = THINKING
		"run.completed", "run.failed", "run.cancelled":
			end()
			return
	refresh()

func begin(id: String) -> void:
	run_id = id
	step = ""
	thinker = false
	stopping = false
	started_at = now()
	runs_seen += 1

func end() -> void:
	runs_ended += 1
	run_id = ""
	step = ""
	stopping = false
	set_shown(false)

## The ✕: ask the daemon to stop this run. It answers with the run's end (or input.refused).
func stop() -> void:
	if run_id == "" or stopping or widget.ws == null or not widget.ws.is_open():
		return
	stopping = true
	cancels_sent += 1
	widget.ws.send({"type": "run.cancel", "run_id": run_id})
	refresh()

## The daemon would not stop it (not the run going on): the chip waits for the run's own end.
func on_refused(data: Dictionary) -> void:
	refused = str(data.get("ref", ""))
	if refused == run_id:
		stopping = false
		refresh()

func now() -> float:
	return Time.get_ticks_msec() / 1000.0

func _process(_delta: float) -> void:
	if run_id == "":
		return
	if now() - last_event_at > STALE_S:
		end()
		return
	if not visible and step != "" and now() - started_at >= SHOW_AFTER:
		refresh()

func refresh() -> void:
	label.text = "stopping…" if stopping else step
	stop_button.visible = can_cancel
	stop_button.disabled = stopping
	set_shown(run_id != "" and step != "" and now() - started_at >= SHOW_AFTER and widget.visible)
	if visible:
		place()

func set_shown(shown: bool) -> void:
	if shown == visible:
		return
	visible = shown
	if shown:
		shown_count += 1
		place()
	widget.call_deferred("update_passthrough")

## Centred under the bubble's anchor, as wide as its words.
func place() -> void:
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var font := label.get_theme_font("font")
	var text_w := font.get_string_size(label.text, HORIZONTAL_ALIGNMENT_LEFT, -1, 15).x if font else 120.0
	var width := clampf(text_w + 12.0 + 24.0 + (HEIGHT if stop_button.visible else 0.0), 120.0, w - 28.0)
	var anchor: Vector2 = widget.bubble_anchor_on_screen()
	size = Vector2(width, HEIGHT)
	custom_minimum_size = size
	position = Vector2(roundf((w - width) / 2.0), roundf(anchor.y + GAP))

## The chip's corners in window pixels, for the click-through hull; none while hidden.
func corners() -> PackedVector2Array:
	if not visible:
		return PackedVector2Array()
	var rect := get_global_rect()
	return PackedVector2Array([rect.position, Vector2(rect.end.x, rect.position.y), rect.end,
		Vector2(rect.position.x, rect.end.y)])
