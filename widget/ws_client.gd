extends Node
## Websocket client to strawberryd. Godot connects out and reconnects on its own,
## so the daemon can be restarted freely while the crab keeps running (WIRING.md §1).

signal connected
signal disconnected
signal message_received(data: Dictionary)

const MAX_BACKOFF := 8.0

@export var url := "ws://127.0.0.1:8770/ws"
var peer := WebSocketPeer.new()
var was_open := false
var connecting := false
var retry_in := 0.0
var backoff := 1.0
var reconnects := 0

func _ready() -> void:
	_connect()

func _connect() -> void:
	peer = WebSocketPeer.new()
	var err := peer.connect_to_url(url)
	if err != OK:
		push_warning("strawberryd: connect_to_url(%s) failed: %s" % [url, error_string(err)])
		_schedule_retry()
		return
	connecting = true

func _schedule_retry() -> void:
	connecting = false
	retry_in = backoff
	backoff = minf(backoff * 2.0, MAX_BACKOFF)

func _process(delta: float) -> void:
	if retry_in > 0.0:
		retry_in -= delta
		if retry_in <= 0.0:
			reconnects += 1
			_connect()
		return
	if not connecting and not was_open:
		return
	peer.poll()
	match peer.get_ready_state():
		WebSocketPeer.STATE_OPEN:
			if not was_open:
				was_open = true
				connecting = false
				backoff = 1.0
				send({"type": "hello", "client": "strawberry-widget", "godot": Engine.get_version_info().string})
				connected.emit()
			while peer.get_available_packet_count() > 0:
				var packet := peer.get_packet()
				if not peer.was_string_packet():
					continue
				var parsed: Variant = JSON.parse_string(packet.get_string_from_utf8())
				if parsed is Dictionary:
					message_received.emit(parsed)
				else:
					push_warning("strawberryd sent something that is not a JSON object")
		WebSocketPeer.STATE_CLOSED:
			if was_open:
				was_open = false
				disconnected.emit()
			_schedule_retry()
		_:
			pass  # CONNECTING or CLOSING: keep polling

func send(data: Dictionary) -> void:
	if peer.get_ready_state() == WebSocketPeer.STATE_OPEN:
		peer.send_text(JSON.stringify(data))

func is_open() -> bool:
	return was_open
