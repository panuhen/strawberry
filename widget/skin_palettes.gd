extends RefCounted
## Match the established imported-material colour conversion.
const ORDER = ["strawberry", "peach", "blueberry", "mint", "lavender"]
const SKINS = {
	"strawberry": {"name": "Strawberry", "shell": "cf2b28", "claw": "e2402f", "dark": "7d1516", "cream": "f6e3cf"},
	"peach": {"name": "Peach", "shell": "f2a16f", "claw": "f27f78", "dark": "995344", "cream": "fff0d4"},
	"blueberry": {"name": "Blueberry", "shell": "596bc8", "claw": "8995ed", "dark": "586891", "cream": "efe6d5"},
	"mint": {"name": "Mint", "shell": "76c9ac", "claw": "42b9b0", "dark": "326c61", "cream": "fff5df"},
	"lavender": {"name": "Lavender", "shell": "ad8bcc", "claw": "db91b0", "dark": "665071", "cream": "ffe1ce"},
}

static func colors(id: String) -> Dictionary:
	# An empty override preserves the original Strawberry materials exactly.
	if id == "strawberry" or not SKINS.has(id):
		return {}
	var skin: Dictionary = SKINS[id]
	return {"mat_shell": Color.html(skin.shell).linear_to_srgb(),
		"mat_claw": Color.html(skin.claw).linear_to_srgb(),
		"mat_shell_dark": Color.html(skin.dark).linear_to_srgb(),
		"mat_cream": Color.html(skin.cream).linear_to_srgb()}

static func display_name(id: String) -> String:
	return SKINS.get(id, SKINS.strawberry).name
