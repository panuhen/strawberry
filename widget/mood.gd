extends Node
## Her living mood (WIRING.md §29, PROTOCOL Part 1e). The brain sends {"type": "mood", "energy": e,
## "feeling": f}, each -1..1 (energy: calm … lively; feeling: grumpy … pleased); this node eases toward it
## and shapes what she already does, each within a fixed bound:
##
##   idle      the idle clip's sway bigger when lively, smaller when calm (SWAY: its body turn scaled)
##   dance     the dance styles' moves scaled the same way (DANCE; dance_style.gd reads dance_scale())
##   posture   a lean toward the viewer and standing taller when lively, leaning back and sinking a little
##             when calm (PITCH_DEG, LIFT); pleased lifts the claws a touch (CLAW_DEG), grumpy sinks her
##   eyes      lively: wider; calm: heavier lids; grumpy: a squint; pleased: a soft happy look (the blink
##             controller's "mood" layer, combined by max with every other layer); lively blinks more often (BLINK)
##   gaze      lively follows the cursor faster and keeps looking longer; calm follows slower and looks
##             ahead again sooner (GAZE, gaze.gd `liveliness`)
##
## None of it while she sleeps, goes down or gets up: the weight eases to 0 there (WEIGHT_S) and the sleep's own
## clip and lids are left alone. Without a mood from the brain (an older daemon, the mood off) everything is
## neutral, exactly as before. The dials follow the brain's with a critically damped spring (SPRING) and a top
## speed (MAX_RATE), so a jump in the mood is never a jump in her pose.
##
## Runs at process priority 148: after the AnimationPlayer has written the clip, before the reactions (150),
## the dance styles (155), touch (157) and the legs (159), which layer their own moves on top.

const Easing = preload("res://easing.gd")
const SWAY := 0.35              # the idle clip's sway scaled 0.65 … 1.35
const DANCE := 0.25             # the dance moves scaled 0.75 … 1.25
const PITCH_DEG := 3.0          # lively leans toward the viewer this far, calm leans back as far
const LIFT := 0.008             # lively stands this much taller, calm sinks as much (world units)
const SULK_LIFT := 0.005        # grumpy sinks this much more
const CLAW_DEG := 7.0           # pleased lifts both claws this far
const WIDE := 0.25              # eye morphs at most: wide (lively), lids (calm), squint (grumpy), happy (pleased)
const LIDS := 0.22
const SQUINT := 0.35
const HAPPY := 0.25
const BLINK := 0.3              # blink pauses scaled 0.7 (lively) … 1.3 (calm)
const GAZE := 1.0               # gaze.gd's liveliness at most (its own bounds: follow ±30 %, look ±50 % as long)
const SPRING := 2.2             # rad/s: the dials settle in about two seconds
const MAX_RATE := 0.6           # and never move faster than this a second
const WEIGHT_S := 0.8           # the mood fades out over this long as she goes to sleep, and back in as she wakes

var widget: Node3D
var skeleton: Skeleton3D
var blink: Node
var body_i := -1
var claw_l_i := -1
var claw_r_i := -1
var body_rest := Quaternion.IDENTITY
var base_min_pause := 5.0
var base_max_pause := 10.0

var target := Vector2.ZERO       # (energy, feeling) as the brain last sent it
var current := Vector2.ZERO      # where she is on the way there
var velocity := Vector2.ZERO
var weight := 0.0                # 0 asleep … 1 awake (eased)
var weight_linear := 0.0
var received := 0                # mood messages taken (the checks)
var outputs := {}                # what this frame asked for (the checks)

func setup(owner: Node3D, model: Node, blink_controller: Node) -> void:
	process_priority = 148
	widget = owner
	blink = blink_controller
	skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	body_i = skeleton.find_bone("body")
	claw_l_i = skeleton.find_bone("claw_arm_L")
	claw_r_i = skeleton.find_bone("claw_arm_R")
	body_rest = skeleton.get_bone_rest(body_i).basis.get_rotation_quaternion()
	base_min_pause = blink.minimum_pause
	base_max_pause = blink.maximum_pause

