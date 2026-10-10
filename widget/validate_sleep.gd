extends SceneTree
## Sleep (widget/SLEEP.md): the delay and what keeps her awake, the tuck and spin down (she hops, the
## legs and claws tuck away, the eyes droop, one full turn with a coin wobble, and she ends facing the
## user), breathing asleep, waking back to the normal pose, interruptions either way that never jump
## or stick half way, the reduced-motion version, turn.gd handing over, the window and the
## click-through shape, music and notifications waking her, and the saved delay.
## Real widget, isolated settings and no daemon traffic.
##
## godot --headless --audio-driver Dummy --path widget --script res://validate_sleep.gd
## Add `-- --capture-dir=/absolute/path` and run with a display (xvfb-run works) for key frames.
class TestWidget:
	extends "res://widget.gd"
	func settings_path() -> String:
		return "user://sleep_test_%s.cfg" % OS.get_process_id()
	func setup_ws() -> void:
		pass
	func setup_sleep() -> void:
		super.setup_sleep()
		sleeper.monitor_enabled = false
	func _input(_event: InputEvent) -> void:
		pass

const FRAME := 1.0 / 60.0
const KINK := 0.09               # the most a frame's step may change from the one before (eyes, body):
                                 # the landings' stops are the sharpest part of the uninterrupted moves
const KINK_SLACK := 1.15         # an interruption may be no sharper than they are, give or take this
const LEGS := ["L1", "L2", "L3", "R1", "R2", "R3"]

var failures: Array[String] = []
var widget: TestWidget
var skeleton: Skeleton3D
var capture_dir := ""
var report := {}

func _initialize() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--capture-dir="):
			capture_dir = arg.trim_prefix("--capture-dir=")
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error(message)

## Frame by frame, in the order the tree runs them: the clip, the controller, the lids, the pincers'
## gestures, the tuck layer's bones, the hat. `each` (optional) is called after every frame.
func advance(seconds: float, each := Callable()) -> void:
	for i in roundi(seconds * 60.0):
		widget.player.advance(FRAME)
		widget.sleeper.tick(FRAME)
		widget.blink_controller.advance_blink(FRAME)
		widget.claw_controller.advance_gestures(FRAME)
		widget.sleeper.tuck.apply()
		widget.top_hat.update_pose(FRAME)
		if each.is_valid():
			each.call()

func morph(name: String, key: String) -> float:
	var mesh: MeshInstance3D = widget.model.find_child(name, true, false)
	return mesh.get_blend_shape_value(mesh.find_blend_shape_by_name(key))

func bone(name: String) -> int:
	return skeleton.find_bone(name)

## How far a bone is turned from its rest (radians).
func turned(name: String) -> float:
	return skeleton.get_bone_pose_rotation(bone(name)).angle_to(skeleton.get_bone_rest(bone(name)).basis.get_rotation_quaternion())

## Her whole turn (the root bone): the spin and the wobble, radians away from facing the user.
func facing_off() -> float:
	return skeleton.get_bone_pose_rotation(bone("root")).get_angle()

## The eye's centre (skeleton space), following its stalk.
func eye(side: String) -> Vector3:
	var b := bone("eyestalk_" + side)
	var s := 1.0 if side == "L" else -1.0
	return skeleton.get_bone_global_pose(b) * (skeleton.get_bone_global_rest(b).affine_inverse() * Vector3(s * 0.16, 0.635, -0.12))

## The eyes and the body, to follow from frame to frame.
func marks() -> PackedVector3Array:
	return PackedVector3Array([eye("L"), eye("R"), body_at()])

## Tracks the largest change of step from one frame to the next (the second difference) of the marks:
## a snap, a jump or a stuck-then-released pose shows as one; a fast but smooth move does not.
func follow(track: Dictionary) -> void:
	var now := marks()
	if track.has("last") and track.has("step"):
		for i in now.size():
			var step: Vector3 = now[i] - track.last[i]
			track.kink = maxf(track.get("kink", 0.0), (step - (track.step[i] as Vector3)).length())
	if track.has("last"):
		var steps := []
		for i in now.size():
			steps.append(now[i] - track.last[i])
		track.step = steps
	track.last = now

