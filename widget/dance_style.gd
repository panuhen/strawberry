extends Node
## Dance styles driven by the beat (WIRING.md §4c). The daemon forwards the beat watcher's
## estimate as {"tempo": {...}}; while she is dancing this node picks a style from it, runs the
## dance clip at the music's tempo, and layers beat-locked moves on top of the clip the same
## way the reaction recipes do (bone offsets after the AnimationPlayer, morphs via the eyes).
##
##   rave      techno/house/trance: even four-on-the-floor kick, 118+ BPM. Stomp + alternating arms,
##             a side's front legs stomping on each beat.
##   headbang  rock/metal: fast, dense, uneven. Forward nod on the beat, claws up, squint, both
##             front legs stomping together on every beat.
##   groove    hip hop/funk: 80–108 BPM with low end. Slow roll, claw pumps every other beat, a
##             front leg tapping on alternate beats.
##   bounce    pop and the rest with a beat. Squash on the beat, a front leg tapping it.
## A tap or stomp lifts the toe between beats and brings it down on the next one (legs.gd beat_lift).
##   sway      slow, quiet, beatless, or a tempo the tracker is not steady on yet. Gentle roll,
##             no lock, clip slowed.
##
## Everything is computed from the next beat's wall-clock time, so the moves land on the kick
## regardless of frame rate. Without a fresh estimate (6 s) she dances the plain clip.

const LIFT_BPM := 119.0          # dance_loop: 4.033 s, 8 leg lifts -> one lift per 0.504 s
const FRESH_S := 6.0
const CONFIRM_MESSAGES := 2      # a new style has to win this many estimates in a row
const CONFIRM_SWAY := 4          # dropping to sway takes longer: breakdowns are 4–8 s and the beat comes back
const HEARTBEAT_S := 2.0         # the watcher's regular post; extra posts (a section change) come sooner, so a
                                 # new style must also have held for (votes - 1) heartbeats, as before them
const MIN_SPEED := 0.65
const MAX_SPEED := 1.6
const FADE_S := 0.3              # the moves ease in when she starts and out when the beat goes
const SWITCH_S := 0.5            # a new style crossfades from the one before over this long
const Easing = preload("res://easing.gd")
const Legs = preload("res://legs.gd")

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
var candidate_since := 0.0
var applied := false
var styles_seen := {}
var wrote_speed := false
var presence := 0.0              # 0..1, how much of the moves she is doing (FADE_S)
var previous_style := ""
var since_switch := SWITCH_S
var last_moves := {}
var yaw := 0.0                   # the styles' turn on the beat, read by turn.gd
var legs: Node                   # legs.gd: the taps and stomps go to it as toe lifts

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

## `at`: when the estimate arrived (now by default); a capture or a check passes a later time to stand
## for the next heartbeat.
func set_tempo(data: Dictionary, at: float = -1.0) -> void:
	received_at = at if at >= 0.0 else Time.get_unix_time_from_system()
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
		candidate_since = received_at
	var needed := CONFIRM_SWAY if candidate == "sway" and style != "" else CONFIRM_MESSAGES
	var held := received_at - candidate_since >= (needed - 1) * HEARTBEAT_S - 0.25
	if candidate_votes >= needed and held and candidate != style:
		previous_style = style
		since_switch = 0.0
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
	# Loudness here is the player's stream before the volume knob, so it says how dense the
	# mix is, not how loud the room is; only near-silence should force a sway.
	if conf < 0.3 or bpm < 76.0 or loud < -48.0:
		return "sway"
	# The tracker says whether the tempo has held for a few estimates. Until it has (the first
	# seconds of a song, a tempo change, a beat it keeps changing its mind about), sway rather
	# than stomp on beats that may be wrong. A watcher that predates the flag sends none.
	if not bool(t.get("steady", true)):
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

func _process(delta: float) -> void:
	if not active():
		if not applied:
			return
		# The beat is gone or she stopped dancing: the clip's speed returns at once, the moves ease out.
		if wrote_speed:
			player.speed_scale = 1.0
			wrote_speed = false
		presence = maxf(0.0, presence - delta / FADE_S)
		if presence <= 0.0:
			reset()
		else:
			layer(last_moves, Easing.in_out(presence))
		return
	presence = minf(1.0, presence + delta / FADE_S)
	since_switch += delta
	var now := Time.get_unix_time_from_system()
	var m := moves(style, now)
	if previous_style != "" and since_switch < SWITCH_S:
		m = mix(moves(previous_style, now), m, Easing.in_out(since_switch / SWITCH_S))
	player.speed_scale = m.speed
	wrote_speed = true
	last_moves = m
	layer(m, Easing.in_out(presence))
	applied = true

