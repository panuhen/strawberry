extends Node
## Touch reactions (WIRING.md §13, "Touch"). Purely local: the widget decides what a press was
## (widget.gd: a short, still press is a poke, a long still one is a hold, anything that moves
## drags her window), and this node decides what she does about it.
##
##   where     a ray from the camera through the press, against her posed meshes:
##             shell top = pat, body/belly/legs = tickle, eye or eyestalk = flinch, claw = pinch back
##             (every other time a wave with that claw). A hold anywhere: she leans into it.
##   how often pokes in a row escalate: curious (the zone's reaction), then mildly annoyed
##             (alert_snap and a squint). The third level, scuttling away, needs legs (a later
##             stage): scuttle_away() is the hook, and until it does something she stays annoyed.
##   asleep    a poke wakes her gently (the wake_up clip), nothing else.
##   busy      talking, listening, thinking, a run going on (the step chip), a one-shot or a
##             daemon reaction playing: only a blink. Nothing here plays a clip over a line,
##             writes the claws while she speaks, or stops a drag.
##
## The recipes are envelopes over bone offsets and morphs, layered like reactions.gd: body
## rotation after the AnimationPlayer (process priority 157, after the reactions and dance
## styles), eyes through the blink controller's "touch" layer, yaw through turn.gd.

signal scuttle_wanted   # level 3: the hook for scuttling away once she has legs that walk

const Easing = preload("res://easing.gd")
const DURATIONS := {"pat": 1.2, "tickle": 1.1, "flinch": 0.75, "pinch": 1.0, "claw_wave": 1.3,
	"annoyed": 1.1, "curious": 1.0, "noticed": 0.35, "hold": 0.0}
const STREAK_S := 4.0            # a poke within this long of the last one continues the streak
const ANNOYED_AT := 3            # the streak's poke that makes her annoyed
const SCUTTLE_AT := 6            # ... and the one that would send her scuttling away
const SHELL_TOP_Y := 0.40        # model space: above this the shell is "top" (pat), below "body"
const HOLD_IN_S := 0.4
const HOLD_OUT_S := 0.5
const TALK_EVERY_S := 25.0       # at most one spoken line this often (the daemon has its own limit)
const TALK_CHANCE := 0.35        # ... and only now and then; always when she first gets annoyed

var widget: Node3D
var camera: Camera3D
var skeleton: Skeleton3D
var blink: Node
var body_i := -1
var claw_i := {}
var claws := {}                  # side -> [mesh, blend shape index]
var squashers: Array[MeshInstance3D] = []
var squash_indices: Array[int] = []
var triangles := {}              # mesh -> PackedVector3Array of its rest-space triangles (lazy)
var rng := RandomNumberGenerator.new()

var recipe := ""
var t := 0.0
var duration := 0.0
var side := 1.0                  # +1: her left (viewer's left, model +X), -1: her right
var lean := Vector2.ZERO         # hold: where the finger is, from her middle, in window pixels
var holding := false
var hold_amount := 0.0
var streak := 0
var last_poke_at := -100.0
var talked_at := -100.0
var snapped_at := -100.0         # when her own annoyance played alert_snap (not a reason to stop counting)
var claw_waves := 0
var wrote_claws := false
var wrote_squash := false
var yaw := 0.0                   # read by turn.gd

# For the checks (validate_touch.gd).
var pokes := 0
var played := {}
var last_zone := ""
var last_level := 0
var scuttles := 0
var lines_asked := 0

func setup(owner: Node3D, model: Node, cam: Camera3D, blink_controller: Node) -> void:
	process_priority = 157
	widget = owner
	camera = cam
	blink = blink_controller
	rng.randomize()
	skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	body_i = skeleton.find_bone("body")
	for s in ["L", "R"]:
		claw_i[s] = skeleton.find_bone("claw_arm_" + s)
		var claw := model.find_child("mesh_claw_lower_" + s, true, false) as MeshInstance3D
		claws[s] = [claw, claw.find_blend_shape_by_name("claw_open_" + s)]
	for mesh_name in ["mesh_shell", "mesh_spots", "mesh_belly"]:
		var mesh := model.find_child(mesh_name, true, false) as MeshInstance3D
		squashers.append(mesh)
		squash_indices.append(mesh.find_blend_shape_by_name("squash"))

func now() -> float:
	return Time.get_ticks_msec() / 1000.0

## Something is going on that a poke must not get in the way of.
func busy() -> bool:
	return widget.state in ["talking", "listening", "thinking"] or widget.bubble.speaking \
		or widget.speech.playing or widget.step_chip.run_id != "" or widget.reactions.recipe != "" \
		or (widget.one_shot != "" and not own_snap())

