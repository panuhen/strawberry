extends Node
## Websocket client to strawberryd. Godot connects out and reconnects on its own,
## so the daemon can be restarted freely while the crab keeps running (WIRING.md §1).
##
## Liveness is not left to TCP: we ping every PING_INTERVAL seconds and, if the daemon
## stays silent for SILENCE_TIMEOUT, drop the socket and reconnect. A daemon that is
## killed hard, a laptop waking from sleep, or a half-open socket all end the same way.

signal connected
signal disconnected
signal message_received(data: Dictionary)
signal refused(reason: String)

const MAX_BACKOFF := 8.0
const CLOSE_VERSION_REFUSED := 4001   # = strawberry_crab.server.CLOSE_VERSION_REFUSED
const REFUSED_BACKOFF := 60.0
const Paths = preload("res://paths.gd")
const PING_INTERVAL := 5.0
const SILENCE_TIMEOUT := 12.0
const CLOCK_SAMPLES := 8          # PROTOCOL §12.1: the smallest round trip of the last 8 pongs
const WELCOME_RTT := 1.0          # welcome.t is a rough first sample: any pong replaces it

@export var url := "ws://127.0.0.1:8770/ws"
var peer := WebSocketPeer.new()
var was_open := false
var connecting := false
var retry_in := 0.0
var backoff := 1.0
var reconnects := 0
var since_activity := 0.0
var since_ping := 0.0
# The brain's clock (PROTOCOL §12.1): brain time = body time + clock_offset. Each sample is
# [round trip, offset]; the one with the smallest round trip wins.
var clock_samples: Array = []
var clock_offset := 0.0
var clock_rtt := INF

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
	since_activity = 0.0

func _schedule_retry() -> void:
	connecting = false
	retry_in = backoff
	backoff = minf(backoff * 2.0, MAX_BACKOFF)

func _drop(reason: String) -> void:
	if was_open:
		was_open = false
		disconnected.emit()
	push_warning("strawberryd: %s; reconnecting" % reason)
	peer.close()
	_schedule_retry()

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
				since_activity = 0.0
				since_ping = 0.0
				send(hello())
				ping()          # a first clock sample now, not in PING_INTERVAL
				connected.emit()
			while peer.get_available_packet_count() > 0:
				since_activity = 0.0
				var packet := peer.get_packet()
				if not peer.was_string_packet():
					continue
				var parsed: Variant = JSON.parse_string(packet.get_string_from_utf8())
				if parsed is Dictionary:
					if parsed.get("type", "") == "pong":
						on_pong(parsed)
						continue
					if parsed.get("type", "") == "welcome" and (parsed.get("t") is float or parsed.get("t") is int):
						add_clock_sample(WELCOME_RTT, float(parsed.t) - now())
					message_received.emit(parsed)
				else:
					push_warning("strawberryd sent something that is not a JSON object")
			since_activity += delta
			since_ping += delta
			if since_ping >= PING_INTERVAL:
				ping()
			if since_activity >= SILENCE_TIMEOUT:
				_drop("silent for %.0fs" % since_activity)
		WebSocketPeer.STATE_CONNECTING:
			since_activity += delta
			if since_activity >= SILENCE_TIMEOUT:
				_drop("connect attempt stalled")
		WebSocketPeer.STATE_CLOSED:
			if was_open:
				was_open = false
				disconnected.emit()
			if peer.get_close_code() == CLOSE_VERSION_REFUSED:
				# The daemon will not talk to this widget version (a major mismatch, §1): say so
				# and keep trying, slowly, in case the daemon is upgraded underneath us.
				push_error("strawberryd refused this widget: " + peer.get_close_reason())
				refused.emit(peer.get_close_reason())
				backoff = REFUSED_BACKOFF
			_schedule_retry()
		_:
			pass  # CLOSING: keep polling until CLOSED

## Protocol v2 (PROTOCOL.md §9b): the v1 fields, then what this body is and what it wants. It
## asks for the run events its step chip shows and says it may send run.cancel (the chip's ✕); it
## shows approvals as a card and may answer them (approval_card.gd); it shows text (her bubble); it
## shows a held hand gesture as a filling ring (`gesture`, gesture_ring.gd), never the hand itself.
## The bus secret goes with it, read from the daemon's file at every connect (PROTOCOL §1.4): without
## it the brain sends only how she moves (no lines, no voice, no tool names), takes no typing, taps or
## answers, and sends no cards.
func hello() -> Dictionary:
	var greeting := {"type": "hello", "client": "strawberry-widget", "version": Paths.version(),
		"godot": Engine.get_version_info().string, "protocol": 2,
		"body": {"id": "crab", "name": "Strawberry"},
		"capabilities": {"phases": ["routing", "thinking", "tool", "speaking", "run"],
			"approvals": true, "speech": {"bubble": true}, "gestures": ["gesture"],
			"sends": {"heard": true, "cancel": true, "approval": true}}}
	var secret := Paths.bus_secret()
	if secret != "":
		greeting["secret"] = secret
	else:
		push_warning("strawberryd: no bus secret at %s yet; she will move but not speak, and take no input" % Paths.bus_secret_file())
	return greeting

func now() -> float:
	return Time.get_ticks_msec() / 1000.0

## A v2 ping carries our own time; the pong brings it back with the brain's (PROTOCOL §12b).
func ping() -> void:
	since_ping = 0.0
	send({"type": "ping", "t": now()})

## PROTOCOL §12.1: arriving at body time r, rtt = r - t and offset = brain_t - (t + r) / 2.
func on_pong(data: Dictionary, arrived := -1.0) -> void:
	var sent_t: Variant = data.get("t")
	var brain_t: Variant = data.get("brain_t")
	if not (sent_t is float or sent_t is int) or not (brain_t is float or brain_t is int):
		return      # a v1 pong: nothing to learn
	var r := arrived if arrived >= 0.0 else now()
	add_clock_sample(maxf(r - float(sent_t), 0.0), float(brain_t) - (float(sent_t) + r) / 2.0)

func add_clock_sample(rtt: float, offset: float) -> void:
	clock_samples.append([rtt, offset])
	while clock_samples.size() > CLOCK_SAMPLES:
		clock_samples.pop_front()
	clock_rtt = INF
	for sample: Array in clock_samples:
		if sample[0] < clock_rtt:
			clock_rtt = sample[0]
			clock_offset = sample[1]

func has_clock() -> bool:
	return not clock_samples.is_empty()

## The brain's monotonic time now, on our clock (an approval's countdown to its expires_t).
func brain_now() -> float:
	return now() + clock_offset

func send(data: Dictionary) -> void:
	if peer.get_ready_state() == WebSocketPeer.STATE_OPEN:
		peer.send_text(JSON.stringify(data))

func is_open() -> bool:
	return was_open
