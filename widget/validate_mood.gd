extends SceneTree
## Her living mood (mood.gd, WIRING.md §29, PROTOCOL Part 1e): neutral without one; a bad message ignored and an
## out-of-range one clamped; at each corner of the two dials every output within its bound, the idle sway scaled by
## exactly its factor and the pose no further from the neutral one than the bounds allow; asleep none of it (the
## pose the same as with no mood at all); a jump in the mood never a jump in her (the dials' speed and acceleration
## capped, the eyes and body changing a little a frame); the dance moves scaled within their bound.
## Real widget, isolated settings and no daemon traffic.
##
## godot --headless --audio-driver Dummy --path widget --script res://validate_mood.gd
## Add `-- --capture-dir=/absolute/path` and run with a display (xvfb-run works) for preview frames.
class TestWidget:
	extends "res://widget.gd"
	func settings_path() -> String:
		return "user://mood_test_%s.cfg" % OS.get_process_id()
	func setup_ws() -> void:
		pass
	func setup_sleep() -> void:
		super.setup_sleep()
		sleeper.monitor_enabled = false
	func _input(_event: InputEvent) -> void:
		pass

const Mood = preload("res://mood.gd")
const DanceStyle = preload("res://dance_style.gd")
const FRAME := 1.0 / 60.0
const CORNERS := [Vector2(1, 1), Vector2(-1, -1), Vector2(1, -1), Vector2(-1, 1)]
const EPS := 1e-4

var failures: Array[String] = []
var report := {}
var widget: TestWidget
var skeleton: Skeleton3D
var mood: Node
var capture_dir := ""

func _initialize() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--capture-dir="):
			capture_dir = arg.trim_prefix("--capture-dir=")
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error(message)

## Frame by frame, in the order the tree runs them: the clip, the legs' snapshot, sleep, the lids, the mood, the
## legs' solve, the tuck. `each` (optional) is called after every frame.
func advance(seconds: float, each := Callable()) -> void:
	for i in roundi(seconds * 60.0):
		widget.player.advance(FRAME)
		widget.legs.snapshot()
		widget.sleeper.tick(FRAME)
		widget.blink_controller.advance_blink(FRAME)
		mood.step(FRAME)
		mood.apply()
		widget.legs._process(FRAME)
		widget.sleeper.tuck.apply()
		if each.is_valid():
			each.call()

func bone(name: String) -> int:
	return skeleton.find_bone(name)

func rotation(name: String) -> Quaternion:
	return skeleton.get_bone_pose_rotation(bone(name))

func morph(key: String) -> float:
	var mesh: MeshInstance3D = widget.model.find_child("mesh_eye_L", true, false)
	return mesh.get_blend_shape_value(mesh.find_blend_shape_by_name(key))

## The mood at `at` at once (no spring), fully shown.
func hold(at: Vector2) -> void:
	mood.target = at
	mood.current = at
	mood.velocity = Vector2.ZERO
	mood.weight_linear = 1.0 if mood.awake() else 0.0
	mood.weight = mood.weight_linear

## The idle clip from its start for `seconds`: each frame's body and claw rotations and the body's height.
func idle_trace(seconds: float) -> Array:
	widget.set_state("idle")
	widget.player.play("idle_loop", 0.0)
	widget.player.seek(0.0, true)
	var out := []
	advance(seconds, func() -> void:
		out.append([rotation("body"), rotation("claw_arm_L"), skeleton.get_bone_pose_position(bone("body")).y]))
	return out

## The angle of `q` from the body's rest.
func from_rest(q: Quaternion) -> float:
	return (mood.body_rest.inverse() * q).normalized().get_angle()

func within(outputs: Dictionary) -> bool:
	return absf(outputs.pitch) <= deg_to_rad(Mood.PITCH_DEG) + EPS and absf(outputs.lift) <= Mood.LIFT + Mood.SULK_LIFT + EPS \
		and outputs.claws >= 0.0 and outputs.claws <= deg_to_rad(Mood.CLAW_DEG) + EPS \
		and outputs.wide <= Mood.WIDE + EPS and outputs.lids <= Mood.LIDS + EPS and outputs.squint <= Mood.SQUINT + EPS \
		and outputs.happy <= Mood.HAPPY + EPS and absf(outputs.blink - 1.0) <= Mood.BLINK + EPS \
		and absf(outputs.gaze) <= Mood.GAZE + EPS and absf(outputs.dance - 1.0) <= Mood.DANCE + EPS \
		and absf(outputs.sway - 1.0) <= Mood.SWAY + EPS

