extends Control
## A small ring beside her that fills while the user holds a hand gesture (WIRING.md §26, PROTOCOL.md
## Part 1d). Fed by the brain's `gesture` events: `started` and `progress` fill it (0..1), `done` flashes it
## full and green, `cancelled` lets it fade from where it stopped, and so does a hold that goes quiet. Only a
## gesture's name and progress reach it, never the hand or the camera. It takes no clicks.

const RADIUS := 15.0
const WIDTH := 4.0
const FADE_S := 0.35
const QUIET_S := 1.0             # no event for this long: the hold is over
const DONE_HOLD_S := 0.3         # a done ring stays full this long, then fades
const INK := Color("fff7ec")
const TRACK := Color(1.0, 1.0, 1.0, 0.28)
const DONE := Color("8fe388")
const OFFSET := Vector2(-130.0, 36.0)   # from the bubble's anchor: beside her, left of the step chip

var widget: Node3D
var progress := 0.0
var alpha := 0.0
var finished := false
var gesture := ""
var last_at := -100.0
var shown_count := 0             # holds that showed it (validate_widget.gd)
var done_count := 0

func setup(owner: Node3D) -> void:
	widget = owner
	name = "GestureRing"
	mouse_filter = Control.MOUSE_FILTER_IGNORE
	var side := RADIUS * 2.0 + WIDTH * 2.0
	size = Vector2(side, side)
	visible = false

func now() -> float:
	return Time.get_ticks_msec() / 1000.0

## One `gesture` event. Returns true when a hold just started (she glances toward the user then).
func on_gesture(data: Dictionary) -> bool:
	var phase := str(data.get("phase", ""))
	var value: Variant = data.get("progress", 0.0)
	var p := clampf(float(value) if (value is float or value is int) else 0.0, 0.0, 1.0)
	var started := false
	last_at = now()
	match phase:
		"started", "progress":
			started = not visible or finished or gesture != str(data.get("name", ""))
			if started:
				shown_count += 1
			finished = false
			progress = p
			alpha = 1.0
			visible = true
		"done":
			finished = true
			progress = 1.0
			alpha = 1.0
			visible = true
			done_count += 1
		"cancelled":
			finished = false
			progress = p
			last_at = now() - QUIET_S     # fade from where it stopped, at once
		_:
			return false
	gesture = str(data.get("name", ""))
	place()
	queue_redraw()
	return started

func _process(delta: float) -> void:
	if not visible:
		return
	if now() - last_at > (DONE_HOLD_S if finished else QUIET_S):
		alpha = maxf(alpha - delta / FADE_S, 0.0)
		if alpha <= 0.0:
			visible = false
			progress = 0.0
		queue_redraw()
	place()

## Beside her: left of where the step chip hangs under her line, kept inside the window.
func place() -> void:
	if widget == null or not widget.has_method("bubble_anchor_on_screen"):
		return
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var anchor: Vector2 = widget.bubble_anchor_on_screen()
	var centre := Vector2(clampf(anchor.x + OFFSET.x, size.x, w - size.x), anchor.y + OFFSET.y)
	position = (centre - size / 2.0).round()

func _draw() -> void:
	var centre := size / 2.0
	draw_arc(centre, RADIUS, 0.0, TAU, 48, Color(TRACK, TRACK.a * alpha), WIDTH, true)
	if progress > 0.0:
		var colour := DONE if finished else INK
		draw_arc(centre, RADIUS, -PI / 2.0, -PI / 2.0 + TAU * progress, 48, Color(colour, alpha), WIDTH, true)
