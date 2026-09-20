extends Node
## Procedural reaction recipes layered over whatever clip is playing (WIRING.md §13).
##
## A recipe is a short envelope (ease in, hold, ease out) that adds bone rotations on top
## of the animation and drives a few morphs. Nothing here is baked into the GLB, so a
## recipe is a dozen lines you can tune live. Bone axes come from the rig:
## claw lift = local +X, eyestalk sway = local Z, body pitch = local X, body roll = local Z.
##
## Runs as a plain node at process priority 150, after the AnimationPlayer has written the
## frame's pose, the same way blink_controller and claw_controller layer their morphs.
## (A SkeletonModifier3D renders the same result but its changes are invisible to
## get_bone_global_pose(), which the acceptance check reads.) Every bone touched here has a
## track in all seven clips, so the mixer resets it each frame and offsets never compound.

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
var claws: Array[MeshInstance3D] = []
var claw_indices: Array[int] = []
var squashers: Array[MeshInstance3D] = []
var squash_indices: Array[int] = []
var wrote_claws := false
var wrote_squash := false

# The offsets for this frame, computed once in _process and applied in the modifier pass.
var body_pitch := 0.0
var body_roll := 0.0
var claw_l_lift := 0.0
var eye_stretch := 0.0

func setup(model: Node, blink: Node) -> void:
	skeleton = model.find_child("Skeleton3D", true, false) as Skeleton3D
	body_i = skeleton.find_bone("body")
	claw_l_i = skeleton.find_bone("claw_arm_L")
	claw_r_i = skeleton.find_bone("claw_arm_R")
	eye_l_i = skeleton.find_bone("eyestalk_L")
	eye_r_i = skeleton.find_bone("eyestalk_R")
	eye_rest_scale = skeleton.get_bone_pose_scale(eye_l_i)
	blink_controller = blink
	for suffix in ["L", "R"]:
		var claw := model.find_child("mesh_claw_lower_" + suffix, true, false) as MeshInstance3D
		claws.append(claw)
		claw_indices.append(claw.find_blend_shape_by_name("claw_open_" + suffix))
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
	var claw_open := Vector2.ZERO
	var squash_extra := 0.0
	var wide := 0.0
	var happy := 0.0
	var squint := 0.0
	match recipe:
		"wave":
			claw_l_lift = deg_to_rad(48.0) * env
			claw_open.x = 0.9 * env * maxf(0.0, sin(TAU * 2.0 * p))  # two open-close beats
			wide = 0.8 * env
		"peek":
			eye_stretch = 0.35 * env
			body_pitch = deg_to_rad(-7.0) * env  # lean toward the viewer
			wide = 0.5 * env
		"shiver":
			body_roll = deg_to_rad(3.0) * sin(TAU * 18.0 * t) * env
			squash_extra = 0.3 * absf(sin(TAU * 9.0 * t)) * env
			squint = 0.6 * env
		"double_hop":
			wide = 0.9 * env  # the hops themselves are the notify_perk clip, played twice by the widget
		"nod":
			body_pitch = deg_to_rad(10.0) * sin(TAU * 2.0 * p) * env
			happy = env
	blink_controller.extra_wide = wide
	blink_controller.extra_happy = happy
	blink_controller.extra_squint = squint
	if claw_open != Vector2.ZERO or wrote_claws:
		for i in claws.size():
			claws[i].set_blend_shape_value(claw_indices[i], claw_open[i])
		wrote_claws = claw_open != Vector2.ZERO
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
