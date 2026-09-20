extends Node
## Dance styles driven by the beat (WIRING.md §4c). The daemon forwards the beat watcher's
## estimate as {"tempo": {...}}; while she is dancing this node picks a style from it, runs the
## dance clip at the music's tempo, and layers beat-locked moves on top of the clip the same
## way the reaction recipes do (bone offsets after the AnimationPlayer, morphs via the eyes).
##
##   rave      techno/house/trance: even four-on-the-floor kick, 118+ BPM. Stomp + alternating arms.
##   headbang  rock/metal: fast, dense, uneven. Forward nod on the beat, claws up, squint.
##   groove    hip hop/funk: 80–108 BPM with low end. Slow roll, claw pumps every other beat.
##   bounce    pop and the rest with a beat. Squash on the beat.
##   sway      slow, quiet or beatless. Gentle roll, no lock, clip slowed.
##
## Everything is computed from the next beat's wall-clock time, so the moves land on the kick
## regardless of frame rate. Without a fresh estimate (6 s) she dances the plain clip.

const LIFT_BPM := 119.0          # dance_loop: 4.033 s, 8 leg lifts -> one lift per 0.504 s
const FRESH_S := 6.0
const CONFIRM_MESSAGES := 2      # a new style has to win this many estimates in a row
const MIN_SPEED := 0.65
const MAX_SPEED := 1.6

var widget: Node3D
var player: AnimationPlayer
var skeleton: Skeleton3D
var blink: Node
var body_i := -1
var claw_l_i := -1
var claw_r_i := -1
var squashers: Array[MeshInstance3D] = []
var squash_indices: Array[int] = []

var tempo := {}
var received_at := 0.0
var style := ""
var candidate := ""
var candidate_votes := 0
var applied := false
var styles_seen := {}
var wrote_speed := false

func setup(owner: Node3D, animation_player: AnimationPlayer, model: Node, blink_controller: Node) -> void:
	process_priority = 155
	widget = owner
	player = animation_player
	blink = blink_controller
	skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	body_i = skeleton.find_bone("body")
	claw_l_i = skeleton.find_bone("claw_arm_L")
	claw_r_i = skeleton.find_bone("claw_arm_R")
	for node in model.find_children("*", "MeshInstance3D", true, false):
		var mesh := node as MeshInstance3D
		var index := mesh.find_blend_shape_by_name("squash")
		if index != -1:
			squashers.append(mesh)
			squash_indices.append(index)

func set_tempo(data: Dictionary) -> void:
	received_at = Time.get_unix_time_from_system()
	if data.get("silent", false):
		tempo = {}
		return
	tempo = data
	var pick := choose(data)
	if pick == candidate:
		candidate_votes += 1
	else:
		candidate = pick
		candidate_votes = 1
	if candidate_votes >= CONFIRM_MESSAGES and candidate != style:
		style = candidate
		styles_seen[style] = true
		print("dance style: ", style, " (%.0f bpm)" % float(data.get("bpm", 0.0)))

## The rules. Tunable thresholds; the beat watcher logs the same numbers for real songs.
static func choose(t: Dictionary) -> String:
	var bpm := float(t.get("bpm", 0.0))
	var conf := float(t.get("confidence", 0.0))
	var even := float(t.get("evenness", 0.0))
	var low := float(t.get("low_ratio", 0.0))
	var dens := float(t.get("density", 0.0))
	var loud := float(t.get("loudness_db", -60.0))
	if conf < 0.3 or bpm < 76.0 or loud < -38.0:
		return "sway"
	if bpm >= 118.0 and even >= 0.45 and low >= 0.25:
		return "rave"
	if bpm >= 132.0 and dens >= 4.0:
		return "headbang"
	if bpm <= 108.0 and low >= 0.2:
		return "groove"
	return "bounce"

func fresh() -> bool:
	return not tempo.is_empty() and Time.get_unix_time_from_system() - received_at < FRESH_S

func active() -> bool:
	return widget.state == "dancing" and player.assigned_animation == "dance_loop" and fresh() and style != ""