## Where the body is (the shell and belly's bone), in skeleton space.
func body_at() -> Vector3:
	return skeleton.get_bone_global_pose(bone("body")).origin

## Every leg's toe close to its hip and every claw shrunk: tucked away under the shell.
func tucked() -> bool:
	for leg in LEGS:
		var hip := skeleton.get_bone_global_pose(bone("leg_%s_upper" % leg)).origin
		if widget.legs.toe_now(leg).distance_to(hip) > 0.035:
			return false
	for side in ["L", "R"]:
		if skeleton.get_bone_pose_scale(bone("claw_arm_" + side)).x > 0.1:
			return false
	return true

## Everything back where the clip has it: no turn, no squash, full-size legs and claws, eyes up.
func normal_pose() -> bool:
	if facing_off() > 0.002 or not skeleton.get_bone_pose_scale(bone("body")).is_equal_approx(Vector3.ONE):
		return false
	for leg in LEGS:
		if not skeleton.get_bone_pose_scale(bone("leg_%s_upper" % leg)).is_equal_approx(Vector3.ONE):
			return false
	for side in ["L", "R"]:
		if not skeleton.get_bone_pose_scale(bone("claw_arm_" + side)).is_equal_approx(Vector3.ONE):
			return false
		if turned("eyestalk_" + side) > deg_to_rad(8.0):
			return false
	return widget.sleeper.tuck.at_rest() and not widget.sleeper.tuck.active

## All of her that is drawn, in window pixels, stays inside the window.
func inside_window() -> bool:
	var size := Vector2(float(ProjectSettings.get_setting("display/window/size/viewport_width")),
		float(ProjectSettings.get_setting("display/window/size/viewport_height")))
	for p in widget.body_points():
		if p.x < 0.0 or p.y < 0.0 or p.x > size.x or p.y > size.y:
			return false
	return true

func capture(name: String) -> void:
	if capture_dir == "":
		return
	widget.player.pause()
	widget.sleeper.set_process(false)
	await process_frame
	await process_frame
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(capture_dir.path_join(name + ".png"))
	widget.sleeper.set_process(true)
	widget.player.play()

func sleep_now() -> void:
	widget.sleeper.begin_sleep()
	advance(3.0)
	check(widget.sleeper.phase == "sleeping", "Descent should finish asleep")

