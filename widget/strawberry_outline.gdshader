shader_type spatial;
render_mode unshaded, cull_front;

uniform vec4 ink_color : source_color = vec4(0.125, 0.075, 0.094, 1.0);
uniform float width : hint_range(0.0, 0.008, 0.0001) = 0.0015;

void vertex() {
    VERTEX += NORMAL * width;
}

void fragment() {
    ALBEDO = ink_color.rgb;
}
