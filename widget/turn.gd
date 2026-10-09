extends Node
## Turning (WIRING.md §13, "Turning and the tilt"). Her yaw and pitch, applied to the model node,
## so the camera, the bubble, the badge and the step chip stay where they are.
##
##   place     where her window sits on its monitor: near the top you see her a little from below,
##             near the bottom a little from above, near a side edge she turns a little toward the
##             middle. The "Turn toward the screen" setting; eased, so it follows a drag smoothly.
##   drift     a slow, small yaw while she is idle.
##   glance    a 3/4 turn toward something (a notification arriving, the middle of the screen now
##             and then, the cursor coming to rest near her), held, then back to face the user.
##   layers    the reaction recipes (peek and wave turn and come back), the dance styles (yaw on
##             the beat) and touch each set a yaw of their own every frame.
##
## Everything is summed and clamped to MAX_YAW / MAX_PITCH: she always comes back to face the user.
## Model space: she faces -Z (the camera); +yaw turns her face to the viewer's right, +pitch tips
## her top away from the viewer (you see her from below).

const Easing = preload("res://easing.gd")
const MAX_YAW := deg_to_rad(15.0)
const MAX_PITCH := deg_to_rad(5.0)
const PLACE_YAW := deg_to_rad(6.0)      # at a left or right edge of the monitor
const PLACE_PITCH := deg_to_rad(4.0)    # at the top or bottom
const PLACE_DEAD := 0.25                # the middle of the monitor (normalised) is neutral
const PLACE_RATE := 5.0                 # per second, toward the place's angles
const DRIFT_YAW := deg_to_rad(4.0)
const GLANCE_YAW := deg_to_rad(12.0)
const GLANCE_IN_S := 0.35
const GLANCE_OUT_S := 0.55
const CENTRE_EVERY_S := Vector2(25.0, 50.0)   # idle: a look toward the middle of the screen
const CURSOR_NEAR_PX := 420.0
const CURSOR_REST_S := 0.4
const CURSOR_EVERY_S := 8.0

var widget: Node3D
var model: Node3D
var enabled := true                     # the location tilt ("Turn toward the screen")
var location_override := Vector2(INF, INF)   # --at=u,v: the place on the monitor, -1..1 each way
var place := Vector2.ZERO               # eased (yaw, pitch) from the place
var drift_clock := 0.0
var idle_weight := 0.0
var glance_to := 0.0                    # the glance's yaw, its clock and length (0: none)
var glance_t := 0.0
var glance_len := 0.0
var glances := 0
var until_centre := 30.0
var cursor_last := Vector2i(-99999, -99999)
var cursor_still := 0.0
var cursor_glanced_at := -100.0
var yaw := 0.0                          # what was applied this frame
var pitch := 0.0
var rng := RandomNumberGenerator.new()

func setup(owner: Node3D, model_node: Node3D) -> void:
	widget = owner
	model = model_node
	# After the reactions (150), dance styles (155) and touch (157) have set their yaw; before the
	# gaze (170), which aims the pupils in her turned frame, and the hat (180).
	process_priority = 165
	rng.randomize()
	until_centre = rng.randf_range(CENTRE_EVERY_S.x, CENTRE_EVERY_S.y)

func _process(delta: float) -> void:
	advance(delta)

func advance(delta: float) -> void:
	var where := location()
	var target := Vector2.ZERO
	if enabled and where.x != INF:
		target = Vector2(-signf(where.x) * PLACE_YAW * smoothstep(PLACE_DEAD, 1.0, absf(where.x)),
			-signf(where.y) * PLACE_PITCH * smoothstep(PLACE_DEAD, 1.0, absf(where.y)))
	place = place.lerp(target, 1.0 - exp(-PLACE_RATE * delta))
	var awake: bool = widget.sleeper == null or widget.sleeper.phase == "awake"
	var idle: bool = awake and widget.state == "idle" and widget.one_shot == ""
	idle_weight = move_toward(idle_weight, 1.0 if idle else 0.0, delta / 1.5)
	drift_clock += delta
	var drift := DRIFT_YAW * (0.6 * sin(TAU * drift_clock / 23.0) + 0.4 * sin(TAU * drift_clock / 9.7 + 1.3))
	if idle and glance_len == 0.0:
		until_centre -= delta
		if until_centre <= 0.0:
			until_centre = rng.randf_range(CENTRE_EVERY_S.x, CENTRE_EVERY_S.y)
			glance_at_centre(1.8)
	watch_cursor(delta, idle)
	var glance := 0.0
	if glance_len > 0.0:
		glance_t += delta
		glance = glance_to * Easing.envelope(glance_t / glance_len, GLANCE_IN_S / glance_len, GLANCE_OUT_S / glance_len)
		if glance_t >= glance_len:
			glance_len = 0.0
	var layered: float = widget.reactions.yaw + widget.dance.yaw
	if widget.touch:
		layered += widget.touch.yaw
	yaw = clampf(place.x + drift * Easing.in_out(idle_weight) + glance + layered, -MAX_YAW, MAX_YAW)
	pitch = clampf(place.y, -MAX_PITCH, MAX_PITCH)
	apply()

