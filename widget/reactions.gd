extends Node
## Procedural reaction recipes layered over whatever clip is playing (WIRING.md §13).
##
## A recipe is a short envelope (ease in, hold, ease out) that adds bone rotations on top
## of the animation and drives a few morphs. Nothing here is baked into the GLB, so a
## recipe is a dozen lines you can tune live. Bone axes come from the rig:
## claw lift = local +X, eyestalk sway = local Z, body pitch = local X, body roll = local Z.
## Turning (yaw) is not a bone: a recipe sets `yaw` and turn.gd turns the whole model by it.
## Legs go through legs.gd: a recipe asks for toe offsets and body lifts, and the legs are solved
## after every layer (shiver trembles them, peek goes on tiptoe, double_hop crouches and lands).
##
## Runs as a plain node at process priority 150, after the AnimationPlayer has written the
## frame's pose, the same way blink_controller and claw_controller layer their morphs.
## (A SkeletonModifier3D renders the same result but its changes are invisible to
## get_bone_global_pose(), which the acceptance check reads.) Every bone touched here has a
## track in all seven clips, so the mixer resets it each frame and offsets never compound.

const Easing = preload("res://easing.gd")
const DURATIONS := {"wave": 1.5, "peek": 1.4, "shiver": 0.9, "double_hop": 1.6, "nod": 1.1}

var recipe := ""
var t := 0.0
var duration := 0.0
var applied_frames := 0
var completed := 0

var skeleton: Skeleton3D
var body_i := -1
var claw_l_i := -1
var claw_r_i := -1
var eye_l_i := -1
var eye_r_i := -1
var eye_rest_scale := Vector3.ONE
var blink_controller: Node
var pincers: Node               # claw_controller.gd: the wave's claw opens through it
var legs: Node                  # legs.gd: toe offsets and body lifts (the leg pass)
var player: AnimationPlayer     # double_hop's crouch and landing follow the notify_perk clip
var squashers: Array[MeshInstance3D] = []
var squash_indices: Array[int] = []
var wrote_squash := false

# The offsets for this frame, computed once in _process and applied in the modifier pass.
var body_pitch := 0.0
var body_roll := 0.0
var claw_l_lift := 0.0
var eye_stretch := 0.0
var yaw := 0.0                   # radians, read by turn.gd: +turns her face to the viewer's right
var turn_side := 1.0

func setup(model: Node, blink: Node) -> void:
	skeleton = model.find_child("Skeleton3D", true, false) as Skeleton3D
	body_i = skeleton.find_bone("body")
	claw_l_i = skeleton.find_bone("claw_arm_L")
	claw_r_i = skeleton.find_bone("claw_arm_R")
	eye_l_i = skeleton.find_bone("eyestalk_L")
	eye_r_i = skeleton.find_bone("eyestalk_R")
	eye_rest_scale = skeleton.get_bone_pose_scale(eye_l_i)
	blink_controller = blink
	for mesh_name in ["mesh_shell", "mesh_spots"]:
		var mesh := model.find_child(mesh_name, true, false) as MeshInstance3D
		squashers.append(mesh)
		squash_indices.append(mesh.find_blend_shape_by_name("squash"))
	process_priority = 150  # after the blink/claw controllers (100), so our morph writes win

func play(name: String) -> bool:
	if not DURATIONS.has(name):
		push_warning("unknown reaction: " + name)
		return false
	recipe = name
	t = 0.0
	duration = DURATIONS[name]
	# Peek looks round to one side or the other; the wave turns toward the lifted (left) claw.
	turn_side = -1.0 if name == "wave" else (1.0 if randf() < 0.5 else -1.0)
	return true