## The one-shot playing is the alert_snap her annoyance started: pokes keep counting through it.
func own_snap() -> bool:
	return widget.one_shot == "alert_snap" and now() - snapped_at < widget.player.get_animation("alert_snap").length + 0.2

## A short, still press at `pixel` (window pixels).
func poke(pixel: Vector2) -> void:
	if not widget.touch_reactions:
		return
	pokes += 1
	var sleeper: Node = widget.sleeper
	if sleeper and sleeper.phase != "awake":
		sleeper.wake()           # gently: the wake_up clip, and nothing on top of it
		streak = 0
		last_zone = "asleep"
		return
	var hit := hit_test(pixel)
	last_zone = str(hit.zone)
	if busy():
		play("noticed")
		last_level = 0
		return
	var at := now()
	streak = streak + 1 if at - last_poke_at < STREAK_S else 1
	last_poke_at = at
	last_level = 1
	if streak >= SCUTTLE_AT:
		last_level = 3
		if not scuttle_away():
			last_level = 2
	elif streak >= ANNOYED_AT:
		last_level = 2
	if last_level >= 2:
		annoyed()
	else:
		react(hit)
	maybe_talk(str(hit.zone), last_level)

## Level 3. Scuttling off needs legs that walk (the crab plan's later stages); until then the hook
## is announced and she stays annoyed. Return true once she really does it.
func scuttle_away() -> bool:
	scuttles += 1
	scuttle_wanted.emit()
	return false

func annoyed() -> void:
	if widget.one_shot == "" and widget.state in ["idle", "dancing"]:
		widget.play_one_shot("alert_snap")
		snapped_at = now()
	play("annoyed")

func react(hit: Dictionary) -> void:
	side = float(hit.side)
	match str(hit.zone):
		"shell":
			play("pat")
		"belly":
			play("tickle")
		"eye":
			play("flinch")
		"claw":
			claw_waves += 1
			play("claw_wave" if claw_waves % 2 == 0 else "pinch")
		_:
			# Beside her (the padding around her outline): a curious look toward the touch.
			side = -1.0 if float(hit.dx) > 0.0 else 1.0
			widget.turn.glance_side(-side * 0.6, 0.6)
			play("curious")

func play(name: String) -> void:
	recipe = name
	t = 0.0
	duration = DURATIONS[name]
	played[name] = int(played.get(name, 0)) + 1

## A long, still press: she leans into it until it ends (release_hold), or it becomes a drag.
func begin_hold(pixel: Vector2) -> void:
	if not widget.touch_reactions or busy() or (widget.sleeper and widget.sleeper.phase != "awake"):
		return
	holding = true
	lean = pixel - world_to_pixel(skeleton.global_transform * skeleton.get_bone_global_pose(body_i).origin)
	if recipe != "hold":
		play("hold")

func release_hold() -> void:
	holding = false

## The optional spoken line ("Talk when poked", off by default): the daemon picks and voices it,
## and turns it down while she is busy or has spoken one lately (PROTOCOL.md, `poked`).
func maybe_talk(zone: String, level: int) -> void:
	if not widget.touch_talk or widget.ws == null or not widget.ws.is_open():
		return
	if now() - talked_at < TALK_EVERY_S:
		return
	if not (level >= 2 and streak == ANNOYED_AT) and rng.randf() > TALK_CHANCE:
		return
	talked_at = now()
	lines_asked += 1
	widget.ws.send({"type": "poked", "zone": zone if zone in ["shell", "belly", "eye", "claw"] else "near", "level": level})