## One style's moves at this moment, at full strength.
func moves(name: String, now: float) -> Dictionary:
	var phase := beat_phase(now)
	var parity := posmod(beat_index(now), 2)   # 0 or 1 (the index is negative before next_beat)
	var m := {"pitch": 0.0, "roll": 0.0, "yaw": 0.0, "lift_l": 0.0, "lift_r": 0.0, "squash": 0.0,
		"wide": 0.0, "happy": 0.0, "squint": 0.0, "speed": clip_speed(),
		"tap_l": 0.0, "tap_r": 0.0, "stomp_l": 0.0, "stomp_r": 0.0}
	var down := Legs.beat_lift(phase)   # 0 on the beat, up between beats
	match name:
		"rave":
			m.squash = 0.3 * pulse(phase, 7.0)
			var swing := 0.5 - 0.5 * cos(TAU * phase)  # 0 on the beat, 1 between beats
			m.lift_l = 1.0 * (swing if parity == 0 else 1.0 - swing) * 0.85 + 0.25
			m.lift_r = 1.0 * (swing if parity == 1 else 1.0 - swing) * 0.85 + 0.25
			# A quick lean onto the beat's side that eases back by the next one (was a sawtooth).
			var lean := Easing.out(phase / 0.12) * (1.0 - Easing.in_out((phase - 0.12) / 0.88))
			m.roll = deg_to_rad(3.0) * (1.0 if parity == 0 else -1.0) * lean
			m.yaw = deg_to_rad(5.0) * (1.0 if parity == 0 else -1.0) * lean
			m.wide = 0.6
			# Hard: front and middle legs of a side together, the sides taking turns.
			m.stomp_l = 0.036 * down if parity == 0 else 0.0
			m.stomp_r = 0.036 * down if parity != 0 else 0.0
		"headbang":
			m.pitch = -deg_to_rad(16.0) * pulse(phase, 5.0)
			m.lift_l = 0.5
			m.lift_r = 0.5
			m.squint = 0.35
			# Hard: both front legs stomp with the nod, every beat.
			m.tap_l = 0.04 * down
			m.tap_r = 0.04 * down
		"groove":
			var bar := fposmod((now - float(tempo.get("next_beat", now))) / (2.0 * float(tempo.get("period_s", 0.5))), 1.0)
			m.roll = deg_to_rad(5.0) * sin(TAU * bar)
			m.yaw = deg_to_rad(7.0) * sin(TAU * bar + PI / 2.0)   # turns at the ends of the roll
			m.squash = 0.18 * pulse(phase, 5.0) * (1.0 if parity == 0 else 0.5)
			m.lift_l = 0.35 * pulse(phase, 4.0) if parity == 0 else 0.0
			m.lift_r = 0.35 * pulse(phase, 4.0) if parity == 1 else 0.0
			m.tap_l = 0.018 * down if parity == 0 else 0.0
			m.tap_r = 0.018 * down if parity != 0 else 0.0
		"bounce":
			m.squash = 0.32 * pulse(phase, 6.0)
			m.pitch = -deg_to_rad(4.0) * pulse(phase, 6.0)
			var hop := Easing.out(phase / 0.15) * (1.0 - Easing.in_out((phase - 0.15) / 0.85))
			m.yaw = deg_to_rad(3.0) * (1.0 if parity == 0 else -1.0) * hop
			m.tap_l = 0.022 * down if parity == 0 else 0.0
			m.tap_r = 0.022 * down if parity != 0 else 0.0
		"sway":
			m.speed = 0.75
			m.roll = deg_to_rad(4.0) * sin(TAU * now / 3.2)
			m.yaw = deg_to_rad(6.0) * sin(TAU * now / 6.4)
			m.happy = 0.4
	return m

static func mix(a: Dictionary, b: Dictionary, weight: float) -> Dictionary:
	var out := {}
	for key in b:
		out[key] = lerpf(float(a[key]), float(b[key]), weight)
	return out

## Puts the moves on top of the clip's pose, scaled by `weight` (the clip's speed is the caller's).
func layer(m: Dictionary, weight: float) -> void:
	var pitch: float = m.pitch * weight
	yaw = m.yaw * weight
	var roll: float = m.roll * weight
	if pitch != 0.0 or roll != 0.0:
		var q := Quaternion(Vector3.RIGHT, pitch) * Quaternion(Vector3.BACK, roll)
		skeleton.set_bone_pose_rotation(body_i, skeleton.get_bone_pose_rotation(body_i) * q)
	if m.lift_l != 0.0:
		skeleton.set_bone_pose_rotation(claw_l_i, skeleton.get_bone_pose_rotation(claw_l_i) * Quaternion(Vector3.RIGHT, m.lift_l * weight))
	if m.lift_r != 0.0:
		skeleton.set_bone_pose_rotation(claw_r_i, skeleton.get_bone_pose_rotation(claw_r_i) * Quaternion(Vector3.RIGHT, m.lift_r * weight))
	# The style's squash stands in for the clip's breath; while easing in or out the two are mixed.
	for i in squashers.size():
		var clip_value := squashers[i].get_blend_shape_value(squash_indices[i])
		squashers[i].set_blend_shape_value(squash_indices[i], lerpf(clip_value, m.squash, weight))
	blink.set_layer("dance", m.wide * weight, m.happy * weight, m.squint * weight)
	if legs:
		legs.add("L1", Vector3(0.0, m.tap_l * weight, 0.0))
		legs.add("R1", Vector3(0.0, m.tap_r * weight, 0.0))
		legs.add_side(1.0, Vector3(0.0, m.stomp_l * weight, 0.0), [1, 2])
		legs.add_side(-1.0, Vector3(0.0, m.stomp_r * weight, 0.0), [1, 2])

func reset() -> void:
	applied = false
	presence = 0.0
	yaw = 0.0
	last_moves = {}
	if wrote_speed:
		player.speed_scale = 1.0
		wrote_speed = false
	blink.set_layer("dance", 0.0, 0.0, 0.0)
	for i in squashers.size():
		squashers[i].set_blend_shape_value(squash_indices[i], 0.0)
