extends SceneTree
## The approval card (WIRING.md §13, §19): the hello that asks for it, the brain's clock, the card on a
## request (plain text, 40 px targets, above the anchor with her line moved up, in the click-through
## hull), Yes tapped on a change tier, a hold needed on a destructive one (a tap and a key are only
## hinted, a refused tap keeps it, a held Yes is sent with `hold: true`), No, the keys, the card going
## when it is answered elsewhere or the run ends, a reconnect's replay not making a second card and a
## forgotten one going, the countdown reaching "waiting…" and the last-resort expiry, pokes and card
## clicks kept apart, the chip's ✕ still working, her waiting pose. Real widget, isolated settings, no
## daemon (a stand-in socket with a brain clock 1000 s ahead).
##
## godot --headless --path widget --script res://validate_approval.gd
class FakeWs:
	extends Node
	var sent: Array = []
	var open := true
	var offset := 1000.0
	func is_open() -> bool:
		return open
	func send(data: Dictionary) -> void:
		sent.append(data)
	func brain_now() -> float:
		return Time.get_ticks_msec() / 1000.0 + offset

class TestWidget:
	extends "res://widget.gd"
	func settings_path() -> String:
		return "user://approval_test_%s.cfg" % OS.get_process_id()
	func setup_ws() -> void:
		ws = FakeWs.new()
		add_child(ws)
	func setup_sleep() -> void:
		super.setup_sleep()
		sleeper.monitor_enabled = false

const WsClient = preload("res://ws_client.gd")

var failures: Array[String] = []
var report := {}
var widget: TestWidget
var card: PanelContainer
var ws: FakeWs
var ids := 0

func _initialize() -> void:
	call_deferred("run")

func check(ok: bool, message: String) -> void:
	if not ok:
		failures.append(message)
		push_error("FAIL: " + message)

func wait(seconds: float) -> void:
	await create_timer(seconds).timeout

func frames(n: int) -> void:
	for i in n:
		await process_frame

func button(at: Vector2, pressed: bool) -> void:
	var e := InputEventMouseButton.new()
	e.button_index = MOUSE_BUTTON_LEFT
	e.pressed = pressed
	e.position = at
	e.global_position = at
	Input.parse_input_event(e)
	await process_frame

func tap(at: Vector2, hold := 0.08) -> void:
	await button(at, true)
	await wait(hold)
	await button(at, false)

func key(code: Key) -> void:
	for pressed in [true, false]:
		var e := InputEventKey.new()
		e.keycode = code
		e.physical_keycode = code
		e.pressed = pressed
		Input.parse_input_event(e)
		await process_frame

func centre(control: Control) -> Vector2:
	return control.get_global_rect().get_center()

func answers() -> Array:
	return ws.sent.filter(func(m): return m.get("type") == "approval.answer")

## A request as the brain sends it: the next id, `seconds` to go on the brain's clock.
func request(risk := "change", seconds := 10.0, prompt := "Remove 'Feeling Good' from Running?", run := "r-1") -> Dictionary:
	ids += 1
	var brain: float = ws.brain_now()
	var data := {"type": "approval.request", "run_id": run, "seq": 4, "t": brain, "approval_id": "a-0c0ffe-%d" % ids,
		"risk": risk, "prompt": prompt, "timeout_s": seconds, "expires_t": brain + seconds,
		"hold": risk in ["sends", "destructive"]}
	widget.on_typed(data)
	await frames(3)
	return data

func resolved(data: Dictionary, answer: String, by := "") -> void:
	var out := {"type": "approval.resolved", "run_id": data.run_id, "seq": 5, "t": ws.brain_now(),
		"approval_id": data.approval_id, "answer": answer}
	if by != "":
		out.by = by
	widget.on_typed(out)
	await frames(2)

