extends SceneTree
## Reconnect check: the widget must come back on its own after strawberryd restarts.
## Driven from scripts/check_reconnect.sh, which starts, kills, and restarts the daemon.
##
## godot --headless --path widget --script res://validate_reconnect.gd -- --ws=ws://127.0.0.1:PORT/ws --restart-after=3 --reconnect-timeout=20

var restart_after := 3.0
var drop_timeout := 6.0
var reconnect_timeout := 20.0
var failures: Array[String] = []
var report: Dictionary = {"godot_version": Engine.get_version_info().string}
var widget: Node3D

func _initialize() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--restart-after="):
			restart_after = float(arg.trim_prefix("--restart-after="))
		elif arg.begins_with("--drop-timeout="):
			drop_timeout = float(arg.trim_prefix("--drop-timeout="))
		elif arg.begins_with("--reconnect-timeout="):
			reconnect_timeout = float(arg.trim_prefix("--reconnect-timeout="))
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

func wait_until(predicate: Callable, timeout: float) -> float:
	var waited := 0.0
	while not predicate.call() and waited < timeout:
		await create_timer(0.1).timeout
		waited += 0.1
	return waited

func run() -> void:
	widget = (load("res://widget.tscn") as PackedScene).instantiate()
	root.add_child(widget)
	var t := await wait_until(func(): return widget.ws.is_open(), 6.0)
	check(widget.ws.is_open(), "initial connect")
	report["initial_connect_seconds"] = snappedf(t, 0.1)

	# The shell script kills the daemon at about restart_after seconds.
	t = await wait_until(func(): return not widget.ws.is_open(), restart_after + drop_timeout)
	check(not widget.ws.is_open(), "widget should notice the daemon going away within %.0fs" % drop_timeout)
	report["noticed_drop_after_seconds"] = snappedf(maxf(t - restart_after, 0.0), 0.1)
	report["ready_state_after_drop"] = widget.ws.peer.get_ready_state()

	t = await wait_until(func(): return widget.ws.is_open(), reconnect_timeout)
	check(widget.ws.is_open(), "widget should reconnect within %.0fs" % reconnect_timeout)
	report["reconnected_after_seconds"] = snappedf(t, 0.1)
	report["reconnect_attempts"] = widget.ws.reconnects

	report["failures"] = failures
	report["passed"] = failures.is_empty()
	var file := FileAccess.open("res://reconnect_checks.json", FileAccess.WRITE)
	file.store_string(JSON.stringify(report, "\t"))
	file.close()
	print("reconnect checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
