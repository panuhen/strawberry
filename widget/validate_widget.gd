extends SceneTree
## End-to-end check of Phase 1 against a running strawberryd (WIRING.md §11):
## the widget connects, /perform changes her state and fires one-shots, text shows in
## the bubble, speech ending returns her to idle, /event round-trips, bad blobs are 400.
##
## godot --headless --path widget --script res://validate_widget.gd -- --daemon=http://127.0.0.1:8770 --ws=ws://127.0.0.1:8770/ws
## The exported binary has no --script (release templates drop it):
## strawberry-widget --headless -- --acceptance=res://validate_widget.gd --daemon=... --ws=... --report=/tmp/checks.json

var daemon_url := "http://127.0.0.1:8770"
var report_path := "res://widget_checks.json"   # an exported binary's res:// is read-only: pass --report=
var failures: Array[String] = []
var report: Dictionary = {"godot_version": Engine.get_version_info().string,
	"widget_version": preload("res://paths.gd").version(), "exported": OS.has_feature("template")}
var widget: Node3D
var http: HTTPRequest

func _initialize() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--daemon="):
			daemon_url = arg.trim_prefix("--daemon=")
		elif arg.begins_with("--report="):
			report_path = arg.trim_prefix("--report=")
	report["daemon"] = daemon_url
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

func post(path: String, body: Dictionary) -> Array:
	var err := http.request(daemon_url + path, ["Content-Type: application/json"], HTTPClient.METHOD_POST, JSON.stringify(body))
	if err != OK:
		return [err, 0, {}]
	var result: Array = await http.request_completed
	var parsed: Variant = JSON.parse_string(result[3].get_string_from_utf8())
	return [result[0], result[1], parsed if parsed is Dictionary else {}]

func get_json(path: String) -> Array:
	var err := http.request(daemon_url + path)
	if err != OK:
		return [err, 0, {}]
	var result: Array = await http.request_completed
	var parsed: Variant = JSON.parse_string(result[3].get_string_from_utf8())
	return [result[0], result[1], parsed if parsed is Dictionary else {}]

func wait(seconds: float) -> void:
	await create_timer(seconds).timeout

## Bounded: a script error inside the widget must fail the run, not hang it.
func wait_speech_end(timeout := 15.0) -> void:
	var waited := 0.0
	while widget.bubble.speaking and waited < timeout:
		await wait(0.1)
		waited += 0.1
	check(not widget.bubble.speaking, "speech should end within %.0fs" % timeout)

