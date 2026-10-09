extends SceneTree
## The legs and pincers (WIRING.md §9, §13): the rig's bones and their skin, the clips driving the
## legs, the pincers following her voice with a spring. Real widget, isolated settings, no daemon
## (a stand-in socket).
##
## godot --headless --path widget --script res://validate_legs.gd
class FakeWs:
	extends Node
	var sent: Array = []
	func is_open() -> bool:
		return true
	func send(data: Dictionary) -> void:
		sent.append(data)

class TestWidget:
	extends "res://widget.gd"
	func settings_path() -> String:
		return "user://legs_test_%s.cfg" % OS.get_process_id()
	func setup_ws() -> void:
		ws = FakeWs.new()
		add_child(ws)
	func setup_sleep() -> void:
		super.setup_sleep()
		sleeper.monitor_enabled = false

const LEGS := ["L1", "L2", "L3", "R1", "R2", "R3"]

var failures: Array[String] = []
var report := {}
var widget: TestWidget
var skeleton: Skeleton3D

func _initialize() -> void:
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

func wait(seconds: float) -> void:
	await create_timer(seconds).timeout

## How far a bone is turned from its rest (radians).
func turned(bone_name: String) -> float:
	var bone := skeleton.find_bone(bone_name)
	return skeleton.get_bone_pose_rotation(bone).angle_to(skeleton.get_bone_rest(bone).basis.get_rotation_quaternion())

## The largest turn of any leg bone.
func legs_turned() -> float:
	var most := 0.0
	for leg in LEGS:
		for part in ["_upper", "_lower"]:
			most = maxf(most, turned("leg_" + leg + part))
	return most

## The clip `name` at `at` seconds, written by the mixer now.
func pose_clip(name: String, at: float) -> void:
	widget.player.play(name)
	widget.player.seek(at, true)

## A mesh's skin binds, by bone name.
func bind_names(mesh_name: String) -> Array:
	var mesh: MeshInstance3D = widget.model.find_child(mesh_name, true, false)
	var names := []
	for i in mesh.skin.get_bind_count():
		var n := String(mesh.skin.get_bind_name(i))
		if n == "":
			n = skeleton.get_bone_name(mesh.skin.get_bind_bone(i))
		names.append(n)
	return names

