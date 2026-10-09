extends SceneTree
## The legs and pincers (WIRING.md §9, §13): the rig's bones and their skin, the clips driving the
## legs, the pincers following her voice with a spring, the procedural leg pass (dance, reactions,
## touch), the gait under a drag, Wander's rules (direction, staying on her monitor, never while
## busy, stopping when something starts, never while pressed) and the scuttle on the sixth poke.
## Real widget, isolated settings, no daemon (a stand-in socket), a stand-in desktop for the window.
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

	# 5. The procedural leg pass: stomps and taps on the beat (harder for rave and headbang), the hop's
	# crouch and landing, the shiver, the peek's tiptoes, a tickle's scrabble, a hold settling her.
	var legs: Node = widget.legs
	var dance_lifts := {}
	for style in ["bounce", "rave", "headbang"]:
		dance_lifts[style] = await dance_lift(style)
	report["dance_toe_lift"] = dance_lifts
	check(dance_lifts.bounce > 0.012, "bounce: a front leg should tap the beat, %.3f" % dance_lifts.bounce)
	check(dance_lifts.rave > dance_lifts.bounce + 0.008 and dance_lifts.headbang > dance_lifts.bounce + 0.008, "rave and headbang should stomp harder than bounce taps (%s)" % [dance_lifts])
	widget.perform({"state": "idle"})
	widget.dance.set_tempo({"silent": true})
	await wait(0.6)

	widget.perform({"state": "idle", "reaction": "double_hop"})
	var crouch := 0.0
	var planted := 0.0
	waited = 0.0
	while waited < 0.2:
		await process_frame
		waited += widget.get_process_delta_time()
		var dropped: float = skeleton.get_bone_global_pose(skeleton.find_bone("body")).origin.y - legs.clip_body.origin.y
		if dropped < crouch:
			crouch = dropped
			planted = (legs.toe_now("L2") - (legs.clip["L2"][2] as Vector3)).length()
	report["hop_crouch"] = [snappedf(crouch, 0.001), snappedf(planted, 0.001)]
	check(crouch < -0.015 and planted < 0.004, "double_hop: a crouch before the jump, toes staying put (%.3f, %.3f)" % [crouch, planted])
	var landing := 0.0
	var knee_most := 0.0
	while widget.one_shot != "":
		await process_frame
		var hop: float = widget.player.current_animation_position / widget.player.get_animation("notify_perk").length
		if hop > 0.62 and hop < 0.84:
			var dropped: float = skeleton.get_bone_global_pose(skeleton.find_bone("body")).origin.y - legs.clip_body.origin.y
			landing = minf(landing, dropped)
			knee_most = maxf(knee_most, turned("leg_L2_lower"))
	report["hop_landing"] = [snappedf(landing, 0.001), snappedf(rad_to_deg(knee_most), 0.1)]
	check(landing < -0.015 and knee_most > deg_to_rad(10.0), "double_hop: the landing bends the knees (%.3f, %.1f°)" % [landing, rad_to_deg(knee_most)])
	await wait(0.4)

	var trembled := await toe_motion(func(): widget.reactions.play("shiver"), 0.6)
	report["shiver_toe_motion"] = snappedf(trembled, 0.001)
	check(trembled > 0.004, "shiver should tremble the legs, %.3f" % trembled)
	await wait(0.4)
	widget.reactions.play("peek")
	await wait(0.6)
	var up: float = skeleton.get_bone_global_pose(skeleton.find_bone("body")).origin.y - legs.clip_body.origin.y
	var toe_in: float = absf((legs.clip["L1"][2] as Vector3).x) - absf(legs.toe_now("L1").x)
	report["peek_tiptoe"] = [snappedf(up, 0.001), snappedf(toe_in, 0.001)]
	check(up > 0.01 and toe_in > 0.015, "peek: up on tiptoe, toes drawn in (%.3f, %.3f)" % [up, toe_in])
	await wait(1.2)
	var scrabble := await toe_motion(func(): widget.touch.play("tickle"), 0.6)
	report["tickle_toe_motion"] = snappedf(scrabble, 0.001)
	check(scrabble > 0.01, "a tickle should make the legs scrabble, %.3f" % scrabble)
	await wait(0.8)
	widget.touch.holding = true
	widget.touch.play("hold")
	await wait(0.7)
	var sunk: float = skeleton.get_bone_global_pose(skeleton.find_bone("body")).origin.y - legs.clip_body.origin.y
	widget.touch.holding = false
	report["hold_settle"] = snappedf(sunk, 0.001)
	check(sunk < -0.008, "a hold settles her legs (she sinks a little), %.3f" % sunk)
	await wait(0.8)

	# 6. The gait: her window moving (a drag here) sets the tripods going, at the drag's speed, and
	# her toes stay put on the desktop through each stance; still, the feet settle back.
	var wander: Node = widget.wander
	widget.set_state("talking")   # legs at rest under her while the gait is measured
	wander.desk = {"screens": [Rect2i(0, 0, 1920, 1040), Rect2i(1920, 0, 1280, 1024)], "pos": Vector2i(700, 400), "size": Vector2i(380, 560)}
	await wait(0.3)
	var falls: int = legs.footfalls
	var stance_ok := true
	var tripod_ok := true
	var outside := 0
	var hull := widget.passthrough_polygon()
	waited = 0.0
	while waited < 1.0:
		var d := widget.get_process_delta_time()
		wander.desk.pos += Vector2i(roundi(150.0 * maxf(d, 1.0 / 60.0)), 0)   # dragged right, ~150 px/s
		await process_frame
		waited += d
		if legs.gait_weight >= 1.0:
			tripod_ok = tripod_ok and legs.in_stance("L1") == legs.in_stance("R2") and legs.in_stance("R2") == legs.in_stance("L3") \
				and legs.in_stance("L1") != legs.in_stance("R1") and legs.in_stance("R1") == legs.in_stance("L2")
			# In stance the toe sweeps toward her +X (the screen's left) as the window goes right.
			stance_ok = stance_ok and legs.stride < 0.0
			for p in widget.body_points():
				if not Geometry2D.is_point_in_polygon(p, hull):
					outside += 1
	var hz: float = (legs.footfalls - falls) / 2.0
	report["drag_gait"] = {"motion": snappedf(legs.motion.x, 1.0), "cycles_per_s": hz, "stride": snappedf(legs.stride, 0.001), "outside_hull": outside}
	check(legs.gait_weight > 0.99 and legs.motion.x > 100.0, "a drag should set her walking (%.2f, %.0f px/s)" % [legs.gait_weight, legs.motion.x])
	check(hz > 1.5 and hz < legs.MAX_HZ + 0.5, "the cadence should follow the drag, %.1f cycles a second" % hz)
	check(tripod_ok, "a tripod gait: L1, R2, L3 together, half a cycle from R1, L2, R3")
	check(stance_ok, "she scuttles sideways, the stride along her X against the drag")
	check(outside == 0, "her stepping legs should stay inside the click-through hull (%d points out)" % outside)
	# Planted: a stance toe's screen position (window + toe) barely moves while the window goes on.
	var leg := "L1" if legs.in_stance("L1") else "R1"
	var screen_a: float = wander.desk.pos.x + widget.touch.world_to_pixel(skeleton.global_transform * legs.toe_now(leg)).x
	var d2 := widget.get_process_delta_time()
	wander.desk.pos += Vector2i(roundi(150.0 * maxf(d2, 1.0 / 60.0)), 0)
	await process_frame
	if legs.in_stance(leg):
		var screen_b: float = wander.desk.pos.x + widget.touch.world_to_pixel(skeleton.global_transform * legs.toe_now(leg)).x
		report["stance_toe_slip_px"] = snappedf(absf(screen_b - screen_a), 0.1)
		check(absf(screen_b - screen_a) < 2.5, "a stance toe should stay put on the desktop, slipped %.1f px" % absf(screen_b - screen_a))
	await wait(0.6)
	check(legs.gait_weight == 0.0 and legs_turned() < deg_to_rad(0.5), "still again, her feet settle back under her")

	# 7. Wander: on by default, saved, in the menu; the walk's direction by where she is; she stays on
	# her monitor; never while busy; a walk stops when something starts; never while pressed.
	widget.set_state("idle")
	await wait(0.3)
	check(widget.wander_enabled and wander.enabled, "Wander should be on by default")
	widget.menu._refresh()
	check(widget.menu.is_item_checked(widget.menu.get_item_index(widget.menu.WANDER)), "the menu should show Wander on")
	widget.menu._on_pressed(widget.menu.WANDER)
	var cfg := ConfigFile.new()
	cfg.load(widget.settings_path())
	check(not widget.wander_enabled and cfg.get_value("window", "wander", true) == false, "Wander off should be saved")
	wander.until_walk = 0.0
	await wait(0.2)
	check(not wander.walking, "Wander off: no walk")
	widget.menu._on_pressed(widget.menu.WANDER)
	check(widget.wander_enabled, "the menu turns Wander back on")

	var ranges := []
	for start in [10, 1530, 700]:
		wander.desk.pos = Vector2i(start, 400)
		var dirs := {}
		for i in 6:
			wander.desk.pos = Vector2i(start, 400)
			await process_frame
			check(wander.wander(), "a walk should start from x %d" % start)
			check(wander.until_walk >= wander.WANDER_S.x and wander.until_walk <= wander.WANDER_S.y, "the next walk is 5 to 15 minutes off")
			dirs[wander.direction] = true
			var lo: int = wander.desk.pos.x
			var hi: int = lo
			while wander.walking:
				await process_frame
				lo = mini(lo, wander.desk.pos.x)
				hi = maxi(hi, wander.desk.pos.x)
			ranges.append([start, lo, hi])
			check(lo >= 0 and hi <= 1920 - 380, "she stays on her monitor (from %d: %d..%d)" % [start, lo, hi])
		if start == 10:
			check(dirs.keys() == [1.0], "near the left edge she walks right")
		elif start == 1530:
			check(dirs.keys() == [-1.0], "near the right edge she walks left (never onto the next display)")
	report["walks"] = ranges
	# The right-hand monitor's own edges hold her there too.
	wander.desk.pos = Vector2i(1920 + 1280 - 380 - 2, 300)
	check(wander.wander() and wander.direction < 0.0, "on the second monitor, at its right edge, she walks left")
	while wander.walking:
		await process_frame
		check(wander.desk.pos.x >= 1920 and wander.desk.pos.x <= 1920 + 1280 - 380, "she stays on the second monitor")

	wander.desk.pos = Vector2i(700, 400)
	var busy_cases := {
		"talking": func(on: bool): widget.set_state("talking" if on else "idle"),
		"listening": func(on: bool): widget.set_state("listening" if on else "idle"),
		"thinking": func(on: bool): widget.set_state("thinking" if on else "idle"),
		"dancing": func(on: bool): widget.set_state("dancing" if on else "idle"),
		"pressed": func(on: bool):
			widget.pressing = on
			widget.press_at = Time.get_ticks_msec() / 1000.0
			if not on:
				widget.touch.release_hold(),
		"dragged": func(on: bool): widget.dragging = on,
		"run": func(on: bool):
			if on:
				widget.step_chip.on_phase({"type": "thinking", "run_id": "r-walk"})
			else:
				widget.step_chip.on_phase({"type": "run.completed", "run_id": "r-walk"}),
		"approval": func(on: bool):
			if on:
				widget.approval_card.welcomed({"approvals": true, "approval": true})
				widget.approval_card.on_event({"type": "approval.request", "run_id": "r-ok", "seq": 1, "t": widget.brain_now(),
					"approval_id": "a-walk-1", "risk": "change", "prompt": "Walk?", "timeout_s": 10.0, "expires_t": widget.brain_now() + 10.0, "hold": false})
			else:
				widget.approval_card.close("test"),
		# Sleep itself never starts mid-walk (sleep_controller.can_sleep); asleep is set directly here.
		"asleep": func(on: bool): widget.sleeper.phase = "sleeping" if on else "awake",
	}
	var stopped := {}
	for name in busy_cases:
		var toggle: Callable = busy_cases[name]
		await calm_down()
		# Busy first: no walk starts.
		toggle.call(true)
		await process_frame
		var refused: bool = not wander.wander()
		wander.until_walk = 0.0
		await process_frame
		refused = refused and not wander.walking
		toggle.call(false)
		await calm_down()
		# Walking, then it starts: she stops at once and the window stays where it is.
		check(wander.wander(), "a walk should start before %s (%s, %s)" % [name, wander.blocker(), widget.touch.recipe])
		await wait(0.3)
		toggle.call(true)
		await process_frame
		var held: int = wander.desk.pos.x
		await wait(0.3)
		stopped[name] = [refused, not wander.walking, wander.stop_reason, wander.desk.pos.x == held]
		check(refused, "no walk while %s" % name)
		check(not wander.walking and wander.desk.pos.x == held, "a walk should stop at once when %s starts (%s)" % [name, wander.stop_reason])
		toggle.call(false)
	report["walk_stops"] = stopped
	await calm_down()

	# 8. Touch level 3: the sixth poke in a row sends her scuttling a short step away from it, and the
	# annoyance starts over. With no room that way she scrabbles in place.
	wander.desk.pos = Vector2i(700, 400)
	var left_of_her := aim(Vector3(0.12, 0.47, -0.12))     # her shell, on the viewer's left
	var scuttle_signals := [0]
	widget.touch.scuttle_wanted.connect(func(): scuttle_signals[0] += 1)
	var from: int = wander.desk.pos.x
	for i in 6:
		await tap(left_of_her)
		await wait(0.15)
	report["scuttle"] = {"level": widget.touch.last_level, "kind": wander.kind, "dir": wander.direction, "streak": widget.touch.streak}
	check(widget.touch.last_level == 3 and scuttle_signals[0] == 1 and wander.walking and wander.kind == "scuttle", "the sixth poke should scuttle her")
	check(wander.direction > 0.0, "away from a poke on her left side: to the right")
	check(widget.touch.streak == 0, "and the annoyance resets")
	waited = 0.0
	var scuttle_gait := 0.0
	while wander.walking and waited < 2.0:
		await process_frame
		waited += widget.get_process_delta_time()
		scuttle_gait = maxf(scuttle_gait, legs.gait_weight)
	report["scuttle"]["moved_px"] = wander.desk.pos.x - from
	check(wander.desk.pos.x - from > wander.SCUTTLE_PX - 6 and wander.desk.pos.x - from <= wander.SCUTTLE_PX + 6 and scuttle_gait > 0.9, "a short scuttle with the gait (%d px)" % (wander.desk.pos.x - from))
	await calm_down()
	wander.desk.pos = Vector2i(1920 - 380, 400)
	widget.touch.streak = 0
	for i in 6:
		await tap(left_of_her)
		await wait(0.15)
	check(wander.kind == "shuffle" and wander.desk.pos.x == 1920 - 380, "no room to the right: she scrabbles in place (%s)" % wander.kind)
	await wait(0.8)
	wander.desk = {}

	var path: String = widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(path)
	report["passed"] = failures.is_empty()
	report["failures"] = failures
	print(JSON.stringify(report))
	print("legs checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)

## Nothing going on: idle, awake, no recipe, no clip on top, no walk, no run, no card.
func calm_down() -> void:
	widget.pressing = false
	widget.dragging = false
	widget.touch.release_hold()
	if widget.wander.walking:
		widget.wander.stop("test")
	widget.set_state("idle")
	widget.rest_state = "idle"
	var waited := 0.0
	while waited < 5.0 and (widget.one_shot != "" or widget.reactions.recipe != "" or widget.touch.recipe != "" \
			or widget.sleeper.phase != "awake" or widget.step_chip.visible or widget.approval_card.visible):
		await process_frame
		waited += widget.get_process_delta_time()
	await process_frame

## Model-space point -> window pixel, through her current pose.
func aim(point: Vector3) -> Vector2:
	return widget.touch.world_to_pixel(widget.model.global_transform * point)

func button(at: Vector2, pressed: bool) -> void:
	var e := InputEventMouseButton.new()
	e.button_index = MOUSE_BUTTON_LEFT
	e.pressed = pressed
	e.position = at
	e.global_position = at
	Input.parse_input_event(e)
	await process_frame

func tap(at: Vector2) -> void:
	await button(at, true)
	await wait(0.06)
	await button(at, false)

## The highest a front or middle toe lifts above where the clip has it, over a second of `style`.
func dance_lift(style: String) -> float:
	var by_style := {"rave": [130.0, 0.8, 0.7, 0.45, 3.0], "headbang": [160.0, 0.6, 0.3, 0.2, 5.0], "bounce": [112.0, 0.6, 0.3, 0.15, 3.0]}
	var f: Array = by_style[style]
	widget.perform({"state": "dancing"})
	var t := {"bpm": f[0], "period_s": 60.0 / f[0], "confidence": f[1], "next_beat": Time.get_unix_time_from_system() + 0.3,
		"evenness": f[2], "low_ratio": f[3], "density": f[4], "loudness_db": -16.0}
	widget.dance.set_tempo(t)
	widget.dance.set_tempo(t)
	await wait(0.8)
	check(widget.dance.style == style, "dance style should be %s, was %s" % [style, widget.dance.style])
	var most := 0.0
	var waited := 0.0
	while waited < 1.2:
		await process_frame
		waited += widget.get_process_delta_time()
		for leg in ["L1", "R1", "L2", "R2"]:
			most = maxf(most, widget.legs.toe_now(leg).y - (widget.legs.clip[leg][2] as Vector3).y)
	return snappedf(most, 0.001)

## Start something, then the largest any toe strays from where the clip has it over `seconds`.
func toe_motion(start: Callable, seconds: float) -> float:
	start.call()
	var most := 0.0
	var waited := 0.0
	while waited < seconds:
		await process_frame
		waited += widget.get_process_delta_time()
		for leg in widget.legs.LEGS:
			most = maxf(most, widget.legs.toe_now(leg).distance_to(widget.legs.clip[leg][2] as Vector3))
	return most

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