## Her rotation: yaw about her own up, then the view's tilt about the screen's horizontal.
func apply() -> void:
	model.transform.basis = Basis(Vector3.RIGHT, pitch) * Basis(Vector3.UP, yaw)

## Where her window sits on the monitor it is on: x -1 (left edge) .. 1 (right edge), y -1 (top)
## .. 1 (bottom). INF when that cannot be known (headless, no screen).
func location() -> Vector2:
	if location_override.x != INF:
		return location_override
	if widget.is_headless():
		return Vector2(INF, INF)
	var position := DisplayServer.window_get_position()
	var size := DisplayServer.window_get_size()
	var center := position + size / 2
	var screen: int = widget.screen_at(center)
	if screen < 0:
		return Vector2(INF, INF)
	var usable := DisplayServer.screen_get_usable_rect(screen)
	if usable.size.x <= 0 or usable.size.y <= 0:
		return Vector2(INF, INF)
	var middle := Vector2(usable.position) + Vector2(usable.size) / 2.0
	var reach := (Vector2(usable.size) - Vector2(size)) / 2.0
	return Vector2(clampf((center.x - middle.x) / maxf(reach.x, 1.0), -1.0, 1.0),
		clampf((center.y - middle.y) / maxf(reach.y, 1.0), -1.0, 1.0))

## A 3/4 turn toward a point on the screen (desktop pixels), held for `hold` seconds, then back.
func glance_at(point: Vector2, hold := 1.4) -> void:
	var center := Vector2(DisplayServer.window_get_position() + DisplayServer.window_get_size() / 2)
	var dx := point.x - center.x
	if absf(dx) < 40.0:
		return     # straight above or below her: nothing to turn to
	glance_side(signf(dx) * smoothstep(40.0, 500.0, absf(dx)), hold)

## A glance toward the viewer's right (side > 0) or left, `side` -1..1 of GLANCE_YAW.
func glance_side(side: float, hold := 1.4) -> void:
	glance_to = clampf(side, -1.0, 1.0) * GLANCE_YAW
	glance_t = 0.0
	glance_len = GLANCE_IN_S + hold + GLANCE_OUT_S
	glances += 1

## Toward the middle of her monitor. Headless (or right in the middle) she picks a side.
func glance_at_centre(hold := 1.4) -> void:
	var where := location()
	if where.x == INF or absf(where.x) < 0.1:
		glance_side(1.0 if rng.randf() < 0.5 else -1.0, hold)
	else:
		glance_side(-signf(where.x) * clampf(absf(where.x) * 1.5, 0.5, 1.0), hold)

## A notification arriving: toward the top middle of her monitor, where the desktop shows them.
func glance_at_notification() -> void:
	var where := location()
	if where.x == INF or widget.is_headless():
		glance_at_centre(1.2)
		return
	var usable := DisplayServer.screen_get_usable_rect(widget.screen_at(DisplayServer.window_get_position() + DisplayServer.window_get_size() / 2))
	glance_at(Vector2(usable.position.x + usable.size.x / 2.0, usable.position.y), 1.2)

## The cursor coming to rest near her (and not on her): a glance toward it, now and then.
func watch_cursor(delta: float, idle: bool) -> void:
	if widget.is_headless() or not idle or widget.dragging:
		return
	var mouse := DisplayServer.mouse_get_position()
	if mouse != cursor_last:
		cursor_last = mouse
		cursor_still = 0.0
		return
	cursor_still += delta
	if cursor_still < CURSOR_REST_S or cursor_still - delta >= CURSOR_REST_S:
		return     # once per rest
	var rect := Rect2(Vector2(DisplayServer.window_get_position()), Vector2(DisplayServer.window_get_size()))
	var center := rect.get_center()
	var away := Vector2(mouse).distance_to(center)
	if rect.has_point(Vector2(mouse)) or away > CURSOR_NEAR_PX:
		return
	if drift_clock - cursor_glanced_at < CURSOR_EVERY_S or glance_len > 0.0:
		return
	cursor_glanced_at = drift_clock
	glance_at(Vector2(mouse), 1.0)
