extends Node
## The pincers (WIRING.md §9, §13). Each lower claw hinges on its own bone, `pincer_L` / `pincer_R`,
## at the hinge the old `claw_open` keys turned about: 0 is shut, 1 is the old key's full 48°.
##
## Everything that opens a claw asks this node for an amount each frame (`request`): her voice
## (speech_player.gd), the wave (reactions.gd), a pinch back (touch.gd) and the gestures below
## (thinking, dancing, the alert snap). The largest request wins, and a spring takes the pincer
## there: it snaps open a touch past the mark and settles, and shutting onto the upper claw it
## clacks and bounces back a little. It runs after them all (process priority 162, after her
## voice at 160) and writes the bone every frame; no clip animates the pincers.
@export var minimum_think_pause := 5.0
@export var maximum_think_pause := 10.0
const OPEN_ANGLE := deg_to_rad(48.0)
const STIFFNESS := 42.0          # rad/s: the spring's natural frequency
const DAMPING := 0.55            # < 1: a little overshoot (about 12 %) before it settles
const CLACK_BOUNCE := 0.3        # shutting: this much of the closing speed comes back as a bounce
const MAX_OPEN := 1.12           # how far past full open an overshoot may go
const HINGE := Vector3(0, 0, -1) # skeleton space: Blender's +Y, the front, through the hinge

var held_open := false
var player: AnimationPlayer
var skeleton: Skeleton3D
var bones: Array[int] = []
var rest_rotations: Array[Quaternion] = []
var axes: Array[Vector3] = []    # the hinge in each pincer bone's own frame
var signs := [1.0, -1.0]         # left (+X) opens about +HINGE, right about -HINGE
var requests := Vector2.ZERO     # this frame's largest request per side, consumed by apply()
var openness := Vector2.ZERO     # where each pincer is (0 shut, 1 open)
var velocity := Vector2.ZERO
var target := Vector2.ZERO       # what the spring pulled toward this frame (checks read it)
var clacks := 0
var rng := RandomNumberGenerator.new()
var mode := ""
var remaining := 0.0
var elapsed := -1.0
var gesture_duration := 1.2
var gesture_amount := 0.65
var side := 0
var gesture_count := 0

func setup(model: Node, animation_player: AnimationPlayer) -> void:
	process_priority = 162
	player = animation_player
	rng.randomize()
	side = rng.randi_range(0, 1)
	skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	for suffix in ["L", "R"]:
		var bone := skeleton.find_bone("pincer_" + suffix)
		bones.append(bone)
		rest_rotations.append(skeleton.get_bone_rest(bone).basis.get_rotation_quaternion())
		axes.append((skeleton.get_bone_global_rest(bone).basis.inverse() * HINGE).normalized())

## Open a pincer (0 left, 1 right) by `amount` this frame. The largest request of the frame wins.
func request(index: int, amount: float) -> void:
	requests[index] = maxf(requests[index], amount)

func request_both(amounts: Vector2) -> void:
	request(0, amounts.x)
	request(1, amounts.y)

func _process(delta: float) -> void:
	advance_gestures(delta)
	apply(delta)

## The gestures this node makes itself, by clip: they become requests like anyone else's.
func advance_gestures(delta: float) -> void:
	var clip := player.assigned_animation
	if clip != mode:
		mode = clip
		elapsed = -1.0
		remaining = rng.randf_range(minimum_think_pause, maximum_think_pause)
	var wanted := Vector2.ZERO
	if held_open:
		wanted = Vector2.ONE
	elif mode == "alert_snap":
		var t := player.current_animation_position / player.get_animation(mode).length
		wanted = Vector2.ONE * 0.35 * smoothstep(0.0, 0.08, t) * (1.0 - smoothstep(0.66, 1.0, t))
	elif mode == "dance_loop":
		# Same phase as the alternating leg lifts: four cycles per dance loop.
		var length := player.get_animation(mode).length
		var wave := sin(TAU * 4.0 * player.current_animation_position / length)
		wanted = Vector2(maxf(0.0, wave), maxf(0.0, -wave)) * 0.85
	elif mode == "think_loop":
		if elapsed < 0.0:
			remaining -= delta
			if remaining <= 0.0:
				elapsed = 0.0
				side = 1 - side
				gesture_duration = rng.randf_range(1.0, 1.4)
				gesture_amount = rng.randf_range(0.55, 0.8)
				gesture_count += 1
		if elapsed >= 0.0:
			elapsed += delta
			var t := elapsed / gesture_duration
			var amount := smoothstep(0.0, 0.3, t) * (1.0 - smoothstep(0.5, 1.0, t))
			wanted[side] = amount * gesture_amount
			if t >= 1.0:
				elapsed = -1.0
				remaining = rng.randf_range(minimum_think_pause, maximum_think_pause)
	request_both(wanted)

## The spring toward this frame's requests, then the bones. Substeps keep it stable at a low frame rate.
func apply(delta: float) -> void:
	target = requests.clamp(Vector2.ZERO, Vector2.ONE)
	requests = Vector2.ZERO
	var steps := maxi(1, ceili(delta / 0.004))
	var h := delta / steps
	for i in 2:
		var x := openness[i]
		var v := velocity[i]
		for s in steps:
			v += (STIFFNESS * STIFFNESS * (target[i] - x) - 2.0 * DAMPING * STIFFNESS * v) * h
			x += v * h
			if x < 0.0:
				# Shut onto the upper claw: a clack, and a small bounce back.
				x = 0.0
				if v < -0.5:
					clacks += 1
				v = -v * CLACK_BOUNCE
			elif x > MAX_OPEN:
				x = MAX_OPEN
				v = 0.0
		openness[i] = x
		velocity[i] = v
		if absf(x) < 1e-4 and absf(v) < 1e-3 and target[i] == 0.0:
			openness[i] = 0.0
			velocity[i] = 0.0
		write(i)

func write(i: int) -> void:
	var q := rest_rotations[i] * Quaternion(axes[i], signs[i] * OPEN_ANGLE * openness[i])
	skeleton.set_bone_pose_rotation(bones[i], q)

## The left pincer's opening (0..1); the checks read this where they read claw_open before.
func value(i := 0) -> float:
	return openness[i]
