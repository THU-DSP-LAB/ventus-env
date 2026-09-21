#version 460
#extension GL_EXT_ray_tracing : require
layout(location=0) rayPayloadInEXT uvec4 hit;
void main() { hit=uvec4(0); }