func run() -> void:
	widget = (load("res://widget.tscn") as PackedScene).instantiate()
	root.add_child(widget)
	http = HTTPRequest.new()
	root.add_child(http)

	# 1. Widget connects on its own.
	var waited := 0.0
	while not widget.ws.is_open() and waited < 6.0:
		await wait(0.1)
		waited += 0.1
	check(widget.ws.is_open(), "widget did not connect to " + widget.ws_url)
	report["connect_seconds"] = snappedf(waited, 0.1)
	if not widget.ws.is_open():
		finish()
		return

	var health := await get_json("/health")
	check(health[1] == 200 and int(health[2].get("widgets", 0)) >= 1, "daemon /health should count this widget")

	# 2. A bare state change.
	var thinking := await post("/perform", {"state": "thinking"})
	check(thinking[1] == 200 and int(thinking[2].get("sent", 0)) == 1, "/perform thinking should reach one widget")
	await wait(0.3)
	check(widget.state == "thinking", "widget state should be thinking, was " + widget.state)
	check(widget.player.assigned_animation == "think_loop", "clip should be think_loop, was " + widget.player.assigned_animation)

	# 3. A full talking performance: one-shot, bubble, auto-return to idle.
	var line := "Did someone say my name?"
	var talk := await post("/perform", {"state": "talking", "anim": "alert_snap", "text": line, "emotion": "alert"})
	check(talk[1] == 200 and int(talk[2].get("sent", 0)) == 1, "/perform talking should reach one widget")
	await wait(0.25)
	check(widget.state == "talking", "state should be talking during speech")
	check(widget.player.assigned_animation == "alert_snap", "one-shot alert_snap should be playing, was " + widget.player.assigned_animation)
	check(widget.bubble.visible and widget.bubble.speaking, "bubble should be visible and speaking")
	check(widget.bubble.line == line, "bubble should hold the sent line")
	var alert_length: float = widget.player.get_animation("alert_snap").length
	await wait(alert_length + 0.3)
	check(widget.one_shot == "", "one-shot should be cleared after it finishes")
	check(widget.player.assigned_animation == "talk_base", "talk_base should resume after the one-shot, was " + widget.player.assigned_animation)
	var expected_speech: float = widget.bubble.heuristic_duration(line) + 0.9 + 0.35
	report["expected_speech_seconds"] = snappedf(expected_speech, 0.01)
	var spoke := 0.0
	while widget.bubble.speaking and spoke < expected_speech + 3.0:
		await wait(0.1)
		spoke += 0.1
	check(not widget.bubble.speaking, "speech should have ended by now")
	report["observed_speech_seconds"] = snappedf(spoke + alert_length + 0.55, 0.1)
	check(not widget.bubble.speaking and not widget.bubble.visible, "bubble should hide when speech ends")
	await wait(0.3)
	check(widget.state == "idle", "state should auto-return to idle after speech, was " + widget.state)
	check(widget.player.assigned_animation == "idle_loop", "clip should be idle_loop after speech, was " + widget.player.assigned_animation)

	# 4. The doorway path: /event -> reactor -> perform -> widget.
	var before: int = widget.performances
	var event := await post("/event", {"source": "git", "title": "strawberry", "body": "Add websocket"})
	check(event[1] == 200, "/event should be accepted")
	var performance: Dictionary = event[2].get("performance", {})
	check(performance.get("state", "") == "talking", "/event reaction should be a talking performance")
	check(String(performance.get("text", "")).contains("strawberry"), "/event reaction text should mention the repo")
	await wait(0.25)
	check(widget.performances == before + 1, "widget should have performed the /event reaction")
	check(widget.bubble.line == performance.get("text", ""), "bubble should show the reaction text")
	report["event_reaction"] = performance

	# 5. Bad input is rejected and leaves her alone.
	var perf_before: int = widget.performances
	var bad := await post("/perform", {"state": "zoomies"})
	check(bad[1] == 400, "bad state should be a 400, was %d" % bad[1])
	await wait(0.2)
	check(widget.performances == perf_before, "rejected blob must not reach the widget")

	# 6. Dancing is a valid state on v2. It arrives while the /event one-shot is still
	#    playing: the state is taken immediately, the loop switches once the one-shot ends.
	await post("/perform", {"state": "dancing"})
	await wait(0.2)
	check(widget.state == "dancing", "state should be dancing right away, was " + widget.state)
	var shot_wait := 0.0
	while widget.one_shot != "" and shot_wait < 3.0:
		await wait(0.1)
		shot_wait += 0.1
	await wait(0.3)
	check(widget.one_shot == "", "one-shot should finish on its own")
	check(widget.player.assigned_animation == "dance_loop", "dancing should play dance_loop after the one-shot, was " + widget.player.assigned_animation)
	report["one_shot_then_dance_seconds"] = snappedf(shot_wait, 0.1)

	# 7. Talking while dancing returns to dancing, not idle (the media doorway relies on this).
	var short_line := "Nice track."
	await post("/perform", {"state": "talking", "text": short_line, "emotion": "happy"})
	await wait(0.2)
	check(widget.state == "talking" and widget.rest_state == "dancing", "rest state should stay dancing while she talks")
	var back := 0.0
	while widget.bubble.speaking and back < 6.0:
		await wait(0.1)
		back += 0.1
	await wait(0.3)
	check(widget.state == "dancing", "after talking she should return to dancing, was " + widget.state)
	check(widget.player.assigned_animation == "dance_loop", "clip should be dance_loop again, was " + widget.player.assigned_animation)
	await post("/perform", {"state": "idle"})
	await wait(0.2)
	check(widget.rest_state == "idle", "idle should reset the rest state")

	# 7b. A long line must stay inside the window: the bubble grows upward from y 0.98 and
	#     the camera view must contain its top edge.
	var long_line := "James wants to know if you are still on for tonight, and whether you remembered the cake, the candles and the good plates!"
	await post("/perform", {"state": "talking", "text": long_line, "emotion": "happy"})
	await wait(float(widget.bubble.heuristic_duration(long_line)) + 0.2)  # fully revealed
	var view_top: float = widget.view_top()
	var bubble_top: float = (widget.bubble.global_transform * widget.bubble.get_aabb()).end.y
	report["bubble_lines"] = widget.bubble.estimate_lines(long_line, widget.bubble.font_size)
	report["bubble_measured_height"] = snappedf(widget.bubble.measured_height(long_line, widget.bubble.font_size), 0.01)
	report["bubble_actual_height"] = snappedf(bubble_top - widget.bubble.global_position.y, 0.01)
	report["bubble_font_size"] = widget.bubble.font_size
	report["bubble_top_y"] = snappedf(bubble_top, 0.01)
	report["view_top_y"] = snappedf(view_top, 0.01)
	check(bubble_top <= view_top, "bubble top %.2f must be inside the view (top %.2f)" % [bubble_top, view_top])
	check(widget.bubble.font_size >= widget.bubble.MIN_FONT_SIZE, "font must not shrink below the minimum")
	await wait_speech_end()
	await post("/perform", {"state": "talking", "text": "Short.", "emotion": "neutral"})
	await wait(0.2)
	check(widget.bubble.font_size == widget.bubble.BASE_FONT_SIZE, "a short line should use the base font size again")
	await wait_speech_end()

	# 8. A message: wave recipe layered on the clip, app icon badge, window hop (no-op headless).
	var skeleton := widget.model.find_child("Skeleton3D", true, false) as Skeleton3D
	var claw_i := skeleton.find_bone("claw_arm_L")
	var claw_before: Quaternion = skeleton.get_bone_global_pose(claw_i).basis.get_rotation_quaternion()
	var icon: String = preload("res://paths.gd").on_disk("res://capture_phase1.png")
	var wave := await post("/perform", {"state": "talking", "reaction": "wave", "hop": true, "icon": icon, "text": "James says hi", "emotion": "happy"})
	check(wave[1] == 200, "/perform with reaction/icon/hop should be accepted")
	await wait(0.6)
	check(widget.reactions.recipe == "wave", "wave should be active, was %s" % widget.reactions.recipe)
	check(widget.reactions.applied_frames > 0, "the skeleton modifier should be running")
	var claw_now: Quaternion = skeleton.get_bone_global_pose(claw_i).basis.get_rotation_quaternion()
	var lift := rad_to_deg((claw_before.inverse() * claw_now).get_angle())
	report["wave_claw_lift_deg"] = snappedf(lift, 0.1)
	check(lift > 20.0, "wave should lift claw_arm_L by more than 20 degrees, got %.1f" % lift)
	check(widget.badge.visible and widget.badge.texture != null, "badge should show the app icon")
	var wide := 0.0
	for eye in widget.blink_controller.eyes:
		wide = maxf(wide, eye.get_blend_shape_value(eye.find_blend_shape_by_name("eye_wide")))
	check(wide > 0.3, "wave should widen the eyes, got %.2f" % wide)
	await wait(1.2)
	check(widget.reactions.recipe == "", "wave should finish on its own")
	var claw_after: Quaternion = skeleton.get_bone_global_pose(claw_i).basis.get_rotation_quaternion()
	var residual := rad_to_deg((claw_before.inverse() * claw_after).get_angle())
	report["wave_claw_residual_deg"] = snappedf(residual, 0.1)
	check(residual < 8.0, "claw should settle back after the wave, residual %.1f" % residual)
	await wait_speech_end()
	await wait(0.2)
	check(not widget.badge.visible, "badge should hide with the bubble")

	# 9. A burst: double hop plays notify_perk twice.
	var shots_before: int = widget.one_shots_played
	await post("/perform", {"state": "talking", "reaction": "double_hop", "text": "Seven pings!", "emotion": "alert"})
	var hop_len: float = widget.player.get_animation("notify_perk").length
	await wait(hop_len * 2.0 + 0.6)
	check(widget.one_shots_played == shots_before + 2, "double_hop should play notify_perk twice, played %d" % (widget.one_shots_played - shots_before))
	check(widget.one_shot == "", "one-shots should be done after the double hop")
	await wait_speech_end()

	# 10. Every recipe runs and finishes without error.
	for name in ["peek", "shiver", "nod"]:
		await post("/perform", {"state": "idle", "reaction": name})
		await wait(0.3)
		check(widget.reactions.recipe == name, "%s should be active" % name)
		await wait(float(widget.reactions.DURATIONS[name]) + 0.3)
		check(widget.reactions.recipe == "", "%s should finish" % name)
	report["reactions_completed"] = widget.reactions.completed
	var bad_reaction := await post("/perform", {"state": "idle", "reaction": "backflip"})
	check(bad_reaction[1] == 400, "unknown reaction should be a 400")

	# 11. Audio: a wav plays through the analysed bus, the claws open to it, and close after.
	var wav_path := make_test_wav(1.6)
	widget.set_muted(false)
	widget.set_quiet_until(0.0)
	var claw_idle: float = widget.speech.claw_value()
	check(absf(claw_idle) < 0.01, "claw_open should be 0 before speech, was %.2f" % claw_idle)
	await post("/perform", {"state": "talking", "text": "Testing, one two three.", "audio": wav_path})
	await wait(0.6)
	check(widget.speech.playing, "speech wav should be playing")
	var claw_mid: float = widget.speech.claw_value()
	report["speech_claw_mid"] = snappedf(claw_mid, 0.01)
	report["speech_level_mid"] = snappedf(widget.speech.level, 0.01)
	check(claw_mid > 0.05, "claw_open should follow the audio, was %.2f" % claw_mid)
	# The bubble reveal is timed to the wav (1.6 s), so it is still revealing at 1.2 s
	# where the text-length heuristic (1.9 s for this line) would also be; check the length
	# the widget used instead.
	var wav_len: float = widget.speech.stream.get_length() if widget.speech.stream else 0.0
	report["speech_wav_seconds"] = snappedf(wav_len, 0.01)
	check(absf(wav_len - 1.6) < 0.02, "widget should read the wav length")
	await wait(1.4)
	check(not widget.speech.playing, "speech wav should have finished")
	await wait(0.1)
	var claw_end: float = widget.speech.claw_value()
	check(absf(claw_end) < 0.01, "claw_open should return to 0 after speech, was %.2f" % claw_end)
	report["speech_peak_level"] = snappedf(widget.speech.peak_level, 0.01)
	await wait_speech_end()
	check(widget.state == widget.rest_state, "she should rest after a spoken line")
	var bad_audio := await post("/perform", {"state": "talking", "text": "x", "audio": "/nonexistent/line.wav"})
	check(bad_audio[1] == 400, "missing audio file should be a 400")

	# 12. Right-click menu exists; mute keeps the bubble but drops the sound.
	check(widget.menu != null and widget.menu.item_count >= 8, "menu should have its items")
	report["menu_items"] = widget.menu.item_count if widget.menu else 0
	var ids := {}
	for i in widget.menu.item_count:
		if not widget.menu.is_item_separator(i):
			ids[widget.menu.get_item_id(i)] = true
	check(ids.size() == widget.menu.item_count - 2, "menu item ids should be unique")
	widget.menu._refresh()
	check(widget.menu.is_item_checked(widget.menu.get_item_index(widget.menu.ALWAYS_ON_TOP)), "always-on-top row should show checked")
	check(widget.menu.get_item_text(widget.menu.get_item_index(widget.menu.ALWAYS_ON_TOP)) == "Always on top", "check mark should be on the right row")
	widget.set_muted(true)
	await post("/perform", {"state": "talking", "text": "Muted line.", "audio": wav_path})
	await wait(0.4)
	check(widget.bubble.speaking, "muted: bubble should still show")
	check(not widget.speech.playing, "muted: wav should not play")
	widget.set_muted(false)
	widget.set_quiet_until(Time.get_unix_time_from_system() + 60.0)
	await post("/perform", {"state": "talking", "text": "Quiet line.", "audio": wav_path})
	await wait(0.4)
	check(not widget.speech.playing, "quiet hour: wav should not play")
	widget.set_quiet_until(0.0)
	widget.set_voice_volume(0.5)
	check(absf(widget.speech.volume_db - linear_to_db(0.5)) < 0.01, "voice volume should set the player's dB")
	widget.set_voice_volume(1.0)
	await wait_speech_end()
	DirAccess.remove_absolute(wav_path)

	# 13. Pupils follow the cursor: the pupil shader sits on both eyes' ink surface and the
	# gaze swings with a pinned cursor position, opposite ways for the two window edges.
	for side in ["L", "R"]:
		var mat := widget.gaze.meshes[side].get_surface_override_material(1) as ShaderMaterial
		check(mat != null and mat.shader == widget.gaze.PupilShader, "eye %s should carry the pupil shader" % side)
	widget.gaze.look_override = Vector2(0.0, 300.0)
	await wait(0.6)
	var gaze_left: Vector2 = widget.gaze.gaze
	widget.gaze.look_override = Vector2(380.0, 300.0)
	await wait(0.6)
	var gaze_right: Vector2 = widget.gaze.gaze
	widget.gaze.look_override = Vector2(190.0, 0.0)
	await wait(0.6)
	var gaze_up: Vector2 = widget.gaze.gaze
	report["gaze_left"] = [snappedf(gaze_left.x, 0.01), snappedf(gaze_left.y, 0.01)]
	report["gaze_right"] = [snappedf(gaze_right.x, 0.01), snappedf(gaze_right.y, 0.01)]
	report["gaze_up"] = [snappedf(gaze_up.x, 0.01), snappedf(gaze_up.y, 0.01)]
	check(absf(gaze_left.x) > 0.15 and signf(gaze_left.x) == -signf(gaze_right.x), "gaze yaw should swing opposite ways for the two edges")
	check(gaze_up.y > 0.2, "gaze pitch should rise for a cursor above her, was %.2f" % gaze_up.y)
	widget.gaze.look_override = Vector2(-1, -1)
	await wait(0.6)
	check(widget.gaze.gaze.length() < 0.05, "headless with no cursor: gaze should relax to centre")

	# 14. Beat-driven dance styles: /tempo picks a style while dancing, the clip runs at the
	# music's speed, moves land on the beat, and everything resets when the beat is gone.
	await post("/perform", {"state": "dancing"})
	await wait(0.3)
	var now := Time.get_unix_time_from_system()
	var techno := {"bpm": 130.0, "period_s": 60.0 / 130.0, "confidence": 0.8, "next_beat": now + 0.4,
		"evenness": 0.7, "low_ratio": 0.45, "density": 3.0, "loudness_db": -18.0}
	await post("/tempo", techno)
	await post("/tempo", techno)
	await wait(0.3)
	check(widget.dance.style == "rave", "130 bpm even kick should be rave, got %s" % widget.dance.style)
	check(absf(widget.player.speed_scale - 130.0 / 119.0) < 0.02, "clip should run at the music's speed, got %.2f" % widget.player.speed_scale)
	report["dance_speed_rave"] = snappedf(widget.player.speed_scale, 0.01)
	check(widget.dance.applied, "rave should be layering moves")
	var groove := {"bpm": 92.0, "period_s": 60.0 / 92.0, "confidence": 0.7, "next_beat": now + 0.5,
		"evenness": 0.4, "low_ratio": 0.35, "density": 2.0, "loudness_db": -20.0}
	await post("/tempo", groove)
	check(widget.dance.style == "rave", "one estimate should not flip the style yet")
	await post("/tempo", groove)
	await wait(0.2)
	check(widget.dance.style == "groove", "92 bpm with low end should be groove, got %s" % widget.dance.style)
	var rules := {"sway": {"bpm": 70.0, "confidence": 0.9, "evenness": 0.5, "low_ratio": 0.3, "density": 2.0, "loudness_db": -20.0},
		"headbang": {"bpm": 160.0, "confidence": 0.6, "evenness": 0.3, "low_ratio": 0.2, "density": 5.0, "loudness_db": -12.0},
		"bounce": {"bpm": 112.0, "confidence": 0.6, "evenness": 0.3, "low_ratio": 0.15, "density": 3.0, "loudness_db": -16.0},
		"rave": {"bpm": 140.0, "confidence": 0.9, "evenness": 0.8, "low_ratio": 0.5, "density": 4.0, "loudness_db": -10.0}}
	for name in rules:
		check(widget.dance.choose(rules[name]) == name, "rule table: expected %s" % name)
	await post("/tempo", {"silent": true})
	await wait(0.2)
	check(not widget.dance.applied, "silence should stop the layers")
	check(absf(widget.player.speed_scale - 1.0) < 0.001, "clip speed should return to 1")
	report["dance_styles_seen"] = widget.dance.styles_seen.keys()
	await post("/perform", {"state": "idle"})

	# 17. Typing to her: the glass box opens with focus, a submitted line round-trips as a voice event.
	check(widget.type_box != null and not widget.type_box.visible, "type box should start hidden")
	check(widget.menu.get_item_index(widget.menu.TYPE_BOX) >= 0, "menu should offer the type box")
	widget.open_type_box()
	check(widget.type_box.visible and widget.type_box.field.has_focus(), "type box should open with focus")
	check(widget.type_box.field.editable, "type box should be editable while she is idle and connected")
	var typed_before: int = widget.performances
	widget.type_box.submit("hello from the keyboard")
	check(widget.type_box.sent == 1 and widget.type_box.field.text == "", "type box should clear after sending")
	waited = 0.0
	while widget.performances == typed_before and waited < 10.0:
		await wait(0.1)
		waited += 0.1
	check(widget.performances == typed_before + 1, "a typed line should come back as one performance")
	check(widget.state == "talking" and widget.bubble.speaking, "she should answer the typed line")
	var ledger := await get_json("/health")
	var turns: Array = ledger[2].get("ledger", [])
	check(not turns.is_empty() and turns[-1].get("said", "") == "hello from the keyboard", "the typed line should be in the ledger like a spoken one")
	widget.type_box.submit("")
	check(widget.type_box.sent == 1, "an empty line should not be sent")
	widget.close_type_box()
	check(not widget.type_box.visible and not widget.type_box.field.has_focus(), "type box should close and drop focus")
	await wait_speech_end()

	finish()

