extends PanelContainer
## The approval card (WIRING.md §13, §19): when a call waits for the user's yes, her question shows
## as a card with Yes and No, so it can be answered on screen as well as by voice or typing.
##
## Fed by `approval.request` and `approval.resolved` (PROTOCOL.md §13b). It shows the request's
## `prompt` as plain text (a Label: no markup is ever read from it), a countdown to `expires_t` on
## the brain's clock (ws_client.gd, PROTOCOL §12.1), and sends `approval.answer`. For a tier that
## asks for a hold (`hold: true`: sends, destructive) Yes must be held for HOLD_S, filling as it
## goes; letting go early, or a key, only shows "hold to confirm". No is always one tap.
##
## It goes on `approval.resolved` for its id (whoever answered: the card, her voice, the type box,
## the Brain UI), the run's terminal event, a refusal that says the question is gone, or LINGER_S
## after `expires_t` with no word. The countdown can reach 0 while the brain still waits (the user
## is speaking): then it says "waiting…". After a reconnect the open request comes again with the
## same id, and the card stays as it is.
##
## The card sits just above the bubble's usual anchor, and her bubble moves up above it while it
## shows (widget.gd lift_bubble), so it reads line, card, step chip, crab. The same smoked glass as
## the step chip and the type box; buttons 48 px tall, a fingertip on a small touch screen.

const GLASS := Color(0.13, 0.07, 0.09, 0.40)
const RIM := Color(1.0, 0.96, 0.92, 0.55)
const INK := Color("fff7ec")
const GAP := 8.0                 # between the card's bottom and the bubble's usual anchor
const BUBBLE_GAP := 4.0          # between the card's top and her lifted bubble
const MARGIN := 14.0             # from the window's sides
const MIN_WIDTH := 300.0
const BUTTON_H := 48.0
const YES_W := 88.0
const NO_W := 72.0
const BAR_H := 2.0
const PROMPT_SIZE := 17
const BUTTON_SIZE := 17
const STATUS_SIZE := 14
const PROMPT_LINES := 3
const MAX_PROMPT := 160          # = the brain's cut (runs.FIELDS)
const HOLD_S := 1.0              # how long Yes is held for a tier that asks for a hold
const DRAIN_S := 0.25            # an early release empties the fill this fast
const HINT_S := 2.2
const SHAKE_S := 0.4
const LINGER_S := 60.0           # after expires_t with no word from the brain (PROTOCOL §13b)
const REPLAY_S := 2.0            # after welcome, the open request comes again within this, or it is gone
const LEAN := 4.0                # degrees she leans toward the user while she waits
const HOLD_HINT := "hold to confirm"

var widget: Node3D
var prompt_label: Label
var status_label: Label
var row: PanelContainer
var no_button: Button
var yes_slot: Control
var yes_fill: ColorRect
var yes_edge: ColorRect
var yes_button: Button
var bar: ColorRect

var approval_id := ""
var run_id := ""
var risk := ""
var hold := false
var timeout_s := 0.0
var expires_t := 0.0
var prompt := ""
var can_answer := false          # the welcome said this body may send approval.answer
var sent := ""                   # the answer on its way ("yes"/"no"); "" while none is
var holding := false
var hold_t := 0.0                # how far the hold is, in seconds
var hint_until := 0.0
var shake_from := -100.0
var stale_at := -1.0             # a welcome came: the request must come again by then
var attention := 0.0             # her waiting pose, eased 0..1
var left_x := 0.0
var placed_height := -1.0
var was_hinting := false
var skeleton: Skeleton3D
var body_i := -1

# For the checks (validate_approval.gd).
var shown_count := 0
var answers_sent := 0
var hints := 0
var replays := 0
var closed_reason := ""

