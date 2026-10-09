extends Node
## Walking (WIRING.md §13, "Wander and scuttle"). Measures how fast her window moves and hands it to
## the gait (legs.gd: a drag sets her legs going), and moves the window itself when she walks:
##
##   wander   now and then (every WANDER_S, at random) she walks a short way left or right on her
##            own: right near the left edge of her monitor, left near the right edge, either way in
##            the middle, sometimes out and back. "Wander" in her menu, on by default.
##   scuttle  touch level 3: on the sixth poke in a row she scuttles a short step away from it.
##
## She stays on her monitor: the whole window within that monitor's usable area, never onto
## another display. She never walks while she is talking, listening, thinking or dancing, being
## dragged or pressed, showing the step chip or an approval card, asleep, or during a run; a walk
## stops at once (she settles) if any of those start, and never moves her while the pointer is
## pressed on her. The window moves with her steps (`DisplayServer.window_set_position`); the
## click-through polygon is in window pixels, so it moves with her (on Windows follow_pose keeps
## taking her posed legs in).

const Easing = preload("res://easing.gd")
const WANDER_S := Vector2(300.0, 900.0)  # 5 to 15 minutes between walks
const RETRY_S := Vector2(20.0, 60.0)     # the walk came due while she was busy: try again this soon
const WALK_PX := Vector2(80.0, 200.0)    # how far a walk goes
const WALK_PX_S := 70.0
const OUT_AND_BACK := 0.3                # in the middle of her monitor, this often a walk out and back
const EDGE := 0.4                        # |place| past this counts as near an edge (-1..1, as turn.gd)
const MIN_WALK_PX := 30.0                # less room than this on a side: not that way
const SCUTTLE_PX := 70.0
const SCUTTLE_PX_S := 240.0
const SHUFFLE_S := 0.5                   # no room to scuttle: she scrabbles in place this long
const STEP_SURGE := 0.3                  # the window goes faster mid-stance, slower as the feet change
const VELOCITY_RATE := 12.0              # per second, smoothing of a drag's measured speed

var widget: Node3D
var legs: Node
var rng := RandomNumberGenerator.new()
var enabled := true                      # "Wander" (widget.cfg [window] wander)
var until_walk := 0.0

# A walk (or a scuttle) under way.
var walking := false
var kind := ""                           # "wander", "scuttle", "shuffle"
var direction := 0.0                     # +1: right on the screen, -1: left
var speed := 0.0
var goal_x := 0.0                        # window x it walks to
var start_x := 0.0
var back_x := INF                        # out and back: where it returns to after goal_x
var at_x := 0.0                          # the window's x, kept fractional
var shuffle_left := 0.0
var velocity := Vector2.ZERO             # px/s handed to the gait

var last_pos := Vector2i.ZERO
var have_last := false

# For the checks: a stand-in desktop (headless has no windows to move) and what happened.
var desk := {}                           # {"screens": [Rect2i usable, ...], "pos": Vector2i, "size": Vector2i}
var walks := 0
var scuttles := 0
var stops := 0
var stop_reason := ""
var moves := 0

func setup(owner: Node3D, legs_node: Node) -> void:
	process_priority = 140                # before the gait reads `motion` (legs.gd, 159)
	widget = owner
	legs = legs_node
	rng.randomize()
	until_walk = rng.randf_range(WANDER_S.x, WANDER_S.y)

# --- the desktop (or the checks' stand-in) ----------------------------------------------

func available() -> bool:
	return not desk.is_empty() or not widget.is_headless()

func window_pos() -> Vector2i:
	return desk.pos if not desk.is_empty() else DisplayServer.window_get_position()

func window_size() -> Vector2i:
	return desk.size if not desk.is_empty() else DisplayServer.window_get_size()

func set_window_pos(p: Vector2i) -> void:
	moves += 1
	if not desk.is_empty():
		desk.pos = p
	else:
		DisplayServer.window_set_position(p)

## The usable area of the monitor her window's middle is on (the same rule as turn.gd).
func usable_rect() -> Rect2i:
	var center := window_pos() + window_size() / 2
	if not desk.is_empty():
		for r: Rect2i in desk.screens:
			if r.has_point(center):
				return r
		return Rect2i()
	var screen: int = widget.screen_at(center)
	return DisplayServer.screen_get_usable_rect(screen) if screen >= 0 else Rect2i()

## The window's x range on its monitor: its whole width stays inside the usable area.
func x_range() -> Vector2:
	var usable := usable_rect()
	return Vector2(usable.position.x, usable.position.x + usable.size.x - window_size().x)

## Where she sits across her monitor, -1 (left edge) .. 1 (right edge).
func place() -> float:
	var r := x_range()
	if r.y <= r.x:
		return 0.0
	return clampf((window_pos().x - r.x) / (r.y - r.x) * 2.0 - 1.0, -1.0, 1.0)

# --- when she may ------------------------------------------------------------------------

## Nothing going on that a walk would get in the way of. `why` names the first thing that is.
func blocker() -> String:
	if widget.dragging or widget.pressing:
		return "pressed"
	if widget.sleeper and widget.sleeper.phase != "awake":
		return "asleep"
	if widget.state != "idle" or widget.rest_state != "idle":
		return widget.state if widget.state != "idle" else widget.rest_state
	if widget.speech.playing or widget.bubble.speaking:
		return "talking"
	if widget.step_chip and (widget.step_chip.run_id != "" or widget.step_chip.visible):
		return "run"
	if widget.approval_card and (widget.approval_card.is_open() or widget.approval_card.visible):
		return "approval"
	if widget.one_shot != "" and widget.one_shot != "alert_snap":
		return "one_shot"
	if widget.reactions.recipe != "":
		return "reaction"
	if widget.menu.visible or (widget.type_box and widget.type_box.visible):
		return "menu"
	return ""

