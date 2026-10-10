extends Node
## The tuck and spin (widget/SLEEP.md): how she goes to sleep and wakes up, as a procedural layer on
## top of the clip. sleep_controller.gd decides when; this node says how.
##
## Going to sleep: a small crouch, a hop; in the air the legs and claws fold tight and shrink in
## under the shell and the eye stalks bend outward at the base and droop over the rim; she lands right
## side up with a squash, turns once around while the shell wobbles like a settling coin (the tilt's
## low side circles the rim), both slowing together until she faces the user. Asleep only the shell
## and the drooped, shut eyes show; `sleep_loop` breathes. Waking is the reverse: a shiver, a crouch,
## a hop with a small twist, everything pops back out, and she lands on her feet with a bounce.
##
## The pose is a handful of channels (`channels`): the body's lift and squash, how far the legs and
## claws are tucked, how far the eyes droop, the lids, the turn, the coin tilt and a shiver. A sequence
## is a function of time into those channels (`enter_at`, `wake_at`). A sequence that starts mid-way
## through another (a wake during the hop, a nap again while she gets up) blends from the channels she
## is in over BLEND_S, and an unfinished turn unwinds to the nearest full turn, so nothing snaps.
## With the system's reduced-motion setting on (`reduced`) she folds and settles in place instead: no
## hop, no turn, no wobble, no shiver.
##
## `advance` runs from the controller (process priority 80, before the layers); the bones are written
## here at 163: after the leg solve (159) and the pincers (162), before turn.gd (165, which hands its
## own turns over while she is not awake), the gaze (170, which aims from the posed eye stalks) and the
## hat (180, which follows the shell). Skeleton space: +X her left, +Y up, she faces -Z.

const Easing = preload("res://easing.gd")
const Legs = preload("res://legs.gd")
const KEYS := ["w", "lift", "squash", "tuck", "droop", "lids", "yaw", "tilt", "tilt_dir", "shiver"]
const DROP := -0.21                 # asleep: the body this far below standing, her belly on the desk
const CROUCH_S := 0.22
const AIR_S := 0.5
const LAND_AT := CROUCH_S + AIR_S
const ENTER_S := 2.6
const HOP := 0.22                   # the hop's arc: about 0.11 above standing at its top
const TILT := deg_to_rad(11.0)      # the coin wobble's first tilt
const WOBBLES := 2.0                # the tilt's low side goes round the rim this often in the one turn
const SHIVER_S := 0.35
const WAKE_HOP_AT := 0.47
const WAKE_LAND_AT := 1.0
const WAKE_S := 1.5
const WAKE_TWIST := deg_to_rad(70.0)
const RISE_S := 0.4                 # interrupted in the crouch: straight back up
const CALM_ENTER_S := 1.6
const CALM_WAKE_S := 1.2
const BLEND_S := 0.3
const UNWIND_S := 0.8
const DROOP_ANGLE := deg_to_rad(115.0)   # the stalks' bend at the base, outward
const DROOP_FORWARD := deg_to_rad(12.0)  # and a little toward the user
const DROOP_SHIFT := Vector3(0.03, -0.035, 0.0)   # the base slides out and down the dome as it bends
const CLAW_ANGLE := deg_to_rad(70.0)
const CLAW_MIN := 0.08              # tucked claws and legs, as a share of their size
const LEG_MIN := 0.1
const BELLY_BELOW := 0.11           # the belly's lowest point below the body bone's head
const BELLY := Vector3(0.244, 0.058, 0.174)   # the belly's radii (her X, up, her Z); it rocks on it
const BELLY_CENTER := Vector3(0.0, 0.278, -0.005)
const BREATH_FOLLOW := 0.05         # the shell's squash lowers the dome here (the eye stalks' base)

var widget: Node3D
var skeleton: Skeleton3D
var legs: Node
var shell: MeshInstance3D
var squash_index := -1
var bones := {}                     # name -> bone index
var body_rest := Transform3D()      # the body bone's global rest
var leg_fold := {}                  # leg -> [upper, lower] local rotations, folded tight
var sequence := ""                  # "enter", "wake", "rise" or "" (holding)
var reduced := false
var t := 0.0                        # the sequence's clock
var began_at := 0.0                 # where on it this one started
var length := 0.0
var from := {}
var unwind := 0.0                   # the turn left over from an interrupted sequence
var channels := {}
var active := false                 # the bones are being written (anything not at rest)
var written := {}                   # bone -> [position, rotation, scale] this layer wrote last
var bases := {}                     # bone -> the pose under it then
var writes := 0