func _process(delta: float) -> void:
	yaw = 0.0
	if widget.pressing and not widget.dragging and not holding and now() - widget.press_at >= widget.HOLD_S:
		begin_hold(widget.press_pos)
	if recipe == "" and hold_amount <= 0.0:
		finish_writes()
		return
	var body_pitch := 0.0
	var body_roll := 0.0
	var lift := 0.0
	var claw_open := 0.0
	var squash := 0.0
	var wide := 0.0
	var happy := 0.0
	var squint := 0.0
	var closed := 0.0
	if recipe == "hold" or hold_amount > 0.0:
		var target := 1.0 if holding and not widget.dragging else 0.0
		hold_amount = move_toward(hold_amount, target, delta / (HOLD_IN_S if target > 0.0 else HOLD_OUT_S))
		var h := Easing.in_out(hold_amount)
		# Into the finger: lean toward it a little, eyes half shut, a slow, content breath.
		body_roll = deg_to_rad(5.0) * clampf(-lean.x / 120.0, -1.0, 1.0) * h
		body_pitch = deg_to_rad(-6.0) * h
		squint = 0.6 * h
		squash = 0.12 * (0.5 - 0.5 * cos(TAU * now() / 2.4)) * h
		if hold_amount <= 0.0 and not holding and recipe == "hold":
			recipe = ""
	if recipe != "" and recipe != "hold":
		t += delta
		var p := clampf(t / duration, 0.0, 1.0)
		var env := Easing.envelope(p, 0.15, 0.3)
		match recipe:
			"pat":
				# Pressed down a little, a happy squint and a wiggle that dies away.
				squash = 0.25 * Easing.envelope(p, 0.1, 0.5)
				squint = 0.75 * env   # the happy morph tucks the lids away; a content squint shows
				body_roll = deg_to_rad(4.0) * sin(TAU * 3.0 * t) * (1.0 - p) * env
			"tickle":
				# Squash and a giggling shiver.
				squash = 0.35 * absf(sin(TAU * 4.0 * t)) * env
				body_roll = deg_to_rad(2.5) * sin(TAU * 12.0 * t) * env
				squint = 0.8 * env    # eyes screwed up, giggling
			"flinch":
				# Eyes shut at once, a jerk back and away, then open again.
				closed = 1.0 - Easing.in_out((p - 0.35) / 0.4)
				body_pitch = deg_to_rad(7.0) * Easing.envelope(p, 0.08, 0.6)
				yaw = -side * deg_to_rad(6.0) * Easing.envelope(p, 0.1, 0.6)
			"pinch":
				# That claw comes up and snaps at the finger twice, playfully.
				lift = deg_to_rad(28.0) * Easing.back_out(p / 0.25) * (1.0 - Easing.in_out((p - 0.7) / 0.3))
				claw_open = 0.9 * maxf(0.0, sin(TAU * 2.0 * clampf((p - 0.15) / 0.6, 0.0, 1.0))) * env
				wide = 0.5 * env
				happy = 0.4 * env
			"claw_wave":
				lift = deg_to_rad(46.0) * Easing.back_out(p / 0.22) * (1.0 - Easing.in_out((p - 0.75) / 0.25))
				claw_open = 0.8 * env * maxf(0.0, sin(TAU * 2.0 * p))
				yaw = side * -deg_to_rad(6.0) * Easing.there_and_back(p / 0.5)
				happy = 0.6 * env
			"annoyed":
				squint = 0.85 * env
				body_pitch = deg_to_rad(4.0) * env
				squash = 0.12 * Easing.envelope(p, 0.1, 0.4)
			"curious":
				wide = 0.6 * env
				body_pitch = deg_to_rad(-4.0) * env
			"noticed":
				closed = 0.8 * Easing.envelope(p, 0.3, 0.5)
		if p >= 1.0:
			recipe = "hold" if hold_amount > 0.0 else ""
	if body_pitch != 0.0 or body_roll != 0.0:
		var q := Quaternion(Vector3.RIGHT, body_pitch) * Quaternion(Vector3.BACK, body_roll)
		skeleton.set_bone_pose_rotation(body_i, skeleton.get_bone_pose_rotation(body_i) * q)
	var s := "L" if side > 0.0 else "R"
	if lift != 0.0:
		skeleton.set_bone_pose_rotation(claw_i[s], skeleton.get_bone_pose_rotation(claw_i[s]) * Quaternion(Vector3.RIGHT, lift))
	# The claws are her voice's while she talks (speech_player.gd writes them after this node).
	if claw_open > 0.0 and not widget.speech.playing:
		claws[s][0].set_blend_shape_value(claws[s][1], claw_open)
		wrote_claws = true
	elif wrote_claws:
		clear_claws()
	if squash > 0.0:
		for i in squashers.size():
			var base := squashers[i].get_blend_shape_value(squash_indices[i])
			squashers[i].set_blend_shape_value(squash_indices[i], clampf(base + squash, 0.0, 1.0))
		wrote_squash = true
	blink.set_layer("touch", wide, happy, squint, closed)

## Once nothing plays: the touch layer's eyes and claws go back to their owners.
func finish_writes() -> void:
	blink.set_layer("touch", 0.0, 0.0, 0.0)
	if wrote_claws:
		clear_claws()
	wrote_squash = false

func clear_claws() -> void:
	wrote_claws = false
	if widget.speech.playing:
		return
	for s in claws:
		claws[s][0].set_blend_shape_value(claws[s][1], 0.0)

