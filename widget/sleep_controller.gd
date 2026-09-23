extends Node
## Local visual sleep state. The daemon's idle/dancing contract remains unchanged.
## Incoming performances wait for the stand-up transition; audio starts afterwards.
const SLEEP_CLIPS := ["sleep_enter", "sleep_loop", "wake_up"]
var widget: Node3D
var phase := "awake"
var pending: Dictionary = {}
var idle_seconds := -1.0
var idle_source := "unavailable"
var quiet_elapsed := 0.0
var poll_elapsed := 0.0
var probe := Thread.new()
var monitor_enabled := true
var python_path: String = preload("res://paths.gd").python()
var helper_path := ""
var last_sample_at := -1.0
var sleep_started_at := 0.0

func setup(owner: Node3D) -> void:
	widget = owner
	process_priority = 80
	helper_path = preload("res://paths.gd").on_disk("res://desktop_idle.py")
	monitor_enabled = not widget.is_headless()
	widget.player.animation_finished.connect(animation_finished)

func _exit_tree() -> void:
	if probe.is_started():
		probe.wait_to_finish()

func read_idle() -> Dictionary:
	var output: Array = []
	if python_path == "":
		return {"seconds": null, "source": "unavailable"}
	var result := OS.execute(python_path, [helper_path], output)
	if result == 0 and not output.is_empty():
		var parsed: Variant = JSON.parse_string(str(output[0]))
		if parsed is Dictionary:
			return parsed
	return {"seconds": null, "source": "unavailable"}

func _process(delta: float) -> void:
	if probe.is_started() and not probe.is_alive():
		var sample: Dictionary = probe.wait_to_finish()
		accept_idle(sample)
	poll_elapsed += delta
	if monitor_enabled and (widget.sleep_after_minutes > 0.0 or phase != "awake") and poll_elapsed >= 1.0 and not probe.is_started():
		poll_elapsed = 0.0
		probe.start(read_idle)
	tick(delta)

func accept_idle(sample: Dictionary) -> void:
	var previous := idle_seconds
	var now := Time.get_ticks_msec() / 1000.0
	var gap := 0.0 if last_sample_at < 0.0 else now - last_sample_at
	last_sample_at = now
	idle_source = str(sample.get("source", "unavailable"))
	var seconds: Variant = sample.get("seconds")
	idle_seconds = float(seconds) if seconds is float or seconds is int else -1.0
	if idle_seconds < 0.0:
		activity()
	elif previous >= 0.0 and idle_seconds + 0.15 < previous + gap:
		quiet_elapsed = 0.0
		# Ignore the click that selected Sleep now when its delayed sample arrives.
		if now - idle_seconds > sleep_started_at + 0.1 and phase in ["entering", "sleeping"]:
			wake()

func can_sleep() -> bool:
	return widget.state == "idle" and widget.rest_state == "idle" and widget.one_shot == "" \
		and not widget.speech.playing and not widget.bubble.speaking \
		and widget.reactions.recipe == "" and not widget.dragging and not widget.menu.visible

func tick(delta: float) -> void:
	if not can_sleep():
		quiet_elapsed = 0.0
		if phase in ["entering", "sleeping"]:
			wake()
		return
	quiet_elapsed += delta
	if phase == "awake" and widget.sleep_after_minutes > 0.0 and idle_seconds >= 0.0:
		var threshold: float = widget.sleep_after_minutes * 60.0
		if minf(quiet_elapsed, idle_seconds) >= threshold:
			begin_sleep()

func begin_sleep() -> void:
	if phase != "awake" or not can_sleep():
		return
	phase = "entering"
	sleep_started_at = Time.get_ticks_msec() / 1000.0
	widget.player.speed_scale = 1.0
	widget.play_clip("sleep_enter", 0.25)
	widget.gaze.enabled = false

func activity() -> void:
	quiet_elapsed = 0.0
	if phase in ["entering", "sleeping"]:
		wake()

func wake() -> void:
	if phase in ["awake", "waking"]:
		return
	# Reverse the descent from its current point when interrupted mid-settle.
	var offset := 0.0
	if phase == "entering":
		var progress: float = widget.player.current_animation_position / widget.player.get_animation("sleep_enter").length
		offset = (1.0 - clampf(progress, 0.0, 1.0)) * widget.player.get_animation("wake_up").length
	phase = "waking"
	widget.player.speed_scale = 1.0
	widget.player.play("wake_up", 0.15)
	widget.player.seek(offset, true)

func defer_performance(data: Dictionary) -> bool:
	quiet_elapsed = 0.0
	if phase == "awake":
		return false
	# Keep the latest performance; a stale voice response must not play after a newer one.
	pending = data.duplicate(true)
	var requested := str(data.get("state", ""))
	if requested in widget.PERSISTENT:
		widget.rest_state = requested
	wake()
	return true

func animation_finished(clip: StringName) -> void:
	if clip == "sleep_enter" and phase == "entering":
		phase = "sleeping"
		widget.play_clip("sleep_loop", 0.0)
	elif clip == "wake_up" and phase == "waking":
		phase = "awake"
		quiet_elapsed = 0.0
		widget.gaze.enabled = true
		widget.play_clip(widget.STATE_CLIPS[widget.state], 0.18)
		if not pending.is_empty():
			var data := pending
			pending = {}
			widget.perform(data)

func set_delay(minutes: float) -> void:
	widget.sleep_after_minutes = maxf(0.0, minutes)
	activity()
	widget.save_settings()