func setup(owner: Node3D) -> void:
	widget = owner
	name = "ApprovalCard"
	visible = false
	process_priority = 158       # after touch (157): her lean goes on top of the clip and the reactions
	mouse_filter = Control.MOUSE_FILTER_STOP
	add_theme_stylebox_override("panel", glass())
	var column := VBoxContainer.new()
	column.add_theme_constant_override("separation", 0)
	add_child(column)

	var text_box := MarginContainer.new()
	for side in ["left", "right"]:
		text_box.add_theme_constant_override("margin_" + side, 14)
	text_box.add_theme_constant_override("margin_top", 10)
	text_box.add_theme_constant_override("margin_bottom", 10)
	column.add_child(text_box)
	prompt_label = Label.new()
	prompt_label.add_theme_font_size_override("font_size", PROMPT_SIZE)
	prompt_label.add_theme_color_override("font_color", INK)
	prompt_label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	prompt_label.max_lines_visible = PROMPT_LINES
	prompt_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	text_box.add_child(prompt_label)

	row = PanelContainer.new()
	row.add_theme_stylebox_override("panel", hairline(true, false))
	row.mouse_filter = Control.MOUSE_FILTER_PASS
	column.add_child(row)
	var buttons := HBoxContainer.new()
	buttons.add_theme_constant_override("separation", 0)
	row.add_child(buttons)
	var status_box := MarginContainer.new()
	status_box.add_theme_constant_override("margin_left", 14)
	status_box.add_theme_constant_override("margin_right", 8)
	status_box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	buttons.add_child(status_box)
	status_label = Label.new()
	status_label.add_theme_font_size_override("font_size", STATUS_SIZE)
	status_label.add_theme_color_override("font_color", Color(INK, 0.72))
	status_label.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	status_label.custom_minimum_size = Vector2(0, BUTTON_H)
	status_label.text_overrun_behavior = TextServer.OVERRUN_TRIM_ELLIPSIS
	status_box.add_child(status_label)

	no_button = make_button("No", NO_W)
	no_button.tooltip_text = "Leave it (N or Esc)"
	no_button.pressed.connect(func(): answer("no"))
	buttons.add_child(no_button)

	# Yes is a slot: the fill drawn first, the button (text, hairline, wash) over it.
	yes_slot = Control.new()
	yes_slot.custom_minimum_size = Vector2(YES_W, BUTTON_H)
	yes_slot.mouse_filter = Control.MOUSE_FILTER_IGNORE
	buttons.add_child(yes_slot)
	yes_fill = ColorRect.new()
	yes_fill.mouse_filter = Control.MOUSE_FILTER_IGNORE
	yes_slot.add_child(yes_fill)
	yes_edge = ColorRect.new()
	yes_edge.color = Color(INK, 0.9)
	yes_edge.mouse_filter = Control.MOUSE_FILTER_IGNORE
	yes_slot.add_child(yes_edge)
	yes_button = make_button("Yes", YES_W)
	yes_button.set_anchors_preset(Control.PRESET_FULL_RECT)
	yes_button.button_down.connect(_on_yes_down)
	yes_button.button_up.connect(_on_yes_up)
	yes_button.pressed.connect(_on_yes_pressed)
	yes_button.mouse_exited.connect(func(): holding = false)
	yes_slot.add_child(yes_button)

	bar = ColorRect.new()
	bar.color = Color(INK, 0.55)
	bar.mouse_filter = Control.MOUSE_FILTER_IGNORE
	bar.custom_minimum_size = Vector2(0, BAR_H)
	bar.size_flags_horizontal = Control.SIZE_SHRINK_BEGIN
	column.add_child(bar)
	apply_skin()

	var model: Node = widget.model
	skeleton = model.find_children("*", "Skeleton3D", true, false)[0]
	body_i = skeleton.find_bone("body")

## The glass itself: dark tint, hairline rim, square corners.
func glass() -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = GLASS
	style.set_border_width_all(1)
	style.border_color = RIM
	return style

