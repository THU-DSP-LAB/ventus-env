"""Shader construction for the private complete-quad numerical experiment.

Uses existing NIR quad operations and existing Ventus shuffle instructions.
The host does not provide derivatives; all 64 invocations are active, with
uniform control flow and one common image/sampler. This is not a Vulkan
subgroup capability advertisement or a fragment execution environment.
"""
import re


def shader(text: str, mode: str, wrong_neighbor: bool, wrong_sign: bool) -> str:
    text = text.replace('#version 460', '#version 460\n'
                        '#extension GL_KHR_shader_subgroup_basic : require\n'
                        '#extension GL_KHR_shader_subgroup_quad : require')
    if mode == 'fine':
        horizontal = 'subgroupQuadSwapVertical' if wrong_neighbor else 'subgroupQuadSwapHorizontal'
        dx = 'quad_h - quad_uv' if wrong_sign else '((i & 1u) == 0u ? quad_h - quad_uv : quad_uv - quad_h)'
        declarations = f'''
    vec2 quad_uv = requests.uv[2*i].xy;
    vec2 quad_h = {horizontal}(quad_uv);
    vec2 quad_v = subgroupQuadSwapVertical(quad_uv);
    vec2 gradient_x = {dx};
    vec2 gradient_y = (i & 2u) == 0u ? quad_v - quad_uv : quad_uv - quad_v;'''
    elif mode == 'coarse':
        target = 2 if wrong_neighbor else 1
        dx = 'quad_0 - quad_1' if wrong_sign else 'quad_1 - quad_0'
        declarations = f'''
    vec2 quad_uv = requests.uv[2*i].xy;
    vec2 quad_0 = subgroupQuadBroadcast(quad_uv, 0u);
    vec2 quad_1 = subgroupQuadBroadcast(quad_uv, {target}u);
    vec2 quad_2 = subgroupQuadBroadcast(quad_uv, 2u);
    vec2 gradient_x = {dx};
    vec2 gradient_y = quad_2 - quad_0;'''
    else:
        raise ValueError('invalid quad mode')
    text, count = re.subn(r'\n    vec2 gradient_x = [^\n]+;\n    vec2 gradient_y = [^\n]+;',
                         declarations, text)
    if count != 1:
        raise RuntimeError('quad shader must replace exactly one host-gradient declaration')
    if 'requests.uv[2*i].zw' in text or 'requests.uv[2*i+1].xy' in text:
        raise RuntimeError('quad shader must not consume host derivatives')
    # Diagnostic LOD uses the already validated binary32 polynomial. It is
    # compared with an independent host log2; color is still real textureGrad.
    helper = '''float quad_debug_lod(float q) {
    q = clamp(q, 2.3283064365386963e-10, 1.152921504606847e18);
    uint bits = floatBitsToUint(q);
    int exponent = int(bits >> 23u) - 127;
    float m = uintBitsToFloat((bits & 0x7fffffu) | 0x3f800000u);
    float z = (m - 1.0) / (m + 1.0);
    float z2 = z*z;
    float poly = 1.0/13.0;
    poly = 1.0/11.0 + z2*poly;
    poly = 1.0/9.0 + z2*poly;
    poly = 1.0/7.0 + z2*poly;
    poly = 1.0/5.0 + z2*poly;
    poly = 1.0/3.0 + z2*poly;
    poly = 1.0 + z2*poly;
    return 0.5*(float(exponent) + z*poly*2.8853900817779268);
}
'''
    text = text.replace('void main() {', helper + 'void main() {')
    for buffer in ('direct','indirect'):
        text = text.replace(buffer+'.values[2 * i + 1]',buffer+'.values[4 * i + 1]')
        text = text.replace(buffer+'.values[2 * i]',buffer+'.values[4 * i]')
    diagnostics = '''    ivec2 quad_size = textureSize(source, 0);
    vec2 scaled_dx = gradient_x*vec2(quad_size);
    vec2 scaled_dy = gradient_y*vec2(quad_size);
    float quad_q = max(dot(scaled_dx, scaled_dx), dot(scaled_dy, scaled_dy));
    vec4 quad_gradient = vec4(gradient_x, gradient_y);
    vec4 quad_meta = vec4(quad_q, quad_debug_lod(quad_q), vec2(quad_size));
    direct.values[4*i+2] = quad_gradient;
    direct.values[4*i+3] = quad_meta;
    indirect.values[4*i+2] = quad_gradient;
    indirect.values[4*i+3] = quad_meta;
'''
    text = text.replace('    imageStore(carrier,',diagnostics+'    imageStore(carrier,')
    return text