func can_wander() -> bool:
	return enabled and available() and blocker() == "" and (widget.touch == null or widget.touch.recipe == "")

# --- each frame ----------------------------------------------------------------------------

func _process(delta: float) -> void:
	if not available():
		legs.motion = Vector2.ZERO
		return
	if walking:
		var why := blocker()
		if why != "":
			stop(why)
		else:
			step(delta)
	elif enabled:
		if can_wander():
			until_walk -= delta
			if until_walk <= 0.0:
				if not wander():
					until_walk = rng.randf_range(RETRY_S.x, RETRY_S.y)
	measure(delta)

## The window's speed this frame: a walk's own, or what a drag (or anything else) did to it.
func measure(delta: float) -> void:
	var pos := window_pos()
	if walking:
		legs.motion = velocity
	else:
		var moved := Vector2(pos - last_pos) / maxf(delta, 1e-3) if have_last else Vector2.ZERO
		velocity = velocity.lerp(moved, 1.0 - exp(-VELOCITY_RATE * delta))
		if velocity.length() < 1.0:
			velocity = Vector2.ZERO
		legs.motion = velocity
	last_pos = pos
	have_last = true

## Start a walk now (the timer calls this; so can a check). False when there is nowhere to go.
func wander() -> bool:
	until_walk = rng.randf_range(WANDER_S.x, WANDER_S.y)
	if not can_wander():
		return false
	var r := x_range()
	var x := float(window_pos().x)
	var room_left := x - r.x
	var room_right := r.y - x
	var where := place()
	var dir := 0.0
	if where < -EDGE:
		dir = 1.0
	elif where > EDGE:
		dir = -1.0
	else:
		dir = 1.0 if rng.randf() < 0.5 else -1.0
	if (room_right if dir > 0.0 else room_left) < MIN_WALK_PX:
		dir = -dir
	var room := room_right if dir > 0.0 else room_left
	if room < MIN_WALK_PX:
		return false
	var far := minf(rng.randf_range(WALK_PX.x, WALK_PX.y), room)
	var out_and_back := absf(where) <= EDGE and rng.randf() < OUT_AND_BACK
	begin("wander", dir, far, WALK_PX_S)
	back_x = x if out_and_back else INF
	walks += 1
	return true

## Touch level 3: a short scuttle away from the poke (`away` +1: to the right on the screen). With no
## room that way she scrabbles in place instead. False when she may not move now.
func scuttle(away: float) -> bool:
	if not available() or blocker() != "":
		return false
	scuttles += 1
	var r := x_range()
	var x := float(window_pos().x)
	var room := (r.y - x) if away > 0.0 else (x - r.x)
	if room < MIN_WALK_PX * 0.5:
		begin("shuffle", away, 0.0, SCUTTLE_PX_S)
		shuffle_left = SHUFFLE_S
		return true
	begin("scuttle", away, minf(SCUTTLE_PX, room), SCUTTLE_PX_S)
	return true

func begin(what: String, dir: float, distance: float, px_s: float) -> void:
	walking = true
	kind = what
	direction = dir
	speed = px_s
	at_x = float(window_pos().x)
	start_x = at_x
	goal_x = at_x + dir * distance
	back_x = INF
	stop_reason = ""

func step(delta: float) -> void:
	if kind == "shuffle":
		# Feet going, window still: the gait sees a speed, nothing moves.
		shuffle_left -= delta
		velocity = Vector2(direction * speed, 0.0)
		if shuffle_left <= 0.0:
			finish()
		return
	# Faster mid-stance, slower as the tripods change over: the window moves with her steps.
	var surge := 1.0 + STEP_SURGE * cos(TAU * 2.0 * legs.gait_phase) * Easing.in_out(legs.gait_weight)
	var gone := absf(at_x - start_x)
	var left := absf(goal_x - at_x)
	# Ease in over the first steps and out over the last, so she starts and stops like a walker.
	var pace := clampf(minf(gone, left) / 18.0 + 0.25, 0.25, 1.0)
	var v := speed * surge * pace
	at_x = move_toward(at_x, goal_x, v * delta)
	velocity = Vector2(direction * v, 0.0)
	var r := x_range()
	var x := clampf(roundf(at_x), r.x, r.y)
	var pos := window_pos()
	if int(x) != pos.x:
		set_window_pos(Vector2i(int(x), pos.y))
	if absf(at_x - goal_x) < 0.5:
		if back_x != INF:
			goal_x = back_x
			start_x = at_x
			back_x = INF
			direction = -direction
		else:
			finish()

## The walk is done: she stands, and her place is remembered.
func finish() -> void:
	walking = false
	kind = ""
	velocity = Vector2.ZERO
	legs.motion = Vector2.ZERO
	if not widget.is_headless():
		widget.save_settings()

## Something started: she stops where she is, at once, and her feet settle under her.
func stop(why: String) -> void:
	stops += 1
	stop_reason = why
	walking = false
	kind = ""
	velocity = Vector2.ZERO
	legs.motion = Vector2.ZERO
	if not widget.is_headless():
		widget.save_settings()