func run() -> void:
	widget = TestWidget.new()
	widget.place_at = Vector2.ZERO
	root.add_child(widget)
	skeleton = widget.model.find_children("*", "Skeleton3D", true, false)[0]
	await wait(0.3)

	# 1. The bones and their skin.
	for s in ["L", "R"]:
		var pincer := skeleton.find_bone("pincer_" + s)
		check(pincer >= 0 and skeleton.get_bone_parent(pincer) == skeleton.find_bone("claw_arm_" + s), "pincer_%s should hang from claw_arm_%s" % [s, s])
		var sign := 1.0 if s == "L" else -1.0
		var hinge := skeleton.get_bone_global_rest(pincer).origin
		check(hinge.distance_to(Vector3(sign * 0.373, 0.401, -0.16)) < 0.002, "pincer_%s should sit on the lower claw's hinge, at %s" % [s, hinge])
		check(bind_names("mesh_claw_lower_" + s).has("pincer_" + s), "the lower claw should follow pincer_" + s)
		for i in range(1, 4):
			var leg := "leg_%s%d" % [s, i]
			var upper := skeleton.find_bone(leg + "_upper")
			var lower := skeleton.find_bone(leg + "_lower")
			check(upper >= 0 and lower >= 0, leg + " should have two bones")
			check(skeleton.get_bone_parent(upper) == skeleton.find_bone("body") and skeleton.get_bone_parent(lower) == upper, leg + ": body > upper > lower")
			var binds := bind_names("mesh_leg_%s%d" % [s, i])
			check(binds.has(leg + "_upper") and binds.has(leg + "_lower"), "mesh_leg_%s%d should be skinned to both its bones, has %s" % [s, i, binds])
	var keys := []
	for node in widget.model.find_children("*", "MeshInstance3D", true, false):
		var mesh := node as MeshInstance3D
		for k in ["claw_open_L", "claw_open_R", "leg_tuck", "sleep_fold"]:
			if mesh.find_blend_shape_by_name(k) != -1:
				keys.append("%s/%s" % [mesh.name, k])
	check(keys.is_empty(), "the claw and leg shape keys should be gone (bones replace them), found %s" % [keys])
	report["bones"] = skeleton.get_bone_count()

	# 2. The clips drive the legs: the hop tucks them, the dance lifts them a side at a time, and the
	# mixer puts them back each frame (a bone a layer turned is reset by the next frame's clip).
	widget.set_state("talking")
	await wait(0.4)
	widget.one_shot = "test"   # keep the widget from replacing the clips posed below
	widget.player.pause()
	pose_clip("talk_base", 0.0)
	report["talk_legs_deg"] = snappedf(rad_to_deg(legs_turned()), 0.1)
	check(legs_turned() < deg_to_rad(0.5), "talk_base: legs at rest, turned %.1f°" % rad_to_deg(legs_turned()))
	pose_clip("notify_perk", 0.4 * widget.player.get_animation("notify_perk").length)
	report["hop_legs_deg"] = snappedf(rad_to_deg(legs_turned()), 0.1)
	check(legs_turned() > deg_to_rad(20.0), "notify_perk: the hop should tuck the legs, turned %.1f°" % rad_to_deg(legs_turned()))
	var dance_len := widget.player.get_animation("dance_loop").length
	pose_clip("dance_loop", dance_len / 32.0)    # a quarter of the first lift: the left side up
	var left := turned("leg_L2_lower")
	var right := turned("leg_R2_lower")
	report["dance_lift_deg"] = [snappedf(rad_to_deg(left), 0.1), snappedf(rad_to_deg(right), 0.1)]
	check(left > deg_to_rad(8.0) and right < deg_to_rad(1.0), "dance_loop: one side's legs lift at a time (%.1f°, %.1f°)" % [rad_to_deg(left), rad_to_deg(right)])
	skeleton.set_bone_pose_rotation(skeleton.find_bone("leg_R2_lower"), Quaternion(Vector3.UP, 1.0))
	pose_clip("talk_base", 0.0)
	check(turned("leg_R2_lower") < deg_to_rad(0.5), "the mixer should reset a leg bone each frame")
	widget.player.play("talk_base")
	widget.one_shot = ""

	# 3. The pincers follow her voice through the spring, the bone turning about the hinge.
	var claws: Node = widget.claw_controller
	check(claws.value(0) < 0.01, "the pincers should start shut")
	widget.perform({"state": "talking", "text": "Testing the pincers.", "audio": make_test_wav(1.4)})
	var peak := 0.0
	var angle_ok := true
	var waited := 0.0
	while waited < 1.0:
		await process_frame
		waited += widget.get_process_delta_time()
		peak = maxf(peak, claws.value(0))
		var expected: float = claws.OPEN_ANGLE * claws.value(0)
		angle_ok = angle_ok and absf(turned("pincer_L") - expected) < deg_to_rad(0.5) and absf(turned("pincer_R") - claws.value(1) * claws.OPEN_ANGLE) < deg_to_rad(0.5)
	report["voice_pincer_peak"] = snappedf(peak, 0.01)
	check(widget.speech.playing and peak > 0.1, "the pincers should open to her voice, peak %.2f" % peak)
	check(angle_ok, "the pincer bones should turn by the opening (48° at 1)")
	widget.speech.stop()
	await wait(0.4)
	check(claws.value(0) < 0.01 and turned("pincer_L") < deg_to_rad(0.5), "the pincers should shut after her voice")
	widget.bubble.hide()
	widget.bubble.speaking = false
	widget.set_state("idle")

	# 4. Snap, weight and a clack: a step to fully open overshoots a little and settles; shutting it clacks.
	var clacks: int = claws.clacks
	var most := 0.0
	var pincer_l := skeleton.find_bone("pincer_L")
	var tip_rest := skeleton.get_bone_global_rest(pincer_l).origin + Vector3(0.13, -0.005, 0.0)
	for i in 30:
		claws.request(0, 1.0)
		await process_frame
		most = maxf(most, claws.value(0))
	var tip_open := skeleton.get_bone_global_pose(pincer_l) * (skeleton.get_bone_global_rest(pincer_l).affine_inverse() * tip_rest)
	report["step_overshoot"] = snappedf(most, 0.01)
	report["step_settled"] = snappedf(claws.value(0), 0.01)
	check(most > 1.03 and most < claws.MAX_OPEN + 0.001, "a step should overshoot a little, peak %.2f" % most)
	check(absf(claws.value(0) - 1.0) < 0.05, "and settle on the mark, %.2f" % claws.value(0))
	check(tip_open.y < tip_rest.y - 0.02, "opening swings the lower claw down from the upper one")
	await wait(0.3)
	report["clacks"] = claws.clacks - clacks
	check(claws.clacks > clacks and claws.value(0) < 0.01, "shutting should clack and stay shut")

	var path: String = widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(path)
	report["passed"] = failures.is_empty()
	report["failures"] = failures
	print(JSON.stringify(report))
	print("legs checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)

## A 1 kHz tone with a syllable-like 5 Hz wobble (as validate_widget.gd), in this run's user dir.
func make_test_wav(seconds: float) -> String:
	var rate := 22050
	var frames := int(seconds * rate)
	var bytes := PackedByteArray()
	bytes.resize(frames * 2)
	for i in frames:
		var t := float(i) / rate
		var envelope := 0.55 + 0.45 * sin(TAU * 5.0 * t)
		bytes.encode_s16(i * 2, int(0.6 * envelope * sin(TAU * 1000.0 * t) * 32767.0))
	var stream := AudioStreamWAV.new()
	stream.format = AudioStreamWAV.FORMAT_16_BITS
	stream.mix_rate = rate
	stream.stereo = false
	stream.data = bytes
	var path := OS.get_user_data_dir().path_join("legs_tone_%s.wav" % OS.get_process_id())
	stream.save_to_wav(path)
	return path