func setup(owner: Node3D) -> void:
	widget = owner
	process_priority = 163
	skeleton = widget.model.find_children("*", "Skeleton3D", true, false)[0]
	legs = widget.legs
	shell = widget.model.find_child("mesh_shell", true, false) as MeshInstance3D
	squash_index = shell.find_blend_shape_by_name("squash")
	for name in ["root", "body", "eyestalk_L", "eyestalk_R", "claw_arm_L", "claw_arm_R"]:
		bones[name] = skeleton.find_bone(name)
	body_rest = skeleton.get_bone_global_rest(bones.body)
	channels = rest()
	fold_legs()

## Each leg folded tight: the thigh up and out against the inside of the shell, the shin back in
## along it. As local rotations (the body's rest frame), the way legs.gd poses a leg.
func fold_legs() -> void:
	if legs == null:
		return
	for leg: String in Legs.LEGS:
		var s := 1.0 if leg.begins_with("L") else -1.0
		var up_i: int = legs.upper[leg]
		var lo_i: int = legs.lower[leg]
		var thigh := Vector3(s * 0.45, 1.0, 0.0).normalized()
		var shin := Vector3(-s * 0.9, -0.45, 0.0).normalized()
		var normal := thigh.cross(shin).normalized()
		if normal.dot(legs.hinges[leg]) < 0.0:
			normal = -normal
		var frames: Array = legs.rest_frames[leg]
		var upper_global := Legs.frame(thigh, normal) * (frames[0] as Basis).inverse() * skeleton.get_bone_global_rest(up_i).basis.orthonormalized()
		var lower_global := Legs.frame(shin, normal) * (frames[1] as Basis).inverse() * skeleton.get_bone_global_rest(lo_i).basis.orthonormalized()
		leg_fold[leg] = [(body_rest.basis.orthonormalized().inverse() * upper_global).get_rotation_quaternion(),
			(upper_global.inverse() * lower_global).get_rotation_quaternion()]

static func rest() -> Dictionary:
	var c := {}
	for key in KEYS:
		c[key] = 0.0
	return c

## Asleep, settled: tucked, drooped, shut, down on her belly.
static func asleep() -> Dictionary:
	var c := rest()
	c.w = 1.0
	c.lift = DROP
	c.tuck = 1.0
	c.droop = 1.0
	c.lids = 1.0
	return c

# --- the sequences ---------------------------------------------------------------------

## Going to sleep, `at` seconds in.
static func enter_at(at: float) -> Dictionary:
	var c := rest()
	c.w = 1.0
	if at < CROUCH_S:
		var k := Easing.in_out(at / CROUCH_S)
		c.w = Easing.out(at / 0.15)
		c.lift = -0.03 * k
		c.squash = 0.14 * k
		c.lids = 0.45 * k
		return c
	if at < LAND_AT:
		var u := (at - CROUCH_S) / AIR_S
		c.lift = -0.03 + (DROP + 0.03) * u + 4.0 * HOP * u * (1.0 - u)
		if u < 0.12:
			c.squash = lerpf(0.14, -0.12, Easing.out(u / 0.12))
		else:
			c.squash = -0.12 * (1.0 - Easing.in_out((u - 0.12) / 0.33)) - 0.06 * Easing.in_out((u - 0.6) / 0.4)
		c.tuck = Easing.out((u - 0.06) / 0.34)
		c.droop = Easing.in_out((u - 0.2) / 0.6)
		c.lids = 0.45 + 0.55 * Easing.in_out(u / 0.5)
		return c
	var s := at - LAND_AT
	var v := clampf(s / (ENTER_S - LAND_AT), 0.0, 1.0)
	c.lift = DROP
	c.tuck = 1.0
	c.lids = 1.0
	# The landing: the fall's stretch turns into a squash at once, which rings out.
	var hit := Easing.out(s / 0.05)
	c.squash = -0.06 * (1.0 - hit) + 0.3 * hit * exp(-7.0 * s) * cos(TAU * 2.8 * s)
	c.droop = 1.0 + 0.15 * exp(-6.0 * s) * sin(TAU * 2.5 * s)
	# One turn, fast at first and slowing to a stop; the wobble rides on it and dies with it.
	var turned := 1.0 - pow(1.0 - v, 2.4)
	c.yaw = TAU * turned
	c.tilt = TILT * smoothstep(0.0, 0.08, v) * pow(1.0 - v, 1.2)
	c.tilt_dir = WOBBLES * c.yaw + PI / 2.0
	return c

