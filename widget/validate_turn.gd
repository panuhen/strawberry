extends SceneTree
## Turning and the location-aware tilt (WIRING.md §13): the place on the monitor turns and tilts
## her, the setting turns it off and persists, glances and the peek's turn come back to face the
## user, the dance turns on the beat, the pupils make up for her turn, and the click-through hull
## holds her silhouette at every turn. Real widget, isolated settings, no daemon.
##
## godot --headless --path widget --script res://validate_turn.gd
class TestWidget:
	extends "res://widget.gd"
	func settings_path() -> String:
		return "user://turn_test_%s.cfg" % OS.get_process_id()
	func setup_ws() -> void:
		pass
	func setup_sleep() -> void:
		super.setup_sleep()
		sleeper.monitor_enabled = false

var failures: Array[String] = []
var report := {}
var widget: TestWidget

func _initialize() -> void:
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

func wait(seconds: float) -> void:
	await create_timer(seconds).timeout

func deg(radians: float) -> float:
	return snappedf(rad_to_deg(radians), 0.1)

func run() -> void:
	widget = TestWidget.new()
	root.add_child(widget)
	await wait(0.2)
	var turn: Node = widget.turn
	check(widget.turn_to_screen and turn.enabled, "Turn toward the screen should be on by default")
	widget.set_state("thinking")   # no idle drift while the place is measured
	await wait(1.6)

	# 1. The place on the monitor: a side edge turns her toward the middle, the top shows her from
	# below (top tipped away), the bottom from above; the middle is neutral.
	var places := {"left": Vector2(-1, 0), "right": Vector2(1, 0), "top": Vector2(0, -1), "bottom": Vector2(0, 1), "middle": Vector2.ZERO}
	var seen := {}
	for name in places:
		turn.location_override = places[name]
		await wait(1.2)
		seen[name] = Vector2(turn.yaw, turn.pitch)
		report["place_" + name] = [deg(turn.yaw), deg(turn.pitch)]
	check(seen.left.x > deg_to_rad(4.0) and seen.right.x < -deg_to_rad(4.0), "a side edge should turn her toward the middle: %s / %s" % [report.place_left, report.place_right])
	check(seen.top.y > deg_to_rad(3.0) and seen.bottom.y < -deg_to_rad(3.0), "top should tip her away, bottom toward: %s / %s" % [report.place_top, report.place_bottom])
	check(seen.middle.length() < deg_to_rad(0.5), "the middle should be neutral: %s" % [report.place_middle])
	for name in seen:
		check(absf(seen[name].x) <= turn.PLACE_YAW + 0.001 and absf(seen[name].y) <= turn.PLACE_PITCH + 0.001, "the place's turn stays a few degrees")
	# Smooth while dragging: a jump of place eases over, it never snaps.
	turn.location_override = Vector2(-1, -1)
	await process_frame
	await process_frame
	check(turn.yaw < deg_to_rad(2.0), "a new place should be eased into, not snapped to (%.1f°)" % rad_to_deg(turn.yaw))
	await wait(1.2)
	check(widget.model.transform.basis.get_euler().length() > deg_to_rad(4.0), "the model node should carry the turn")
	# The setting turns it off, and persists.
	widget.set_turn_to_screen(false)
	await wait(1.2)
	report["place_off"] = [deg(turn.yaw), deg(turn.pitch)]
	check(Vector2(turn.yaw, turn.pitch).length() < deg_to_rad(0.3), "off: she should face the user wherever she is")
	var cfg := ConfigFile.new()
	cfg.load(widget.settings_path())
	check(cfg.get_value("window", "turn_to_screen", true) == false, "the setting should be saved")
	widget.menu._refresh()
	check(not widget.menu.is_item_checked(widget.menu.get_item_index(widget.menu.TURN_TO_SCREEN)), "the menu should show it off")
	widget.menu._on_pressed(widget.menu.TURN_TO_SCREEN)
	check(widget.turn_to_screen, "the menu item should turn it back on")
	turn.location_override = Vector2.ZERO
	await wait(1.2)

	# 2. A glance (a notification arriving: headless she picks a side) turns 3/4 and comes back.
	widget.perform({"state": "idle", "anim": "notify_perk"})
	check(turn.glances == 1, "a notification should start a glance")
	var peak := 0.0
	var waited := 0.0
	while waited < 2.6:
		await process_frame
		waited += widget.get_process_delta_time()
		peak = maxf(peak, absf(turn.yaw - turn.place.x))
	report["glance_peak_deg"] = deg(peak)
	check(peak > deg_to_rad(9.0) and peak <= turn.MAX_YAW + 0.001, "a glance should turn about 12°, got %.1f°" % rad_to_deg(peak))
	widget.set_state("thinking")
	await wait(1.8)   # the idle drift eases out too
	check(absf(turn.yaw) < deg_to_rad(0.5), "she should face the user again after a glance, at %.1f°" % rad_to_deg(turn.yaw))

	# 3. The peek turns and comes back.
	widget.reactions.play("peek")
	var peek_peak := 0.0
	while widget.reactions.recipe != "":
		await process_frame
		peek_peak = maxf(peek_peak, absf(turn.yaw))
	report["peek_turn_deg"] = deg(peek_peak)
	check(peek_peak > deg_to_rad(7.0), "the peek should turn her, got %.1f°" % rad_to_deg(peek_peak))
	await process_frame
	check(absf(turn.yaw) < deg_to_rad(0.5), "the peek should end facing the user")

	# 4. Idle drift: slow and small.
	widget.set_state("idle")
	var drift := 0.0
	waited = 0.0
	while waited < 6.0:
		await process_frame
		waited += widget.get_process_delta_time()
		drift = maxf(drift, absf(turn.yaw))
	report["idle_drift_deg"] = deg(drift)
	check(drift > deg_to_rad(0.5) and drift <= turn.DRIFT_YAW + deg_to_rad(0.1), "idle should drift a little, got %.1f°" % rad_to_deg(drift))

	# 5. Dance: yaw on the beat (rave), eased in.
	widget.set_state("dancing")
	var now := Time.get_unix_time_from_system()
	var techno := {"bpm": 130.0, "period_s": 60.0 / 130.0, "confidence": 0.8, "next_beat": now + 0.2,
		"evenness": 0.7, "low_ratio": 0.45, "density": 3.0, "loudness_db": -18.0}
	widget.dance.set_tempo(techno)
	widget.dance.set_tempo(techno)
	var beat_yaw := 0.0
	waited = 0.0
	while waited < 2.0:
		await process_frame
		waited += widget.get_process_delta_time()
		beat_yaw = maxf(beat_yaw, absf(widget.dance.yaw))
	report["dance_yaw_deg"] = deg(beat_yaw)
	check(beat_yaw > deg_to_rad(3.0), "the rave should turn on the beat, got %.1f°" % rad_to_deg(beat_yaw))
	widget.dance.set_tempo({"silent": true})
	widget.set_state("thinking")
	await wait(0.8)
	check(absf(widget.dance.yaw) < 0.001, "the dance's turn should end with the beat")

	# 6. The pupils make up for her turn: a point straight ahead stays looked at.
	turn.location_override = Vector2(-1, 0)
	await wait(1.5)
	var ahead: Vector2 = widget.gaze.target_for(Vector2(190, 300))
	report["gaze_compensation"] = [deg(turn.yaw), deg(ahead.x)]
	check(turn.yaw > deg_to_rad(4.0) and ahead.x < -deg_to_rad(2.0), "turned right, the pupils should look back left toward the user (body %.1f°, pupils %.1f°)" % [rad_to_deg(turn.yaw), rad_to_deg(ahead.x)])

	# 7. The click-through hull holds her posed silhouette at the widest turns and tilts.
	var hull := Geometry2D.convex_hull(widget.hull_points())
	var outside := 0
	var worst := 0.0
	for y in [-1.0, 1.0]:
		for p in [-1.0, 1.0]:
			turn.yaw = y * turn.MAX_YAW
			turn.pitch = p * turn.MAX_PITCH
			turn.apply()
			for point in widget.body_points():
				if not Geometry2D.is_point_in_polygon(point, hull):
					outside += 1
					var closest := Geometry2D.get_closest_point_to_segment(point, hull[0], hull[1])
					for i in hull.size():
						var c := Geometry2D.get_closest_point_to_segment(point, hull[i], hull[(i + 1) % hull.size()])
						if c.distance_to(point) < closest.distance_to(point):
							closest = c
					worst = maxf(worst, closest.distance_to(point))
	report["hull_points_outside"] = outside
	report["hull_worst_outside_px"] = snappedf(worst, 0.1)
	# body_points takes the bones' full reach (open claws, raised arms); the hull's padding covers it.
	check(worst < widget.PASSTHROUGH_PADDING, "her turned silhouette should stay inside the padded hull (worst %.1f px out)" % worst)

	var path: String = widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(path)
	report["passed"] = failures.is_empty()
	report["failures"] = failures
	print(JSON.stringify(report))
	print("turn checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