# --- where she was touched --------------------------------------------------------

## Window pixel -> world point on the camera's plane (the orthographic camera, from the project's
## window size, as gaze.gd does: a headless viewport is not the window's size).
func pixel_to_world(pixel: Vector2) -> Vector3:
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var h := float(ProjectSettings.get_setting("display/window/size/viewport_height"))
	var units_per_px := camera.size / w
	var origin := camera.global_position
	return Vector3(origin.x - (pixel.x - w / 2.0) * units_per_px, origin.y - (pixel.y - h / 2.0) * units_per_px, origin.z)

## The inverse: a world point -> window pixel (the checks use it to aim).
func world_to_pixel(point: Vector3) -> Vector2:
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var h := float(ProjectSettings.get_setting("display/window/size/viewport_height"))
	var px_per_unit := w / camera.size
	var origin := camera.global_position
	return Vector2(w / 2.0 - (point.x - origin.x) * px_per_unit, h / 2.0 - (point.y - origin.y) * px_per_unit)

## What a press at `pixel` lands on: {zone: shell|belly|eye|claw|near, side: ±1, dx: px from her middle}.
## Each mesh follows one bone rigidly (WIRING.md §13), so the ray goes into that bone's rest space
## and meets the mesh's own triangles there; the nearest hit wins. The hat counts as her shell.
func hit_test(pixel: Vector2) -> Dictionary:
	var origin := pixel_to_world(pixel)
	var direction := (camera.global_transform.basis * Vector3.FORWARD).normalized()
	var best := INF
	var found := {"zone": "near", "side": 1.0, "dx": pixel.x - world_to_pixel(skeleton.global_transform * skeleton.get_bone_global_pose(body_i).origin).x}
	if widget.bone_boxes.is_empty():
		widget.bone_boxes = widget.find_bone_boxes()
	for node in widget.model.find_children("*", "MeshInstance3D", true, false):
		var mesh := node as MeshInstance3D
		if not mesh.is_visible_in_tree() or mesh.mesh == null:
			continue
		var to_world: Transform3D
		if widget.bone_boxes.has(mesh):
			var part: Array = widget.bone_boxes[mesh][0]
			to_world = skeleton.global_transform * skeleton.get_bone_global_pose(part[0]) * part[1]
			if not (to_world * (part[2] as AABB)).grow(0.01).intersects_ray(origin, direction):
				continue
		else:
			to_world = mesh.global_transform   # the hat's parts
		var from_world := to_world.affine_inverse()
		var local_origin := from_world * origin
		var local_direction := (from_world.basis * direction).normalized()
		var tris := mesh_triangles(mesh)
		for i in range(0, tris.size(), 3):
			var at: Variant = Geometry3D.ray_intersects_triangle(local_origin, local_direction, tris[i], tris[i + 1], tris[i + 2])
			if at == null:
				continue
			var world: Vector3 = to_world * (at as Vector3)
			var depth := (world - origin).dot(direction)
			if depth < best:
				best = depth
				found.zone = zone_of(mesh, world)
				# Her left is model +X (the viewer's left).
				found.side = 1.0 if (widget.model.global_transform.affine_inverse() * world).x >= 0.0 else -1.0
	return found

func zone_of(mesh: MeshInstance3D, world: Vector3) -> String:
	var name := String(mesh.name)
	if name.begins_with("mesh_eye"):
		return "eye"
	if name.begins_with("mesh_claw"):
		return "claw"
	if name in ["mesh_shell", "mesh_spots"]:
		var local: Vector3 = widget.model.global_transform.affine_inverse() * world
		return "shell" if local.y >= SHELL_TOP_Y else "belly"
	if name.begins_with("mesh_"):
		return "belly"   # the belly and the legs
	return "shell"       # the hat

## A mesh's triangles in its own rest space, read once (surface arrays, by index).
func mesh_triangles(mesh: MeshInstance3D) -> PackedVector3Array:
	if triangles.has(mesh):
		return triangles[mesh]
	var tris := PackedVector3Array()
	for s in mesh.mesh.get_surface_count():
		var arrays := mesh.mesh.surface_get_arrays(s)
		var vertices: PackedVector3Array = arrays[Mesh.ARRAY_VERTEX]
		var indices: PackedInt32Array = arrays[Mesh.ARRAY_INDEX] if arrays[Mesh.ARRAY_INDEX] != null else PackedInt32Array()
		if indices.is_empty():
			tris.append_array(vertices)
		else:
			for i in indices:
				tris.append(vertices[i])
	triangles[mesh] = tris
	return tris