func _process(delta: float) -> void:
	if recipe == "":
		return
	t += delta
	var p := clampf(t / duration, 0.0, 1.0)
	var env := smoothstep(0.0, 0.15, p) * (1.0 - smoothstep(0.78, 1.0, p))
	body_pitch = 0.0
	body_roll = 0.0
	claw_l_lift = 0.0
	eye_stretch = 0.0
	yaw = 0.0
	var claw_open := Vector2.ZERO
	var squash_extra := 0.0
	var wide := 0.0
	var happy := 0.0
	var squint := 0.0
	match recipe:
		"wave":
			# The claw snaps up a little past 48° and settles, then eases down with the envelope.
			claw_l_lift = deg_to_rad(48.0) * Easing.back_out(p / 0.22) * (1.0 - smoothstep(0.78, 1.0, p))
			claw_open.x = 0.9 * env * maxf(0.0, sin(TAU * 2.0 * p))  # two open-close beats
			wide = 0.8 * env
			yaw = turn_side * deg_to_rad(8.0) * Easing.there_and_back(p / 0.45)   # a quick turn and back
		"peek":
			eye_stretch = 0.35 * env
			yaw = turn_side * deg_to_rad(10.0) * Easing.there_and_back(p / 0.55)
			body_pitch = deg_to_rad(-7.0) * env  # lean toward the viewer
			wide = 0.5 * env
			if legs:
				# On tiptoe: up a little, the toes drawn in under her to reach.
				legs.lift(0.02 * env)
				legs.add_all(Vector3.ZERO, 0.035 * env)
		"shiver":
			body_roll = deg_to_rad(3.0) * sin(TAU * 18.0 * t) * env
			squash_extra = 0.3 * absf(sin(TAU * 9.0 * t)) * env
			squint = 0.6 * env
			if legs:
				# Every leg trembles on its own.
				for i in legs.LEGS.size():
					var k := float(i) * 1.7
					legs.add(legs.LEGS[i], Vector3(sin(TAU * 21.0 * t + k), 0.6 * absf(sin(TAU * 17.0 * t + k * 2.0)), cos(TAU * 19.0 * t + k)) * 0.005 * env)
		"double_hop":
			wide = 0.9 * env  # the hops themselves are the notify_perk clip, played twice by the widget
			if legs and player and player.assigned_animation == "notify_perk" and player.is_playing():
				# A crouch before each jump and a knee bend as she lands: the body drops, the toes stay.
				var hop := player.current_animation_position / player.get_animation("notify_perk").length
				var crouch := sin(PI * clampf(hop / 0.18, 0.0, 1.0))
				var land := sin(PI * clampf((hop - 0.6) / 0.24, 0.0, 1.0))
				legs.lift(-0.03 * crouch - 0.034 * land)
		"nod":
			body_pitch = deg_to_rad(10.0) * sin(TAU * 2.0 * p) * env
			happy = env
	blink_controller.extra_wide = wide
	blink_controller.extra_happy = happy
	blink_controller.extra_squint = squint
	if claw_open != Vector2.ZERO and pincers:
		pincers.request_both(claw_open)
	if squash_extra > 0.0 or wrote_squash:
		for i in squashers.size():
			var base := squashers[i].get_blend_shape_value(squash_indices[i])
			squashers[i].set_blend_shape_value(squash_indices[i], clampf(base + squash_extra, 0.0, 1.0))
		wrote_squash = squash_extra > 0.0
	apply()
	if p >= 1.0:
		finish()

func finish() -> void:
	recipe = ""
	completed += 1
	body_pitch = 0.0
	body_roll = 0.0
	claw_l_lift = 0.0
	eye_stretch = 0.0
	yaw = 0.0
	blink_controller.extra_wide = 0.0
	blink_controller.extra_happy = 0.0
	blink_controller.extra_squint = 0.0
	if skeleton:
		skeleton.set_bone_pose_scale(eye_l_i, eye_rest_scale)
		skeleton.set_bone_pose_scale(eye_r_i, eye_rest_scale)

## Adds this frame's offsets on top of the pose the AnimationPlayer just wrote.
func apply() -> void:
	if skeleton == null:
		return
	applied_frames += 1
	if body_pitch != 0.0 or body_roll != 0.0:
		var q := Quaternion(Vector3.RIGHT, body_pitch) * Quaternion(Vector3.BACK, body_roll)
		skeleton.set_bone_pose_rotation(body_i, skeleton.get_bone_pose_rotation(body_i) * q)
	if claw_l_lift != 0.0:
		skeleton.set_bone_pose_rotation(claw_l_i, skeleton.get_bone_pose_rotation(claw_l_i) * Quaternion(Vector3.RIGHT, claw_l_lift))
	if eye_stretch != 0.0:
		var s := eye_rest_scale * Vector3(1.0, 1.0 + eye_stretch, 1.0)
		skeleton.set_bone_pose_scale(eye_l_i, s)
		skeleton.set_bone_pose_scale(eye_r_i, s)
