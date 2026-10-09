extends RefCounted
## Easing curves shared by the procedural layers (reactions, dance styles, turning, touch).
## Each takes x in [0, 1] (clamped) and returns 0 at 0 and 1 at 1.

## Cubic ease in and out: slow start, slow landing.
static func in_out(x: float) -> float:
	x = clampf(x, 0.0, 1.0)
	return 4.0 * x * x * x if x < 0.5 else 1.0 - pow(-2.0 * x + 2.0, 3.0) / 2.0

## Ease out: fast start, slow landing.
static func out(x: float) -> float:
	x = clampf(x, 0.0, 1.0)
	return 1.0 - pow(1.0 - x, 3.0)

## Ease out past the mark and settle back: about 10 % overshoot at the default strength.
## The same curve as back_out() in model/build_strawberry.py (alert_snap's claw lift).
static func back_out(x: float, strength := 1.70158) -> float:
	x = clampf(x, 0.0, 1.0) - 1.0
	return 1.0 + (strength + 1.0) * x * x * x + strength * x * x

## Up, hold, down: eases in over [0, rise], holds, eases out over [1 - fall, 1].
static func envelope(x: float, rise: float, fall: float) -> float:
	return in_out(x / rise) * (1.0 - in_out((x - (1.0 - fall)) / fall))

## Out and straight back over [0, 1]: a quick look that returns (sin² keeps both ends flat).
static func there_and_back(x: float) -> float:
	x = clampf(x, 0.0, 1.0)
	return pow(sin(PI * x), 2.0)