## Waking up, `at` seconds in.
static func wake_at(at: float) -> Dictionary:
	var c := asleep()
	if at < SHIVER_S:
		c.shiver = sin(PI * at / SHIVER_S)
		c.lids = 1.0 - 0.35 * Easing.in_out((at - 0.2) / 0.15)
		c.droop = 1.0 - 0.12 * Easing.in_out((at - 0.12) / 0.23)
		return c
	if at < WAKE_HOP_AT:
		var k := Easing.in_out((at - SHIVER_S) / (WAKE_HOP_AT - SHIVER_S))
		c.lift = DROP - 0.015 * k
		c.squash = 0.18 * k
		c.droop = 0.88
		c.lids = 0.65
		return c
	if at < WAKE_LAND_AT:
		var u := (at - WAKE_HOP_AT) / (WAKE_LAND_AT - WAKE_HOP_AT)
		var y0 := DROP - 0.015
		c.lift = y0 - y0 * u + 4.0 * 0.2 * u * (1.0 - u)
		if u < 0.1:
			c.squash = lerpf(0.18, -0.14, Easing.out(u / 0.1))
		else:
			c.squash = -0.14 * (1.0 - Easing.in_out((u - 0.1) / 0.35)) - 0.05 * Easing.in_out((u - 0.65) / 0.35)
		# Everything pops out a little past its place and settles back (back_out, eased in so the
		# eyes swing up rather than teleport).
		c.tuck = 1.0 - Easing.back_out(smoothstep(0.0, 1.0, (u - 0.05) / 0.45), 2.4)
		c.droop = 0.88 * (1.0 - Easing.back_out(smoothstep(0.0, 1.0, u / 0.9)))
		c.lids = 0.65 * (1.0 - Easing.in_out(u / 0.35))
		c.yaw = WAKE_TWIST * Easing.there_and_back(u)
		return c
	var s := at - WAKE_LAND_AT
	var hit := Easing.out(s / 0.05)
	c.lift = 0.03 * sin(PI * clampf((s - 0.08) / 0.22, 0.0, 1.0))
	c.squash = -0.05 * (1.0 - hit) + 0.22 * hit * exp(-9.0 * s) * cos(TAU * 3.0 * s)
	c.tuck = 0.0
	c.droop = 0.0
	c.lids = 0.0
	c.w = 1.0 - Easing.in_out((s - 0.18) / (WAKE_S - WAKE_LAND_AT - 0.18))
	return c

## Reduced motion: she folds and settles where she is.
static func calm_enter_at(at: float) -> Dictionary:
	var c := rest()
	c.w = Easing.out(at / 0.2)
	c.lift = DROP * Easing.in_out(at / CALM_ENTER_S)
	c.tuck = Easing.in_out(at / 1.0)
	c.droop = Easing.in_out((at - 0.3) / 1.0)
	c.lids = Easing.in_out(at / 1.2)
	return c

static func calm_wake_at(at: float) -> Dictionary:
	var c := rest()
	c.w = 1.0 - Easing.in_out((at - 0.9) / (CALM_WAKE_S - 0.9))
	c.lift = DROP * (1.0 - Easing.in_out(at / 0.9))
	c.tuck = 1.0 - Easing.in_out((at - 0.1) / 0.7)
	c.droop = 1.0 - Easing.in_out(at / 0.8)
	c.lids = 1.0 - Easing.in_out(at / 0.6)
	return c

func timeline(at: float) -> Dictionary:
	match sequence:
		"enter":
			return calm_enter_at(at) if reduced else enter_at(at)
		"wake":
			return calm_wake_at(at) if reduced else wake_at(at)
	return rest()

# --- starting and running them ------------------------------------------------------------

## Going to sleep. From awake it starts with the crouch; from part way up (a nap again while she
## wakes) with the legs still mostly in, she drops back and settles from the landing.
func start_enter(calm: bool) -> void:
	var tucked: float = channels.tuck
	begin("enter", calm, 0.0 if calm or tucked < 0.5 else LAND_AT)

