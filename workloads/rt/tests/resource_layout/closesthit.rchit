#version 460
#extension GL_EXT_ray_tracing : require
layout(location=0) rayPayloadInEXT uvec4 hit;
hitAttributeEXT vec2 attrib;
void main() { hit=uvec4(gl_GeometryIndexEXT+1,gl_PrimitiveID+1,gl_InstanceCustomIndexEXT,gl_InstanceID); }