## A faint line on the top (the row under the prompt) or the left (between buttons), and a wash.
func hairline(top: bool, left: bool, wash := 0.0) -> StyleBoxFlat:
	var style := StyleBoxFlat.new()
	style.bg_color = Color(1.0, 0.96, 0.92, wash)
	style.border_width_top = 1 if top else 0
	style.border_width_left = 1 if left else 0
	style.border_color = Color(RIM, 0.35)
	return style

func make_button(text: String, width: float) -> Button:
	var button := Button.new()
	button.text = text
	button.focus_mode = Control.FOCUS_NONE
	button.custom_minimum_size = Vector2(width, BUTTON_H)
	button.add_theme_font_size_override("font_size", BUTTON_SIZE)
	for state_name in ["font_color", "font_hover_color", "font_pressed_color", "font_focus_color", "font_hover_pressed_color"]:
		button.add_theme_color_override(state_name, INK)
	button.add_theme_color_override("font_disabled_color", Color(INK, 0.4))
	button.add_theme_stylebox_override("normal", hairline(false, true))
	button.add_theme_stylebox_override("hover", hairline(false, true, 0.12))
	button.add_theme_stylebox_override("pressed", hairline(false, true, 0.22))
	button.add_theme_stylebox_override("hover_pressed", hairline(false, true, 0.22))
	button.add_theme_stylebox_override("disabled", hairline(false, true))
	button.add_theme_stylebox_override("focus", StyleBoxEmpty.new())
	return button

## The hold fill takes the skin's claw colour, as the type box's caret does.
func apply_skin() -> void:
	var skin: Dictionary = widget.SkinPalettes.SKINS.get(widget.skin_id, widget.SkinPalettes.SKINS.strawberry)
	yes_fill.color = Color(Color.html(skin.claw).lightened(0.15), 0.62)

func is_open() -> bool:
	return approval_id != ""

func now() -> float:
	return Time.get_ticks_msec() / 1000.0

## The welcome's `accepted`. A card still up from before a reconnect waits REPLAY_S for its request
## to come again (PROTOCOL §13b); a daemon that restarted has forgotten it, and then it goes.
func welcomed(accepted: Dictionary) -> void:
	can_answer = bool(accepted.get("approvals", false)) and bool(accepted.get("approval", false))
	if is_open():
		stale_at = now() + REPLAY_S
	refresh()

## A run event: the card's own (approval.request, approval.resolved) and the runs' ends.
func on_event(data: Dictionary) -> void:
	var kind := str(data.get("type", ""))
	match kind:
		"approval.request":
			open(data)
		"approval.resolved":
			if is_open() and str(data.get("approval_id", "")) == approval_id:
				close("resolved " + str(data.get("answer", "")))
		"run.completed", "run.failed", "run.cancelled":
			if is_open() and str(data.get("run_id", "")) == run_id:
				close("run ended")

func open(data: Dictionary) -> void:
	var id := str(data.get("approval_id", ""))
	if id == "":
		return
	var expires: Variant = data.get("expires_t")
	expires_t = float(expires) if expires is float or expires is int else widget.brain_now() + 10.0
	timeout_s = maxf(float(data.get("timeout_s", 10.0)), 0.1)
	if id == approval_id:
		# The same request again (after a reconnect): no second card. An answer that was on its way
		# may have been lost with the socket, so the buttons work again.
		replays += 1
		stale_at = -1.0
		sent = ""
		refresh()
		return
	approval_id = id
	run_id = str(data.get("run_id", ""))
	risk = str(data.get("risk", ""))
	hold = bool(data.get("hold", false))
	prompt = plain(str(data.get("prompt", "")))
	if prompt == "":
		prompt = "Go ahead?"
	sent = ""
	holding = false
	hold_t = 0.0
	hint_until = 0.0
	stale_at = -1.0
	closed_reason = ""
	was_hinting = false
	status_label.add_theme_color_override("font_color", Color(INK, 0.72))
	prompt_label.text = prompt
	shown_count += 1
	apply_skin()
	place()
	refresh()