func run() -> void:
	# Headless, the root viewport is not the window's size: give it the project's, so the camera puts
	# her, the bubble's anchor and the card where the window has them, and clicks land on them.
	root.size = Vector2i(int(ProjectSettings.get_setting("display/window/size/viewport_width")),
		int(ProjectSettings.get_setting("display/window/size/viewport_height")))
	widget = TestWidget.new()
	widget.place_at = Vector2.ZERO
	root.add_child(widget)
	card = widget.approval_card
	ws = widget.ws
	await wait(0.4)

	# 1. The hello asks for approvals and to answer them, keeps cancel, and says she shows text.
	var client := WsClient.new()
	var hello: Dictionary = client.hello()
	report["hello_capabilities"] = hello.capabilities
	check(hello.protocol == 2 and hello.capabilities.approvals == true and hello.capabilities.sends.approval == true
		and hello.capabilities.sends.cancel == true and hello.capabilities.speech.bubble == true,
		"the hello should declare approvals, sends.approval, sends.cancel and speech.bubble: %s" % str(hello.capabilities))

	# 2. The brain's clock (PROTOCOL §12.1): welcome.t is a rough first sample; the pong with the
	#    smallest round trip of the last eight wins.
	client.add_clock_sample(client.WELCOME_RTT, 500.0)
	check(client.has_clock() and is_equal_approx(client.clock_offset, 500.0), "welcome.t should give a first offset")
	client.on_pong({"type": "pong", "t": 10.0, "brain_t": 1010.2}, 10.4)     # rtt 0.4, offset 1000.0
	client.on_pong({"type": "pong", "t": 20.0, "brain_t": 1020.06}, 20.02)  # rtt 0.02, offset 1000.05
	client.on_pong({"type": "pong", "t": 30.0, "brain_t": 1031.0}, 30.5)    # rtt 0.5, offset 1000.75
	report["clock_offset"] = client.clock_offset
	check(is_equal_approx(client.clock_offset, 1000.05) and is_equal_approx(client.clock_rtt, 0.02),
		"the offset should come from the quickest pong, got %.3f (rtt %.3f)" % [client.clock_offset, client.clock_rtt])
	client.on_pong({"type": "pong"})
	check(client.clock_samples.size() == 4, "a v1 pong has no clock in it")
	client.free()

	# 3. A request shows the card: plain text, the buttons, the countdown on the brain's clock, above the
	#    bubble's anchor with her line moved up over it, and the chip saying she waits.
	widget.on_typed({"type": "welcome", "protocol": 2, "t": ws.brain_now(),
		"accepted": {"phases": ["run", "tool"], "cancel": true, "approvals": true, "approval": true}})
	widget.perform({"state": "talking", "text": "Remove 'Feeling Good' from Running? Say yes.", "emotion": "neutral"})
	widget.step_chip.on_phase({"type": "thinking", "run_id": "r-1"})
	widget.step_chip.on_phase({"type": "speaking", "run_id": "r-1"})
	var first := await request()
	await wait(0.4)
	check(card.visible and card.approval_id == first.approval_id and card.shown_count == 1, "a request should show the card")
	check(card.prompt_label is Label and card.prompt_label.text == first.prompt, "the card shows the prompt, said '%s'" % card.prompt_label.text)
	check(card.status_label.text == "10 s" or card.status_label.text == "9 s", "the countdown should follow the brain's clock, said '%s'" % card.status_label.text)
	for b: Control in [card.yes_button, card.no_button]:
		check(b.visible and b.size.x >= 40.0 and b.size.y >= 40.0, "%s should be at least 40 px, was %s" % [b.text, str(b.size)])
	var anchor: Vector2 = widget.bubble_anchor_on_screen()
	var rect: Rect2 = card.get_global_rect()
	report["card_rect"] = [rect.position.x, rect.position.y, rect.size.x, rect.size.y]
	check(rect.end.y <= anchor.y and rect.position.y > 0.0, "the card should stand on the bubble's anchor, %s against %s" % [str(rect), str(anchor)])
	check(widget.bubble.position.y > widget.BUBBLE_ANCHOR.y and widget.bubble.visible and widget.bubble.speaking,
		"her line should move up above the card and keep going")
	var line_bottom: Vector2 = widget.camera.unproject_position(widget.bubble.global_position)
	check(line_bottom.y <= rect.position.y, "her line should end above the card (%.0f, card at %.0f)" % [line_bottom.y, rect.position.y])
	check(widget.step_chip.step == widget.step_chip.WAITING, "the chip should say she is waiting, said '%s'" % widget.step_chip.step)
	check(widget.state == "talking", "she keeps talking while the card shows")
	check(card.attention > 0.5 and widget.blink_controller.layers.has("approval"), "she should perk up while she waits")

	# 4. The card is in the click-through hull.
	var corners: PackedVector2Array = widget.card_points()
	var hull: PackedVector2Array = widget.passthrough_polygon()
	var inside := corners.size() == 4
	for corner in corners:
		inside = inside and Geometry2D.is_point_in_polygon(corner, hull)
	check(inside, "the card's corners should be inside the click-through hull")

	# 5. A press on the card is the card's, never a poke or a drag; a poke on her never answers it.
	var pokes: int = widget.touch.pokes
	await tap(centre(card.prompt_label))
	check(widget.touch.pokes == pokes and not widget.pressing and not widget.dragging, "a press on the card is never a poke")
	widget.touch.poke(widget.touch.world_to_pixel(widget.model.global_transform * Vector3(0.0, 0.47, -0.12)))
	await tap(widget.touch.world_to_pixel(widget.model.global_transform * Vector3(0.0, 0.47, -0.12)))
	check(widget.touch.pokes >= pokes + 1 and answers().is_empty() and card.is_open(), "a poke should never answer the card")

	# 6. Yes on a change tier is a tap: answer yes, no hold; the card waits for the brain's word.
	await tap(centre(card.yes_button))
	var sent := answers()
	report["tap_yes"] = sent
	check(sent.size() == 1 and sent[0].answer == "yes" and sent[0].hold == false and sent[0].approval_id == first.approval_id,
		"a tap on Yes should send yes without hold: %s" % str(sent))
	check(card.is_open() and card.yes_button.disabled and card.status_label.text == "sending…", "the card waits for the outcome")
	await tap(centre(card.no_button))
	check(answers().size() == 1, "one answer at a time")
	await resolved(first, "yes", "body")
	check(not card.visible and not card.is_open() and widget.card_points().is_empty(), "approval.resolved should hide the card")
	check(is_equal_approx(widget.bubble.position.y, widget.BUBBLE_ANCHOR.y), "her line goes back to its anchor")

	# 7. No.
	var second := await request()
	await tap(centre(card.no_button))
	sent = answers()
	check(sent.size() == 2 and sent[1].answer == "no" and sent[1].hold == false, "No should send no: %s" % str(sent[-1]))
	await resolved(second, "no", "body")
	check(not card.is_open(), "a no should end the card")

	# 8. A destructive tier needs a hold: a tap is only hinted (nothing sent), a refusal for a tap keeps
	#    the card and hints, letting go early drains the fill, a held Yes goes with `hold: true`.
	var shred := await request("destructive", 30.0, "Shred your note called groceries?")
	check(card.hold and card.status_label.text.begins_with("hold Yes"), "a hold tier says so, said '%s'" % card.status_label.text)
	var hints: int = card.hints
	await tap(centre(card.yes_button))
	check(answers().size() == 2 and card.hints == hints + 1 and card.status_label.text == card.HOLD_HINT and card.is_open(),
		"a tap on a hold tier's Yes should only hint, said '%s'" % card.status_label.text)
	widget.on_typed({"type": "input.refused", "ref": shred.approval_id, "reason": "hold_required"})
	await frames(2)
	check(card.is_open() and card.hints == hints + 2 and card.sent == "", "hold_required should keep the card and hint")
	await button(centre(card.yes_button), true)
	await wait(0.5)
	var half: float = card.hold_t / card.HOLD_S
	report["fill_at_half_second"] = snappedf(half, 0.01)
	check(half > 0.35 and half < 0.75 and card.yes_fill.size.x > 20.0 and answers().size() == 2, "the fill should show the hold's progress, %.2f" % half)
	await button(centre(card.yes_button), false)
	await wait(0.4)
	check(card.hold_t == 0.0 and answers().size() == 2, "letting go early cancels it, the fill drains")
	await button(centre(card.yes_button), true)
	await wait(1.2)
	await button(centre(card.yes_button), false)
	sent = answers()
	check(sent.size() == 3 and sent[2].answer == "yes" and sent[2].hold == true, "a held Yes should send yes with hold: %s" % str(sent[-1]))
	await resolved(shred, "yes", "body")
	check(not card.is_open(), "the held yes ends the card")

	# 9. The keys: Y on a tap tier, Y on a hold tier only hints, N and Esc are no.
	var keyed := await request()
	await key(KEY_Y)
	sent = answers()
	check(sent.size() == 4 and sent[3].answer == "yes" and sent[3].hold == false, "Y should answer yes on a tap tier")
	await resolved(keyed, "yes", "body")
	keyed = await request("sends", 30.0, "Send 'running late' to the group?")
	hints = card.hints
	await key(KEY_Y)
	check(answers().size() == 4 and card.hints == hints + 1, "Y on a hold tier should only hint")
	await key(KEY_ESCAPE)
	sent = answers()
	check(sent.size() == 5 and sent[4].answer == "no", "Esc should answer no")
	await resolved(keyed, "no", "body")
	keyed = await request()
	await key(KEY_N)
	check(answers().size() == 6 and answers()[5].answer == "no", "N should answer no")
	await resolved(keyed, "no", "body")
	await key(KEY_N)
	check(answers().size() == 6, "with no card the keys answer nothing")

	# 10. Answered elsewhere (her voice, the type box, the Brain UI), the run's end, a refusal that says
	#     it is gone: the card goes.
	var elsewhere := await request()
	await resolved(elsewhere, "yes", "voice")
	check(not card.is_open() and card.closed_reason == "resolved yes", "an answer by voice should hide the card")
	elsewhere = await request()
	await resolved(elsewhere, "timeout")
	check(not card.is_open(), "a timeout hides the card")
	elsewhere = await request("change", 10.0, "Remove it?", "r-7")
	widget.on_typed({"type": "run.cancelled", "run_id": "r-6", "reason": "stopped"})
	check(card.is_open(), "another run's end leaves the card")
	widget.on_typed({"type": "run.cancelled", "run_id": "r-7", "reason": "stopped"})
	check(not card.is_open() and card.closed_reason == "run ended", "the run's end should hide the card")
	elsewhere = await request()
	widget.on_typed({"type": "input.refused", "ref": elsewhere.approval_id, "reason": "resolved"})
	check(not card.is_open(), "input.refused resolved should hide the card")

	# 11. The chip's ✕ still stops the run while the card shows (the brain then resolves it cancelled).
	widget.step_chip.on_phase({"type": "thinking", "run_id": "r-9"})
	var stopped := await request("change", 10.0, "Remove it?", "r-9")
	await wait(widget.step_chip.SHOW_AFTER + 0.1)
	check(widget.step_chip.visible and card.visible, "the chip and the card show together")
	await tap(centre(widget.step_chip.stop_button))
	check(ws.sent.any(func(m): return m.get("type") == "run.cancel" and m.get("run_id") == "r-9") and answers().size() == 6,
		"the ✕ should send run.cancel, not an answer")
	await resolved(stopped, "cancelled")
	widget.on_typed({"type": "run.cancelled", "run_id": "r-9", "reason": "stopped"})
	check(not card.is_open() and not widget.step_chip.visible, "the cancelled run ends the card and the chip")

	# 12. Reconnecting: the open request comes again after welcome, same id: one card. A welcome after
	#     which it does not come (a restarted daemon forgot it) lets the card go.
	var again := await request()
	var shown: int = card.shown_count
	ws.open = false
	await frames(2)
	check(card.no_button.disabled and card.status_label.text == "not connected", "without the socket the buttons wait")
	ws.open = true
	widget.on_typed({"type": "welcome", "protocol": 2, "t": ws.brain_now(),
		"accepted": {"phases": ["run"], "cancel": true, "approvals": true, "approval": true}})
	widget.on_typed(again)
	await frames(2)
	var cards := widget.get_children().filter(func(n): return n.name == "ApprovalCard")
	check(card.shown_count == shown and card.replays == 1 and cards.size() == 1 and card.is_open() and not card.no_button.disabled,
		"the replayed request should not make a second card")
	await wait(card.REPLAY_S + 0.3)
	check(card.is_open(), "a replayed card stays")
	widget.on_typed({"type": "welcome", "protocol": 2, "t": ws.brain_now(), "accepted": {"approvals": true, "approval": true}})
	await wait(card.REPLAY_S + 0.3)
	check(not card.is_open() and card.closed_reason == "gone", "a card the brain did not send again should go")

	# 13. The countdown reaches 0 while the brain still waits (the user speaking): "waiting…", not gone.
	#     LINGER_S past expires_t with no word, it goes.
	await request("change", 1.2)
	await wait(1.5)
	report["after_expiry"] = card.status_label.text
	check(card.is_open() and card.status_label.text == "waiting…", "past the countdown the card says waiting…, said '%s'" % card.status_label.text)
	card.expires_t = ws.brain_now() - card.LINGER_S - 0.5
	await frames(2)
	check(not card.is_open() and card.closed_reason == "expired", "a minute past expires_t with no word, the card goes")

	# 14. A body the welcome did not let answer shows the question without buttons.
	widget.on_typed({"type": "welcome", "protocol": 2, "t": ws.brain_now(), "accepted": {"approvals": true, "approval": false}})
	await request()
	check(card.visible and not card.yes_button.visible and card.status_label.text == "say yes or no", "no answering: the card shows the question only")
	await tap(centre(card))
	check(answers().size() == 6, "and sends nothing")
	widget.on_typed({"type": "approval.resolved", "run_id": "r-1", "approval_id": card.approval_id, "answer": "no", "by": "typed"})
	await wait(0.8)
	check(card.attention == 0.0 and not widget.blink_controller.layers.has("approval"), "her waiting pose eases out")

	# 15. The prompt is plain text: markup shows as written, control characters go.
	widget.on_typed({"type": "welcome", "protocol": 2, "t": ws.brain_now(), "accepted": {"approvals": true, "approval": true}})
	await request("change", 10.0, "Delete [b]all[/b]\n[url=x]this[/url]?")
	check(card.prompt_label.text == "Delete [b]all[/b] [url=x]this[/url]?", "markup should show as plain text, got '%s'" % card.prompt_label.text)
	report["shown"] = card.shown_count
	report["answers"] = answers().size()

	var path: String = widget.settings_path()
	widget.queue_free()
	await process_frame
	DirAccess.remove_absolute(path)
	report["passed"] = failures.is_empty()
	report["failures"] = failures
	print(JSON.stringify(report))
	print("approval checks: ", "PASSED" if failures.is_empty() else "FAILED (%d)" % failures.size())
	quit(0 if failures.is_empty() else 1)