## The brain's `mood` message. Numbers out of range are clamped; anything else is ignored.
func on_mood(data: Dictionary) -> bool:
	var energy: Variant = data.get("energy")
	var feeling: Variant = data.get("feeling")
	if not (energy is float or energy is int) or not (feeling is float or feeling is int):
		return false
	if is_nan(float(energy)) or is_nan(float(feeling)):
		return false
	target = Vector2(clampf(float(energy), -1.0, 1.0), clampf(float(feeling), -1.0, 1.0))
	received += 1
	return true

## Awake and in her usual poses: the mood shows. Asleep, going down or getting up: it does not.
func awake() -> bool:
	return widget.sleeper == null or widget.sleeper.phase == "awake"

## The dance styles' moves are scaled by this (dance_style.gd).
func dance_scale() -> float:
	return 1.0 + DANCE * current.x * weight

func _process(delta: float) -> void:
	step(delta)
	apply()

## The dials and the weight, `delta` seconds on.
func step(delta: float) -> void:
	if delta <= 0.0:
		return
	# A critically damped spring toward the target, its speed capped: smooth, and no overshoot.
	var accel := (target - current) * SPRING * SPRING - velocity * 2.0 * SPRING
	velocity += accel * delta
	if velocity.length() > MAX_RATE:
		velocity = velocity.normalized() * MAX_RATE
	current += velocity * delta
	current = current.clamp(Vector2(-1.0, -1.0), Vector2(1.0, 1.0))
	weight_linear = move_toward(weight_linear, 1.0 if awake() else 0.0, delta / WEIGHT_S)
	weight = Easing.in_out(weight_linear)

## This frame's offsets on top of the clip's pose, the eyes' layer, the blink pauses and the gaze.
func apply() -> void:
	var e := current.x * weight
	var f := current.y * weight
	var lively := maxf(e, 0.0)
	var calm := maxf(-e, 0.0)
	var pleased := maxf(f, 0.0)
	var grumpy := maxf(-f, 0.0)
	outputs = {"sway": 1.0, "pitch": 0.0, "lift": 0.0, "claws": 0.0, "wide": WIDE * lively, "lids": LIDS * calm,
		"squint": SQUINT * grumpy, "happy": HAPPY * pleased, "blink": 1.0 - BLINK * e, "gaze": GAZE * e,
		"dance": dance_scale()}
	blink.minimum_pause = base_min_pause * outputs.blink
	blink.maximum_pause = base_max_pause * outputs.blink
	blink.set_layer("mood", outputs.wide, outputs.happy, outputs.squint, outputs.lids)
	if widget.gaze:
		widget.gaze.liveliness = outputs.gaze
	if weight <= 0.0 or skeleton == null:
		return
	# The idle clip's sway, bigger or smaller: its body turn from rest, scaled.
	if widget.state == "idle" and widget.one_shot == "" and widget.player.assigned_animation == "idle_loop" \
			and absf(e) > 0.001:
		outputs.sway = 1.0 + SWAY * e
		var turned: Quaternion = (body_rest.inverse() * skeleton.get_bone_pose_rotation(body_i)).normalized()
		if turned.w < 0.0:
			turned = -turned    # the short way round
		var axis := Vector3(turned.x, turned.y, turned.z)
		if axis.length() > 1e-6:
			var angle := 2.0 * atan2(axis.length(), turned.w)
			turned = Quaternion(axis.normalized(), angle * outputs.sway)
			skeleton.set_bone_pose_rotation(body_i, body_rest * turned)
	outputs.pitch = deg_to_rad(-PITCH_DEG) * e
	outputs.claws = deg_to_rad(CLAW_DEG) * pleased
	outputs.lift = LIFT * e - SULK_LIFT * grumpy
	if outputs.pitch != 0.0:
		skeleton.set_bone_pose_rotation(body_i, skeleton.get_bone_pose_rotation(body_i) * Quaternion(Vector3.RIGHT, outputs.pitch))
	if outputs.claws != 0.0:
		for bone in [claw_l_i, claw_r_i]:
			skeleton.set_bone_pose_rotation(bone, skeleton.get_bone_pose_rotation(bone) * Quaternion(Vector3.RIGHT, outputs.claws))
	if widget.legs and outputs.lift != 0.0:
		widget.legs.lift(outputs.lift)