func close(reason: String) -> void:
	if not is_open():
		return
	closed_reason = reason
	approval_id = ""
	run_id = ""
	sent = ""
	holding = false
	hold_t = 0.0
	stale_at = -1.0
	placed_height = -1.0
	visible = false
	widget.lift_bubble(0.0)
	widget.call_deferred("update_passthrough")

## One line, no control characters, as long as the brain allows: shown as is, never as markup.
static func plain(text: String) -> String:
	var out := ""
	for i in text.length():
		var code := text.unicode_at(i)
		out += " " if code < 32 or code == 127 else text[i]
	return out.strip_edges().left(MAX_PROMPT)

## The daemon's input.refused for this card's id (PROTOCOL §13b).
func on_refused(data: Dictionary) -> void:
	if not is_open() or str(data.get("ref", "")) != approval_id.left(32):
		return
	match str(data.get("reason", "")):
		"hold_required":
			sent = ""
			hint()
		"resolved", "not_open":
			close(str(data.reason))
		"not_declared":
			sent = ""
			can_answer = false
		_:
			sent = ""
	refresh()

## Send the answer. A yes to a hold tier must have been held (`held`); otherwise only the hint.
func answer(choice: String, held := false) -> bool:
	if not is_open() or sent != "" or not can_answer or widget.ws == null or not widget.ws.is_open():
		return false
	if choice == "yes" and hold and not held:
		hint()
		return false
	sent = choice
	answers_sent += 1
	widget.ws.send({"type": "approval.answer", "approval_id": approval_id, "answer": choice, "hold": held})
	refresh()
	return true

## Y on the keyboard: a key is never a hold, so a hold tier gets the hint.
func key_yes() -> void:
	if hold:
		hint()
	else:
		answer("yes")

## "hold to confirm", and a small shake.
func hint() -> void:
	hints += 1
	hint_until = now() + HINT_S
	shake_from = now()
	refresh()

func _on_yes_down() -> void:
	if hold and can_answer and sent == "":
		holding = true

func _on_yes_up() -> void:
	if not holding:
		return
	holding = false
	if hold_t < HOLD_S:
		hint()       # let go early: nothing is sent

func _on_yes_pressed() -> void:
	if not hold:
		answer("yes")

func _process(delta: float) -> void:
	pose(delta)
	if not is_open():
		return
	var at := now()
	if stale_at > 0.0 and at > stale_at:
		close("gone")
		return
	var left: float = expires_t - widget.brain_now()
	if left < -LINGER_S:
		close("expired")
		return
	if holding:
		hold_t += delta
		if hold_t >= HOLD_S:
			holding = false
			hold_t = HOLD_S
			answer("yes", true)
	elif sent != "yes":
		hold_t = maxf(hold_t - delta * HOLD_S / DRAIN_S, 0.0)
	refresh()

## Status, countdown, buttons, the hold fill and the shake: what the card shows this frame.
func refresh() -> void:
	set_shown(is_open() and widget.visible)
	if not is_open():
		return
	settle()
	var connected := is_connected_to_brain()
	var left: float = expires_t - widget.brain_now()
	var hinting := now() < hint_until
	var text := ""
	if not connected:
		text = "not connected"
	elif not can_answer:
		text = "say yes or no"
	elif sent != "":
		text = "sending…"
	elif hinting:
		text = HOLD_HINT
	elif left <= 0.0:
		text = "waiting…"
	elif hold:
		text = "hold Yes · %d s" % ceili(left)
	else:
		text = "%d s" % ceili(left)
	status_label.text = text
	if hinting != was_hinting:
		was_hinting = hinting
		status_label.add_theme_color_override("font_color", INK if hinting else Color(INK, 0.72))
	var usable := connected and can_answer and sent == ""
	yes_button.disabled = not usable
	no_button.disabled = not usable
	yes_button.visible = can_answer
	no_button.visible = can_answer
	yes_slot.visible = can_answer
	var fill := clampf(hold_t / HOLD_S, 0.0, 1.0) if hold else 0.0
	yes_fill.position = Vector2.ZERO
	yes_fill.size = Vector2(roundf(YES_W * fill), BUTTON_H)
	yes_edge.visible = fill > 0.0 and fill < 1.0
	yes_edge.position = Vector2(yes_fill.size.x - 1.0, 0.0)
	yes_edge.size = Vector2(2.0, BUTTON_H)
	# The bar keeps its row at 0 (an empty one), so the card does not move when it runs out.
	bar.custom_minimum_size.x = roundf(maxf(size.x - 2.0, 0.0) * clampf(left / timeout_s, 0.0, 1.0))
	var shake := now() - shake_from
	position.x = left_x + (roundf(6.0 * sin(shake * TAU * 7.0) * (1.0 - shake / SHAKE_S)) if shake < SHAKE_S else 0.0)