## Waking. Asleep she shivers first; caught in the crouch she just stands back up; caught in the air
## or the turn she drops into the wake's crouch and hops up from there.
func start_wake(calm: bool) -> void:
	if sequence == "enter" and t < CROUCH_S and not calm:
		begin("rise", calm, 0.0)
	elif sequence == "enter" and not calm:
		begin("wake", calm, SHIVER_S)
	else:
		begin("wake", calm, 0.0)

func begin(name: String, calm: bool, at: float) -> void:
	from = channels.duplicate()
	unwind = wrapf(float(channels.yaw), -PI, PI)
	from.yaw = 0.0
	sequence = name
	reduced = calm
	t = at
	began_at = at
	match name:
		"enter":
			length = CALM_ENTER_S if calm else ENTER_S
		"wake":
			length = CALM_WAKE_S if calm else WAKE_S
		_:
			length = RISE_S

## The channels at the sequence's clock: blended in from where she was, the old turn unwinding.
## Returns true when the sequence has just finished.
func advance(delta: float, phase: String) -> bool:
	if sequence == "":
		channels = asleep() if phase == "sleeping" else (rest() if phase == "awake" else channels)
		return false
	t += delta
	var since := t - began_at
	var target := timeline(t)
	var blend := Easing.in_out(since / (RISE_S if sequence == "rise" else BLEND_S))
	var c := {}
	for key in KEYS:
		c[key] = lerpf(float(from[key]), float(target[key]), blend)
	c.tilt_dir = target.tilt_dir if target.tilt > 0.0 else from.tilt_dir
	c.yaw = target.yaw + unwind * (1.0 - Easing.in_out(since / UNWIND_S))
	channels = c
	if t >= length:
		var finished := sequence
		sequence = ""
		channels = asleep() if finished == "enter" else rest()
		return true
	return false

# --- the bones -----------------------------------------------------------------------------

func _process(_delta: float) -> void:
	apply()

func at_rest() -> bool:
	for key in KEYS:
		if absf(float(channels[key])) > 1e-5:
			return false
	return true

