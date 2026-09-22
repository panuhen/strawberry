extends SceneTree
## Exercise the actual worker-thread/helper path without connecting to the daemon.
func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var monitor = load("res://sleep_controller.gd").new()
	monitor.helper_path = preload("res://paths.gd").on_disk("res://desktop_idle.py")
	var error: int = monitor.probe.start(monitor.read_idle)
	if error != OK:
		push_error("Idle worker did not start")
		monitor.free()
		quit(1)
		return
	var frames := 0
	while monitor.probe.is_alive():
		frames += 1
		await process_frame
	var result: Dictionary = monitor.probe.wait_to_finish()
	print("DESKTOP_IDLE ", JSON.stringify(result), " rendering thread frames=", frames)
	var passed: bool = result.get("seconds") != null and float(result.seconds) >= 0.0 and frames > 0
	monitor.free()
	quit(0 if passed else 1)