## Beat phase in [0, 1): 0 exactly on a beat.
func beat_phase(now: float) -> float:
	var period := float(tempo.get("period_s", 0.5))
	var next_beat := float(tempo.get("next_beat", now))
	return fposmod((now - next_beat) / period, 1.0)

## Beat count parity (0/1), so moves can alternate sides.
func beat_index(now: float) -> int:
	var period := float(tempo.get("period_s", 0.5))
	var next_beat := float(tempo.get("next_beat", now))
	return int(floor((now - next_beat) / period))

static func pulse(phase: float, sharpness: float) -> float:
	return exp(-phase * sharpness)

func clip_speed() -> float:
	var bpm := float(tempo.get("bpm", LIFT_BPM))
	var speed := bpm / LIFT_BPM
	while speed > MAX_SPEED:
		speed /= 2.0
	while speed < MIN_SPEED:
		speed *= 2.0
	return speed

func _process(_delta: float) -> void:
	if not active():
		if applied:
			reset()
		return
	var now := Time.get_unix_time_from_system()
	var phase := beat_phase(now)
	var parity := beat_index(now) % 2
	var pitch := 0.0
	var roll := 0.0
	var lift_l := 0.0
	var lift_r := 0.0
	var squash := 0.0
	var speed := clip_speed()
	match style:
		"rave":
			squash = 0.3 * pulse(phase, 7.0)
			var swing := 0.5 - 0.5 * cos(TAU * phase)  # 0 on the beat, 1 between beats
			lift_l = 1.0 * (swing if parity == 0 else 1.0 - swing) * 0.85 + 0.25
			lift_r = 1.0 * (swing if parity == 1 else 1.0 - swing) * 0.85 + 0.25
			roll = deg_to_rad(3.0) * (1.0 if parity == 0 else -1.0) * (1.0 - phase)
			blink.extra_wide = 0.6
		"headbang":
			pitch = -deg_to_rad(16.0) * pulse(phase, 5.0)
			lift_l = 0.5
			lift_r = 0.5
			blink.extra_squint = 0.35
		"groove":
			var bar := fposmod((now - float(tempo.get("next_beat", now))) / (2.0 * float(tempo.get("period_s", 0.5))), 1.0)
			roll = deg_to_rad(5.0) * sin(TAU * bar)
			squash = 0.18 * pulse(phase, 5.0) * (1.0 if parity == 0 else 0.5)
			lift_l = 0.35 * pulse(phase, 4.0) if parity == 0 else 0.0
			lift_r = 0.35 * pulse(phase, 4.0) if parity == 1 else 0.0
		"bounce":
			squash = 0.32 * pulse(phase, 6.0)
			pitch = -deg_to_rad(4.0) * pulse(phase, 6.0)
		"sway":
			speed = 0.75
			roll = deg_to_rad(4.0) * sin(TAU * now / 3.2)
			blink.extra_happy = 0.4
	player.speed_scale = speed
	wrote_speed = true
	if pitch != 0.0 or roll != 0.0:
		var q := Quaternion(Vector3.RIGHT, pitch) * Quaternion(Vector3.BACK, roll)
		skeleton.set_bone_pose_rotation(body_i, skeleton.get_bone_pose_rotation(body_i) * q)
	if lift_l != 0.0:
		skeleton.set_bone_pose_rotation(claw_l_i, skeleton.get_bone_pose_rotation(claw_l_i) * Quaternion(Vector3.RIGHT, lift_l))
	if lift_r != 0.0:
		skeleton.set_bone_pose_rotation(claw_r_i, skeleton.get_bone_pose_rotation(claw_r_i) * Quaternion(Vector3.RIGHT, lift_r))
	for i in squashers.size():
		squashers[i].set_blend_shape_value(squash_indices[i], squash)
	applied = true

func reset() -> void:
	applied = false
	if wrote_speed:
		player.speed_scale = 1.0
		wrote_speed = false
	blink.extra_wide = 0.0
	blink.extra_squint = 0.0
	blink.extra_happy = 0.0
	for i in squashers.size():
		squashers[i].set_blend_shape_value(squash_indices[i], 0.0)