## The channels onto the bones, over what the clip and the layers wrote this frame.
func apply() -> void:
	var was_active := active
	active = not at_rest()
	if not active:
		if was_active:
			restore()
		return
	writes += 1
	var w: float = channels.w
	var lift: float = channels.lift
	var squash: float = channels.squash
	var tuck: float = channels.tuck
	var droop: float = channels.droop
	# The body: lifted or lowered, squashed about her belly's lowest point, shivering.
	var sy := 1.0 - squash
	var sxz := 1.0 + 0.5 * squash
	var parent := skeleton.get_bone_parent(bones.body)
	var parent_rest := skeleton.get_bone_global_rest(parent).basis.orthonormalized() if parent >= 0 else Basis()
	var rest_local := skeleton.get_bone_rest(bones.body)
	var offset := parent_rest.inverse() * Vector3(0.0, lift - BELLY_BELOW * (1.0 - sy), 0.0)
	var shake: float = channels.shiver * deg_to_rad(4.0) * sin(TAU * 15.0 * t)
	var rotation := rest_local.basis.get_rotation_quaternion() * Quaternion(body_rest.basis.orthonormalized().inverse() * Vector3.BACK, shake)
	var body: Array = base(bones.body)
	put(bones.body, (body[0] as Vector3).lerp(rest_local.origin + offset, w), (body[1] as Quaternion).slerp(rotation, w),
		(body[2] as Vector3).lerp(Vector3(sxz, sy, sxz), w))
	# The whole of her: the turn and the coin tilt, rocking on her belly so its lowest point stays down.
	var yaw: float = channels.yaw
	var tilt: float = channels.tilt
	var pivot := BELLY_CENTER + Vector3(0.0, lift, 0.0)
	var raise := 0.0
	var turn := Basis(Vector3.UP, yaw)
	if absf(tilt) > 1e-6:
		var dir: float = channels.tilt_dir
		var low := Vector3(cos(dir), 0.0, sin(dir))
		var along := dir - yaw   # the low side in her own frame
		var reach := 1.0 / sqrt(pow(cos(along) / BELLY.x, 2.0) + pow(sin(along) / BELLY.z, 2.0))
		raise = sqrt(pow(reach * sin(tilt), 2.0) + pow(BELLY.y * cos(tilt), 2.0)) - BELLY.y
		turn = Basis(Vector3.UP.cross(low).normalized(), tilt) * turn
	var whole := Transform3D(turn, pivot + Vector3(0.0, raise, 0.0)) * Transform3D(Basis(), -pivot)
	var root_pose := whole * skeleton.get_bone_rest(bones.root)
	put(bones.root, root_pose.origin, root_pose.basis.get_rotation_quaternion(), base(bones.root)[2])
	# The eye stalks bend outward at the base, over the rim, and ride the shell's breath.
	var breath := shell.get_blend_shape_value(squash_index) if squash_index >= 0 else 0.0
	for side in ["L", "R"]:
		var s := 1.0 if side == "L" else -1.0
		var bend := Basis(Vector3.UP, s * DROOP_FORWARD * droop) * Basis(Vector3.BACK, -s * DROOP_ANGLE * droop)
		var shift := Vector3(s * DROOP_SHIFT.x, DROOP_SHIFT.y, DROOP_SHIFT.z) * clampf(droop, 0.0, 1.2)
		shift.y -= BREATH_FOLLOW * breath * clampf(droop, 0.0, 1.0)
		layer_bone(bones["eyestalk_" + side], bend, shift, 1.0)
	# The claws swing down and in and shrink under the shell; past 0 (waking) they pop a little big.
	var fold := Easing.out(clampf(tuck, 0.0, 1.0) * 1.3)
	var shrink := tuck if tuck < 0.0 else pow(tuck, 1.3)
	for side in ["L", "R"]:
		var s := 1.0 if side == "L" else -1.0
		layer_bone(bones["claw_arm_" + side], Basis(Vector3.BACK, -s * CLAW_ANGLE * fold), Vector3.ZERO,
			1.0 - (1.0 - CLAW_MIN) * shrink)
	# The legs fold tight against her and shrink in under the rim.
	if legs != null and not leg_fold.is_empty():
		for leg: String in Legs.LEGS:
			var folded: Array = leg_fold[leg]
			var upper: Array = base(legs.upper[leg])
			var lower: Array = base(legs.lower[leg])
			put(legs.upper[leg], upper[0], (upper[1] as Quaternion).slerp(folded[0], fold), upper[2] * (1.0 - (1.0 - LEG_MIN) * shrink))
			put(legs.lower[leg], lower[0], (lower[1] as Quaternion).slerp(folded[1], fold), lower[2])

## Turn a child of the body by `bend` and move it by `shift`, both given in skeleton space at rest,
## about its own head, and scale it by `size`, over the pose the clip gave it.
func layer_bone(bone: int, bend: Basis, shift: Vector3, size: float) -> void:
	var to_parent := body_rest.basis.orthonormalized().inverse()
	var local_bend := (to_parent * bend * body_rest.basis.orthonormalized()).get_rotation_quaternion()
	var pose: Array = base(bone)
	put(bone, pose[0] + to_parent * shift, local_bend * (pose[1] as Quaternion), pose[2] * size)

## A bone's pose under this layer: what the clip and the layers before it wrote this frame. A part
## nothing has written since this layer did (a clip with no key for it, the clip paused, a capture)
## is taken from before this layer, so the layer never stacks on itself.
func base(bone: int) -> Array:
	var now := [skeleton.get_bone_pose_position(bone), skeleton.get_bone_pose_rotation(bone), skeleton.get_bone_pose_scale(bone)]
	if written.has(bone):
		var mine: Array = written[bone]
		var before: Array = bases[bone]
		for k in 3:
			if now[k].is_equal_approx(mine[k]):
				now[k] = before[k]
	bases[bone] = now
	return now

func put(bone: int, position: Vector3, rotation: Quaternion, size: Vector3) -> void:
	skeleton.set_bone_pose_position(bone, position)
	skeleton.set_bone_pose_rotation(bone, rotation)
	skeleton.set_bone_pose_scale(bone, size)
	written[bone] = [position, rotation, size]

## Done: the bones get back what was under this layer (the clip writes them again in any case).
func restore() -> void:
	for bone: int in written:
		var pose: Array = base(bone)
		skeleton.set_bone_pose_position(bone, pose[0])
		skeleton.set_bone_pose_rotation(bone, pose[1])
		skeleton.set_bone_pose_scale(bone, pose[2])
	written.clear()
	bases.clear()
