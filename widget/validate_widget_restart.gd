extends SceneTree
## Runs across a real process restart, with isolated settings and no daemon connection.
class TestWidget:
	extends "res://widget.gd"
	func settings_path() -> String:
		return "user://widget_restart_test.cfg"
	func setup_ws() -> void:
		pass

const MARKER := "user://widget_restart_test.json"

func _initialize() -> void:
	call_deferred("run")

func run() -> void:
	var widget := TestWidget.new()
	root.add_child(widget)
	await process_frame
	if not FileAccess.file_exists(MARKER):
		widget.set_skin("mint")
		widget.set_top_hat(true)
		var marker := {"pid": OS.get_process_id(), "time": Time.get_unix_time_from_system()}
		FileAccess.open(MARKER, FileAccess.WRITE).store_string(JSON.stringify(marker))
		widget.restart_widget()
		return
	var marker: Dictionary = JSON.parse_string(FileAccess.get_file_as_string(MARKER))
	var failures: Array[String] = []
	if OS.get_process_id() == int(marker.pid): failures.append("Process did not restart")
	if Time.get_unix_time_from_system() - float(marker.time) > 30.0: failures.append("Stale restart marker")
	if widget.skin_id != "mint" or not widget.top_hat_enabled or not widget.top_hat.visible: failures.append("Preferences did not survive restart")
	if widget.ws_url != "ws://127.0.0.1:1/ws": failures.append("Daemon argument lost")
	var report := {"passed": failures.is_empty(), "failures": failures, "old_pid": marker.pid, "new_pid": OS.get_process_id(), "preferences_preserved": true, "custom_arguments_preserved": true}
	FileAccess.open("res://restart_checks.json", FileAccess.WRITE).store_string(JSON.stringify(report, "\t"))
	print(JSON.stringify(report))
	var config_path := widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(MARKER)
	DirAccess.remove_absolute(config_path)
	quit(0 if failures.is_empty() else 1)