## A 1 kHz tone with a syllable-like 5 Hz amplitude wobble, saved where the daemon can see it.
func make_test_wav(seconds: float) -> String:
	var rate := 22050
	var frames := int(seconds * rate)
	var bytes := PackedByteArray()
	bytes.resize(frames * 2)
	for i in frames:
		var t := float(i) / rate
		var envelope := 0.55 + 0.45 * sin(TAU * 5.0 * t)
		var sample := int(0.6 * envelope * sin(TAU * 1000.0 * t) * 32767.0)
		bytes.encode_s16(i * 2, sample)
	var stream := AudioStreamWAV.new()
	stream.format = AudioStreamWAV.FORMAT_16_BITS
	stream.mix_rate = rate
	stream.stereo = false
	stream.data = bytes
	var path := OS.get_user_data_dir().path_join("check_tone.wav")
	stream.save_to_wav(path)
	return path

func finish() -> void:
	report["failures"] = failures
	report["passed"] = failures.is_empty()
	report["performances"] = widget.performances if widget else 0
	var file := FileAccess.open(report_path, FileAccess.WRITE)
	if file:
		file.store_string(JSON.stringify(report, "\t"))
		file.close()
	else:
		push_error("could not write %s: %s" % [report_path, error_string(FileAccess.get_open_error())])
		failures.append("report not written to " + report_path)
	print("widget checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