func run() -> void:
	widget = TestWidget.new()
	widget.look_at = Vector2(190, 340)
	widget.place_at = Vector2.ZERO   # the middle of the monitor, also with a display (captures)
	root.add_child(widget)
	await process_frame
	skeleton = widget.model.find_children("*", "Skeleton3D", true, false)[0]
	var sleep: Node = widget.sleeper
	var tuck: Node = sleep.tuck
	check(widget.sleep_after_minutes == 5.0, "Default delay should be five minutes")
	check(widget.player.has_animation("sleep_loop"), "Missing clip sleep_loop")
	check(widget.player.get_animation("sleep_loop").length >= 8.0, "Sleep breathing should take eight seconds")
	widget.set_top_hat(true)
	await capture("sleep_0_awake")
	# Both desktop inactivity and quiet widget time must reach the configured delay.
	sleep.idle_seconds = 600.0
	sleep.quiet_elapsed = 299.0
	sleep.tick(0.5)
	check(sleep.phase == "awake", "Slept before delay")
	sleep.quiet_elapsed = 299.99
	sleep.tick(FRAME)
	check(sleep.phase == "entering", "Did not sleep after inactivity threshold")

	# Going down: crouch, hop, tuck, droop, land, one turn with a wobble, facing the user.
	var standing := body_at().y
	var seen := {"top": -1.0, "low": 1.0, "spun": 0.0, "tilt": 0.0, "last_yaw": 0.0, "window": true, "tucked_in_air": false, "kink": 0.0}
	var watch := func() -> void:
		var y := body_at().y - standing
		seen.top = maxf(seen.top, y)
		seen.low = minf(seen.low, y)
		var yaw: float = tuck.channels.yaw
		seen.spun += maxf(0.0, yaw - seen.last_yaw)
		seen.last_yaw = yaw
		seen.tilt = maxf(seen.tilt, tuck.channels.tilt)
		seen.window = seen.window and inside_window()
		if y > 0.02 and tucked():
			seen.tucked_in_air = true
		follow(seen)
	advance(0.15, watch)
	await capture("sleep_1_crouch")
	advance(0.3, watch)
	await capture("sleep_2_hop")
	advance(0.4, watch)
	await capture("sleep_3_landed")
	advance(0.5, watch)
	await capture("sleep_4_spin")
	advance(1.4, watch)
	check(sleep.phase == "sleeping", "Sleep enter did not settle asleep")
	report["hop_top"] = snappedf(seen.top, 0.001)
	report["spun_degrees"] = snappedf(rad_to_deg(seen.spun), 0.1)
	report["max_tilt_degrees"] = snappedf(rad_to_deg(seen.tilt), 0.1)
	report["largest_kink"] = snappedf(seen.kink, 0.0001)
	check(seen.top > 0.07, "She should hop up before she tucks in (%.3f)" % seen.top)
	check(seen.tucked_in_air, "Legs and claws should be tucked away while she is in the air")
	check(seen.low < -0.19, "She should land low, on her belly (%.3f)" % seen.low)
	check(absf(seen.spun - TAU) < deg_to_rad(3.0), "One full turn on landing (%.1f degrees)" % rad_to_deg(seen.spun))
	check(seen.tilt > deg_to_rad(6.0), "The coin wobble should tilt her (%.1f degrees)" % rad_to_deg(seen.tilt))
	check(seen.window, "Hop and spin must stay inside the window")
	check(seen.kink < KINK, "The way down snapped somewhere (%.4f)" % seen.kink)
	check(facing_off() < deg_to_rad(0.5) and absf(widget.turn.yaw) < 0.001, "She should end facing the user")
	check(tucked(), "Asleep, legs and claws should stay tucked away")
	for side in ["L", "R"]:
		var s := 1.0 if side == "L" else -1.0
		var head := skeleton.get_bone_global_pose(bone("eyestalk_" + side)).origin
		check(s * (eye(side).x - head.x) > 0.1 and eye(side).y < head.y, "Eye %s should droop outward over the rim" % side)
		check(morph("mesh_eye_" + side, "blink") > 0.999, "Sleeping eye should stay shut")
	# Asleep the click-through shape still holds her (a poke on her shell must reach the window).
	var hull: PackedVector2Array = widget.passthrough_polygon()
	var held := true
	for p in widget.body_points():
		held = held and Geometry2D.is_point_in_polygon(p, hull)
	check(held, "The click-through shape should cover her asleep")
	await capture("sleep_5_asleep")
	var minimum := 1.0
	var maximum := 0.0
	for i in 480:
		advance(FRAME)
		var value := morph("mesh_shell", "squash")
		minimum = minf(minimum, value)
		maximum = maxf(maximum, value)
		check(absf(value - morph("mesh_belly", "squash")) < 0.0001, "Belly detached during breath")
		check(morph("mesh_eye_L", "blink") > 0.999, "Automatic blink interfered with sleep")
	check(minimum < 0.13 and maximum > 0.21, "Relaxed breath missing")
	check(absf(widget.top_hat.lift) < 0.0001, "Sleeping hat should rest")
	check(tucked() and facing_off() < 0.001, "Breathing should not untuck or turn her")

	# Waking: shiver, hop with a twist, everything out, landing on her feet.
	sleep.activity()
	check(sleep.phase == "waking", "Input should start waking")
	seen.merge({"top": -1.0, "low": 1.0, "spun": 0.0, "tilt": 0.0, "last_yaw": 0.0, "window": true, "tucked_in_air": false, "kink": 0.0}, true)
	seen.erase("last")
	seen.erase("step")
	var twist := {"most": 0.0}
	var watch_wake := func() -> void:
		watch.call()
		twist.most = maxf(twist.most, absf(tuck.channels.yaw))
	advance(0.2, watch_wake)
	await capture("wake_1_shiver")
	advance(0.4, watch_wake)
	await capture("wake_2_hop")
	advance(0.3, watch_wake)
	await capture("wake_3_pop")
	advance(0.25, watch_wake)
	await capture("wake_4_landed")
	advance(0.4, watch_wake)
	report["wake_hop_top"] = snappedf(seen.top, 0.001)
	report["wake_twist_degrees"] = snappedf(rad_to_deg(twist.most), 0.1)
	check(seen.top > 0.05, "She should hop up when she wakes (%.3f)" % seen.top)
	check(twist.most > deg_to_rad(30.0) and twist.most < deg_to_rad(180.0), "A small spin in the wake hop (%.1f)" % rad_to_deg(twist.most))
	check(seen.window, "Wake hop must stay inside the window")
	report["largest_wake_kink"] = snappedf(seen.kink, 0.0001)
	check(seen.kink < KINK, "Waking snapped somewhere (%.4f)" % seen.kink)
	var sharpest := maxf(seen.kink, report.largest_kink) * KINK_SLACK
	check(sleep.phase == "awake" and widget.player.assigned_animation == "idle_loop", "Wake should return to idle")
	check(normal_pose(), "Wake should end in the normal pose")
	check(turned("leg_L1_upper") < deg_to_rad(6.0), "Leg fold leaked into idle")
	await capture("wake_5_awake")

	# Interrupt the way down at several points (crouch, air, landing, mid-turn): she never jumps,
	# never sticks half way, and ends awake, facing the user, in her normal pose.
	for progress in [0.01, 0.1, 0.3, 0.6, 0.8, 1.3, 2.0]:
		sleep.begin_sleep()
		advance(progress)
		var track := {"kink": 0.0}
		follow(track)
		sleep.activity()
		check(sleep.phase == "waking", "Input should wake her mid-descent at %s" % progress)
		advance(2.0, func() -> void:
			follow(track))
		check(track.kink < sharpest, "Interrupted descent snapped at %s (%.4f)" % [progress, track.kink])
		check(sleep.phase == "awake" and normal_pose(), "Interrupted wake did not finish at %s" % progress)
	# And back to sleep while she is getting up: down again from wherever she is.
	for progress in [0.2, 0.6, 1.1]:
		sleep_now()
		sleep.activity()
		advance(progress)
		var track := {"kink": 0.0}
		follow(track)
		sleep.begin_sleep()
		check(sleep.phase == "entering", "Sleep now should take her down again mid-wake at %s" % progress)
		advance(3.0, func() -> void:
			follow(track))
		check(track.kink < sharpest, "Back to sleep mid-wake snapped at %s (%.4f)" % [progress, track.kink])
		check(sleep.phase == "sleeping" and tucked() and facing_off() < deg_to_rad(0.5), "Back to sleep mid-wake did not settle at %s" % progress)
		sleep.activity()
		advance(2.0)
		check(sleep.phase == "awake" and normal_pose(), "Wake after a second nap did not finish at %s" % progress)

	# turn.gd hands over: a glance under way fades out while she goes down, and comes back after.
	widget.turn.glance_side(1.0, 4.0)
	sleep.begin_sleep()
	for i in 40:
		await process_frame
	check(absf(widget.turn.yaw) < deg_to_rad(1.0), "A glance should not turn her while she goes to sleep (%.1f)" % rad_to_deg(widget.turn.yaw))
	sleep.activity()
	await create_timer(2.0).timeout
	check(sleep.phase == "awake", "Woke with real frames")

	# Reduced motion: she folds and settles where she is, no hop, no turn, no wobble.
	sleep.reduced_motion = 1
	seen.merge({"top": -1.0, "low": 1.0, "spun": 0.0, "tilt": 0.0, "last_yaw": 0.0, "window": true, "tucked_in_air": false, "kink": 0.0}, true)
	seen.erase("last")
	seen.erase("step")
	sleep.begin_sleep()
	advance(2.0, watch)
	check(sleep.phase == "sleeping" and tucked(), "Reduced motion should still tuck her in")
	check(seen.top < 0.005 and seen.spun < 0.001 and seen.tilt < 0.001, "Reduced motion should not hop, turn or wobble")
	sleep.activity()
	advance(0.5, watch)
	sleep.begin_sleep()
	advance(2.0, watch)
	check(sleep.phase == "sleeping", "Reduced motion: back to sleep mid-wake")
	sleep.activity()
	advance(1.5, watch)
	check(sleep.phase == "awake" and normal_pose(), "Reduced motion should wake to the normal pose")
	check(seen.top < 0.005 and seen.spun < 0.001 and seen.kink < KINK, "Reduced motion wake should stay in place (%.3f, %.4f)" % [seen.top, seen.kink])
	sleep.reduced_motion = -1

	# Music wakes, then dance begins. A newer transient must retain the music rest state.
	sleep_now()
	widget.perform({"state": "dancing"})
	check(sleep.phase == "waking", "Music should wake Strawberry")
	widget.perform({"state": "thinking"})
	advance(2.0)
	check(widget.state == "thinking" and widget.rest_state == "dancing", "Music state lost during wake")
	widget.set_state("dancing")
	sleep.quiet_elapsed = 1000.0
	sleep.tick(1.0)
	check(sleep.phase == "awake", "Must not sleep during music")
	for state in ["listening", "thinking", "talking"]:
		widget.set_state(state)
		sleep.quiet_elapsed = 1000.0
		sleep.tick(1.0)
		check(sleep.phase == "awake", "Must not sleep while " + state)
	widget.set_state("idle")
	sleep_now()
	var count: int = widget.one_shots_played
	widget.perform({"state": "idle", "anim": "notify_perk"})
	check(widget.one_shots_played == count, "Notification should wait for stand-up")
	sleep.begin_sleep()
	check(sleep.phase == "waking", "A waiting notification keeps her getting up")
	advance(1.7)
	check(widget.one_shots_played == count + 1, "Notification lost during wake")
	advance(1.5)
	# Disabled and unavailable monitoring never trigger an automatic nap.
	sleep.set_delay(0.0)
	sleep.quiet_elapsed = 1000.0
	sleep.tick(1.0)
	check(sleep.phase == "awake", "Never preference ignored")
	sleep.set_delay(10.0)
	var cfg := ConfigFile.new()
	cfg.load(widget.settings_path())
	check(cfg.get_value("sleep", "after_minutes") == 10.0, "Sleep delay not saved")
	sleep.accept_idle({"seconds": null})
	sleep.quiet_elapsed = 1000.0
	sleep.tick(1.0)
	check(sleep.phase == "awake", "Unavailable monitor must not guess inactivity")
	widget.menu._refresh()
	check(widget.menu.sleep_menu.is_item_checked(2), "Menu check does not reflect delay")
	var path: String = widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(path)
	report.merge({"passed": failures.is_empty(), "failures": failures, "breath_min": minimum, "breath_max": maximum,
		"threshold_and_inhibitors": true, "tuck_and_spin": true, "interruptions": true, "reduced_motion": true,
		"music_and_notification_wake": true, "settings": true})
	var out := FileAccess.open("res://sleep_checks.json", FileAccess.WRITE)   # none in the exported binary
	if out:
		out.store_string(JSON.stringify(report, "\t"))
	print(JSON.stringify(report))
	print("sleep checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
