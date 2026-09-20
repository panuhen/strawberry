extends SceneTree
## End-to-end check of Phase 1 against a running strawberryd (WIRING.md §11):
## the widget connects, /perform changes her state and fires one-shots, text shows in
## the bubble, speech ending returns her to idle, /event round-trips, bad blobs are 400.
##
## godot --headless --path widget --script res://validate_widget.gd -- --daemon=http://127.0.0.1:8770 --ws=ws://127.0.0.1:8770/ws

var daemon_url := "http://127.0.0.1:8770"
var failures: Array[String] = []
var report: Dictionary = {"godot_version": Engine.get_version_info().string}
var widget: Node3D
var http: HTTPRequest

func _initialize() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--daemon="):
			daemon_url = arg.trim_prefix("--daemon=")
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

	finish()

func finish() -> void:
	report["failures"] = failures
	report["passed"] = failures.is_empty()
	report["performances"] = widget.performances if widget else 0
	var file := FileAccess.open("res://widget_checks.json", FileAccess.WRITE)
	file.store_string(JSON.stringify(report, "\t"))
	file.close()
	print("widget checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
