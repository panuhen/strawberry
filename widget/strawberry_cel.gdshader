shader_type spatial;
render_mode specular_disabled, ambient_light_disabled;

uniform vec4 base_color : source_color = vec4(0.81, 0.17, 0.16, 1.0);
uniform vec4 shadow_tint : source_color = vec4(0.78, 0.71, 0.75, 1.0);
uniform float shadow_edge : hint_range(-1.0, 1.0) = 0.15;
uniform float highlight_edge : hint_range(-1.0, 1.0) = 0.68;
uniform float edge_softness : hint_range(0.001, 0.15) = 0.025;

void fragment() {
    ALBEDO = base_color.rgb;
    ROUGHNESS = 1.0;
}

void light() {
    float facing = dot(normalize(NORMAL), normalize(LIGHT));
    float mid = smoothstep(shadow_edge - edge_softness, shadow_edge + edge_softness, facing);
    float high = smoothstep(highlight_edge - edge_softness, highlight_edge + edge_softness, facing);
    vec3 shade = mix(shadow_tint.rgb, vec3(0.72), mid);
    shade = mix(shade, vec3(1.0), high);
    DIFFUSE_LIGHT += ALBEDO * shade * LIGHT_COLOR * ATTENUATION / 3.14159265;
}