func capture(name: String) -> void:
	if capture_dir == "":
		return
	await process_frame
	await process_frame
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(capture_dir.path_join(name + ".png"))

## Preview frames (with a display): the corners idle, then rave at both ends of energy.
func previews() -> void:
	if capture_dir == "":
		return
	widget.set_process(true)
	for named in [["neutral", Vector2.ZERO], ["lively_pleased", Vector2(1, 1)], ["lively_grumpy", Vector2(1, -1)],
			["calm_pleased", Vector2(-1, 1)], ["calm_grumpy", Vector2(-1, -1)]]:
		widget.set_state("idle")
		hold(named[1])
		await create_timer(1.3).timeout
		await capture("idle_" + named[0])
	for named in [["calm", Vector2(-1, 0)], ["neutral", Vector2.ZERO], ["lively", Vector2(1, 0)]]:
		hold(named[1])
		widget.perform({"state": "dancing"})
		var now := Time.get_unix_time_from_system()
		var t := {"bpm": 130.0, "period_s": 60.0 / 130.0, "confidence": 0.8, "next_beat": now + 1.9, "evenness": 0.8,
			"low_ratio": 0.7, "density": 3.0, "loudness_db": -16.0}
		widget.dance.set_tempo(t)
		widget.dance.set_tempo(t, now + widget.dance.HEARTBEAT_S)
		await create_timer(2.0).timeout
		await capture("rave_" + named[0])
		widget.perform({"state": "idle"})
		await create_timer(0.5).timeout

