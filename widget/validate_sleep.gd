extends SceneTree
## Real widget, isolated settings and no daemon traffic. Run headless or with -- --capture-dir=...
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

var failures: Array[String] = []
var widget: TestWidget
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

func advance(seconds: float) -> void:
	for i in ceili(seconds * 60.0):
		widget.player.advance(1.0 / 60.0)
		widget.sleeper.tick(1.0 / 60.0)
		widget.blink_controller.advance_blink(1.0 / 60.0)
		widget.claw_controller.advance_gestures(1.0 / 60.0)
		widget.top_hat.update_pose(1.0 / 60.0)

func morph(name: String, key: String) -> float:
	var mesh: MeshInstance3D = widget.model.find_child(name, true, false)
	return mesh.get_blend_shape_value(mesh.find_blend_shape_by_name(key))

func capture(name: String) -> void:
	if capture_dir == "":
		return
	widget.player.pause()
	await process_frame
	await process_frame
	await RenderingServer.frame_post_draw
	root.get_texture().get_image().save_png(capture_dir.path_join(name + ".png"))
	widget.player.play()

func sleep_now() -> void:
	widget.sleeper.begin_sleep()
	advance(3.0)
	check(widget.sleeper.phase == "sleeping", "Descent should finish in sleep_loop")

func run() -> void:
	widget = TestWidget.new()
	widget.look_at = Vector2(190, 340)
	root.add_child(widget)
	await process_frame
	var sleep: Node = widget.sleeper
	check(widget.sleep_after_minutes == 5.0, "Default delay should be five minutes")
	for name in ["sleep_enter", "sleep_loop", "wake_up"]:
		check(widget.player.has_animation(name), "Missing clip " + name)
	check(widget.player.get_animation("sleep_loop").length >= 8.0, "Sleep breathing should take eight seconds")
	widget.set_top_hat(true)
	await capture("sleep_awake")
	# Both desktop inactivity and quiet widget time must reach the configured delay.
	sleep.idle_seconds = 600.0
	sleep.quiet_elapsed = 299.0
	sleep.tick(0.5)
	check(sleep.phase == "awake", "Slept before delay")
	sleep.tick(0.6)
	check(sleep.phase == "entering", "Did not sleep after inactivity threshold")
	advance(3.0)
	check(sleep.phase == "sleeping", "Sleep enter did not transition to loop")
	for side in ["L", "R"]:
		check(morph("mesh_eye_" + side, "blink") > 0.999, "Sleeping eye should stay shut")
		for i in range(1, 4):
			check(morph("mesh_leg_%s%d" % [side, i], "sleep_fold") > 0.999, "Leg should stay tucked")
	await capture("sleep_rest")
	var minimum := 1.0
	var maximum := 0.0
	for i in 480:
		advance(1.0 / 60.0)
		var value := morph("mesh_shell", "squash")
		minimum = minf(minimum, value)
		maximum = maxf(maximum, value)
		check(absf(value - morph("mesh_belly", "squash")) < 0.0001, "Belly detached during breath")
		check(morph("mesh_eye_L", "blink") > 0.999, "Automatic blink interfered with sleep")
	check(minimum < 0.13 and maximum > 0.21, "Relaxed breath missing")
	check(absf(widget.top_hat.lift) < 0.0001, "Sleeping hat should rest")
	sleep.activity()
	check(sleep.phase == "waking", "Input should start waking")
	advance(0.7)
	await capture("sleep_waking")
	advance(1.0)
	check(sleep.phase == "awake" and widget.player.assigned_animation == "idle_loop", "Wake should return to idle")
	check(morph("mesh_leg_L1", "sleep_fold") < 0.0001, "Leg fold leaked into idle")
	# Interrupt at several points; reversing the descent must not teleport the body.
	var skeleton: Skeleton3D = widget.model.find_child("Skeleton3D", true, false)
	for progress in [0.01, 0.25, 0.5, 0.9]:
		sleep.begin_sleep()
		advance(2.5 * progress)
		var before := skeleton.get_bone_pose_position(skeleton.find_bone("body"))
		sleep.activity()
		widget.player.advance(0.0)
		var after := skeleton.get_bone_pose_position(skeleton.find_bone("body"))
		check(before.distance_to(after) < 0.012, "Interrupted descent jumped at %s" % progress)
		advance(2.0)
		check(sleep.phase == "awake", "Interrupted wake did not finish")
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
	var report := {"passed": failures.is_empty(), "failures": failures, "breath_min": minimum, "breath_max": maximum,
		"threshold_and_inhibitors": true, "interrupted_descent": true, "music_and_notification_wake": true, "settings": true}
	FileAccess.open("res://sleep_checks.json", FileAccess.WRITE).store_string(JSON.stringify(report, "\t"))
	print(JSON.stringify(report))
	quit(0 if failures.is_empty() else 1)