## Answers can go out. A capture (--capture … --approval=…) shows the card as it looks connected.
func is_connected_to_brain() -> bool:
	return widget.capture_approval != "" or (widget.ws != null and widget.ws.is_open())

func set_shown(shown: bool) -> void:
	if shown == visible:
		return
	visible = shown
	widget.call_deferred("update_passthrough")

## As wide as the prompt wants (MIN_WIDTH up to the window less its margins), centred; settle() then
## stands it on the bubble's usual anchor at the height its laid-out content takes.
func place() -> void:
	var w := float(ProjectSettings.get_setting("display/window/size/viewport_width"))
	var font := prompt_label.get_theme_font("font")
	var text_w := font.get_string_size(prompt, HORIZONTAL_ALIGNMENT_LEFT, -1, PROMPT_SIZE).x if font else MIN_WIDTH
	var width := clampf(ceilf(text_w) + 30.0, MIN_WIDTH, w - 2.0 * MARGIN)
	prompt_label.custom_minimum_size = Vector2(width - 28.0, 0.0)
	custom_minimum_size = Vector2(width, 0.0)
	size = Vector2(width, 0.0)
	left_x = roundf((w - width) / 2.0)
	placed_height = -1.0
	settle()

## Its height is what its content takes once laid out (the wrapped prompt settles a frame later): the
## card stands on the bubble's usual anchor, and her line moves up above it.
func settle() -> void:
	var height := get_combined_minimum_size().y
	if size.y != height:
		size = Vector2(size.x, height)
	if size.y == placed_height:
		return
	placed_height = size.y
	var anchor: Vector2 = widget.bubble_anchor_on_screen()
	position = Vector2(left_x, roundf(anchor.y - GAP - size.y))
	widget.lift_bubble(lift())
	widget.call_deferred("update_passthrough")

## How far her bubble moves up while the card shows, in window pixels.
func lift() -> float:
	return size.y + GAP + BUBBLE_GAP

## Her waiting pose: a slight lean toward the user and wider eyes, eased in while the card shows.
func pose(delta: float) -> void:
	var target := 1.0 if is_open() and widget.sleeper != null and widget.sleeper.phase == "awake" else 0.0
	if target == 0.0 and attention == 0.0:
		return
	attention = move_toward(attention, target, delta / (0.5 if target > 0.0 else 0.4))
	var eased := attention * attention * (3.0 - 2.0 * attention)
	widget.blink_controller.set_layer("approval", 0.3 * eased, 0.0, 0.0)
	if eased > 0.0 and body_i >= 0:
		var q := Quaternion(Vector3.RIGHT, deg_to_rad(-LEAN) * eased)
		skeleton.set_bone_pose_rotation(body_i, skeleton.get_bone_pose_rotation(body_i) * q)

## The card's corners in window pixels, for the click-through hull; none while hidden.
func corners() -> PackedVector2Array:
	if not visible:
		return PackedVector2Array()
	var rect := Rect2(Vector2(left_x, position.y), size)
	return PackedVector2Array([rect.position, Vector2(rect.end.x, rect.position.y), rect.end,
		Vector2(rect.position.x, rect.end.y)])