func run() -> void:
	widget = TestWidget.new()
	widget.look_at = Vector2(190, 340)
	widget.place_at = Vector2.ZERO
	root.add_child(widget)
	await process_frame
	skeleton = widget.model.find_children("*", "Skeleton3D", true, false)[0]
	mood = widget.mood
	# The manual frames below are the only ones that move her; previews() lets the tree run again.
	for node: Node in [widget.mood, widget.legs, widget.reactions, widget.dance, widget.touch, widget.turn, widget.gaze,
			widget.sleeper, widget.blink_controller, widget.wander]:
		node.set_process(false)
	widget.player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_MANUAL

	# 1. No mood from the brain: neutral, as before.
	advance(1.0)
	check(mood.received == 0 and mood.current == Vector2.ZERO, "no mood yet should be neutral")
	check(not widget.blink_controller.layers.has("mood"), "no mood yet: no eye layer")
	check(mood.outputs.sway == 1.0 and mood.outputs.pitch == 0.0 and mood.outputs.lift == 0.0 and mood.outputs.dance == 1.0,
		"no mood yet: nothing changes (%s)" % mood.outputs)
	var neutral := idle_trace(4.0)

	# 2. A bad message is ignored, an out-of-range one clamped.
	check(not mood.on_mood({"type": "mood", "energy": "lots", "feeling": 0.2}), "a word for energy should be ignored")
	check(not mood.on_mood({"type": "mood", "feeling": 0.2}), "a missing dial should be ignored")
	check(mood.on_mood({"type": "mood", "energy": 5, "feeling": -9.5}) and mood.target == Vector2(1, -1), "out of range clamps")
	widget.on_typed({"type": "mood", "energy": 0.0, "feeling": 0.0})
	check(mood.target == Vector2.ZERO and mood.received == 2, "the widget hands `mood` to mood.gd")

	# 3. Each corner: the outputs within bounds; the idle sway scaled by its factor; the pose near the neutral one.
	var sways := {}
	for corner: Vector2 in CORNERS:
		hold(corner)
		var trace := idle_trace(4.0)
		check(within(mood.outputs), "outputs out of bounds at %s: %s" % [corner, mood.outputs])
		check(widget.blink_controller.minimum_pause >= 5.0 * (1.0 - Mood.BLINK) - EPS and widget.blink_controller.minimum_pause <= 5.0 * (1.0 + Mood.BLINK) + EPS,
			"blink pause out of bounds at %s" % corner)
		check(absf(widget.gaze.liveliness) <= 1.0, "gaze liveliness out of bounds at %s" % corner)
		var factor: float = mood.outputs.sway
		var pitch := Quaternion(Vector3.RIGHT, mood.outputs.pitch)
		var worst_sway := 0.0
		var worst_pose := 0.0
		var worst_claw := 0.0
		var worst_lift := 0.0
		var amplitude := [0.0, 0.0]
		for i in trace.size():
			var calm_pose: Quaternion = trace[i][0] * pitch.inverse()   # the posture lean taken off
			worst_sway = maxf(worst_sway, absf(from_rest(calm_pose) - factor * from_rest(neutral[i][0])))
			worst_pose = maxf(worst_pose, (neutral[i][0] as Quaternion).angle_to(trace[i][0]))
			worst_claw = maxf(worst_claw, (neutral[i][1] as Quaternion).angle_to(trace[i][1]))
			worst_lift = maxf(worst_lift, absf(float(trace[i][2]) - float(neutral[i][2])))
			amplitude[0] = maxf(amplitude[0], from_rest(calm_pose))
			amplitude[1] = maxf(amplitude[1], from_rest(neutral[i][0]))
		sways[str(corner)] = amplitude[0] / maxf(amplitude[1], 1e-6)
		check(worst_sway < 2e-3, "the idle sway should be scaled by %.2f at %s (off by %.4f rad)" % [factor, corner, worst_sway])
		var allowed := deg_to_rad(Mood.PITCH_DEG) + Mood.SWAY * 0.2 + 0.01
		check(worst_pose <= allowed, "the body turned %.1f° from neutral at %s" % [rad_to_deg(worst_pose), corner])
		check(worst_claw <= deg_to_rad(Mood.CLAW_DEG) + 0.01, "the claws moved %.1f° at %s" % [rad_to_deg(worst_claw), corner])
		check(worst_lift <= Mood.LIFT + Mood.SULK_LIFT + 0.003, "the body moved %.4f up or down at %s" % [worst_lift, corner])
		# The eyes: the layer as the bounds say, and on the morphs after the next blink pass.
		var layer: Array = widget.blink_controller.layers.get("mood", [0.0, 0.0, 0.0, 0.0])
		check(layer[0] <= Mood.WIDE + EPS and layer[1] <= Mood.HAPPY + EPS and layer[2] <= Mood.SQUINT + EPS and layer[3] <= Mood.LIDS + EPS,
			"the eye layer out of bounds at %s: %s" % [corner, layer])
		check((corner.y < 0.0) == (morph("squint") > 0.0), "grumpy squints, and only grumpy (%s)" % corner)
	report["sway_ratio"] = sways
	check(sways[str(Vector2(1, 1))] > 1.2 and sways[str(Vector2(-1, -1))] < 0.8, "lively sways more, calm less: %s" % sways)

	# 4. Dance: the moves scaled by dance_scale(), within its bound; the beat and the eyes untouched.
	hold(Vector2(1, 0))
	var probe := DanceStyle.new()
	var m: Dictionary = probe.moves("rave", 1.8e9 + 0.1)
	probe.free()
	var bigger: Dictionary = DanceStyle.scaled(m, mood.dance_scale())
	check(absf(mood.dance_scale() - (1.0 + Mood.DANCE)) < EPS, "lively dances 25 % bigger")
	check(absf(float(bigger.squash) - float(m.squash) * mood.dance_scale()) < EPS and bigger.speed == m.speed and bigger.wide == m.wide,
		"the dance's sizes scale, its speed and eyes do not")
	hold(Vector2(-1, 0))
	check(absf(mood.dance_scale() - (1.0 - Mood.DANCE)) < EPS, "calm dances 25 % smaller")

	# 5. A jump in the mood is never a jump in her: from one corner to the other, then a turn midway.
	hold(Vector2(-1, -1))
	advance(0.5)
	mood.on_mood({"type": "mood", "energy": 1.0, "feeling": 1.0})
	var seen := {"step": 0.0, "accel": 0.0, "eyes": 0.0}
	var was := {"at": mood.current, "step": Vector2.ZERO, "squint": morph("squint"), "frames": 0}
	advance(5.0, func() -> void:
		was.frames += 1
		if was.frames == 70:
			mood.on_mood({"type": "mood", "energy": -0.5, "feeling": 0.8})   # turned back halfway
		var stepped: Vector2 = mood.current - was.at
		seen.step = maxf(seen.step, stepped.length())
		seen.accel = maxf(seen.accel, (stepped - (was.step as Vector2)).length())
		seen.eyes = maxf(seen.eyes, absf(morph("squint") - float(was.squint)))
		was.at = mood.current
		was.step = stepped
		was.squint = morph("squint"))
	report["transition"] = seen
	check(seen.step <= Mood.MAX_RATE * FRAME * 1.01, "the dials moved %.4f in a frame" % seen.step)
	check(seen.accel <= 2.0 * Mood.SPRING * Mood.SPRING * 2.0 * FRAME * FRAME + 1e-3, "the dials' speed jumped by %.4f" % seen.accel)
	check(seen.eyes <= 0.03, "the squint jumped by %.3f in a frame" % seen.eyes)
	check(mood.current.distance_to(Vector2(-0.5, 0.8)) < 0.02, "the dials should settle on the new mood (%s)" % mood.current)

	# 6. Asleep: no mood motion. Going down it fades out; asleep the pose is the one with no mood at all.
	var asleep_neutral := []
	var asleep_mood := []
	for pass_mood in [Vector2.ZERO, Vector2(1, -1)]:
		widget.set_state("idle")
		advance(1.0)
		hold(pass_mood)
		advance(0.5)
		widget.sleeper.quiet_elapsed = 0.0
		widget.sleeper.begin_sleep()
		advance(3.5)
		check(widget.sleeper.phase == "sleeping", "she should be asleep (%s)" % widget.sleeper.phase)
		check(mood.weight == 0.0 and not widget.blink_controller.layers.has("mood") and widget.gaze.liveliness == 0.0,
			"asleep: the mood shows nowhere (weight %.2f)" % mood.weight)
		var poses := []
		advance(2.0, func() -> void:
			poses.append([rotation("body"), rotation("claw_arm_L"), skeleton.get_bone_pose_position(bone("body"))]))
		if pass_mood == Vector2.ZERO:
			asleep_neutral = poses
		else:
			asleep_mood = poses
		mood.on_mood({"type": "mood", "energy": 1.0, "feeling": 1.0})   # a mood arriving while asleep changes nothing
		advance(0.5)
		check(mood.weight == 0.0, "a mood while asleep should wait")
		widget.sleeper.activity()
		var weights := []
		advance(3.0, func() -> void:
			weights.append(mood.weight))
		check(widget.sleeper.phase == "awake" and mood.weight == 1.0, "awake again, the mood shows again")
		var jump := 0.0
		for i in range(1, weights.size()):
			jump = maxf(jump, absf(weights[i] - weights[i - 1]))
		# easing.gd in_out is steepest in the middle, three times the straight line
		check(jump <= 3.0 * FRAME / Mood.WEIGHT_S + 1e-6, "the mood should fade back in, not jump (%.3f)" % jump)
	var differ := 0.0
	for i in mini(asleep_neutral.size(), asleep_mood.size()):
		differ = maxf(differ, (asleep_neutral[i][0] as Quaternion).angle_to(asleep_mood[i][0]))
		differ = maxf(differ, (asleep_neutral[i][1] as Quaternion).angle_to(asleep_mood[i][1]))
		differ = maxf(differ, (asleep_neutral[i][2] as Vector3).distance_to(asleep_mood[i][2]))
	check(differ < 1e-5, "asleep, her pose should not depend on the mood (%.6f)" % differ)

	# 7. Previews, with a display: the tree runs on its own again.
	if capture_dir != "":
		for node: Node in [widget.mood, widget.legs, widget.reactions, widget.dance, widget.touch, widget.turn, widget.gaze,
				widget.sleeper, widget.blink_controller, widget.wander]:
			node.set_process(true)
		widget.player.callback_mode_process = AnimationMixer.ANIMATION_CALLBACK_MODE_PROCESS_IDLE
		await previews()

	var path: String = widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(path)
	report.merge({"passed": failures.is_empty(), "failures": failures, "neutral_without_one": true, "bounds": true,
		"asleep_ignores_mood": true, "smooth_transitions": true, "dance_scaled": true})
	var out := FileAccess.open("res://mood_checks.json", FileAccess.WRITE)   # none in the exported binary
	if out:
		out.store_string(JSON.stringify(report, "\t"))
	print(JSON.stringify(report))
	print("mood checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
