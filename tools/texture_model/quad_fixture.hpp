#pragma once
#include "model.hpp"

namespace ventus::texture {
// Sixteen independent logical quads, eight per physical 32-lane warp.
// Non-affine corners distinguish fine rows/columns from coarse slot-zero edges.
inline Quad explicit_quad_fixture(unsigned group) {
  constexpr std::array<float,16> slopes{0,0x1p-12f,0x1p-8f,0x1p-5f,
      0x1p-3f,.25f,.5f,1.f,-0x1p-8f,-0x1p-4f,-.25f,-.5f,
      1.5f,2.f,4.f,8.f};
  const float s=slopes[group%slopes.size()];
  const float u=.3125f+static_cast<float>(group%3)*.0625f;
  const float v=.1875f+static_cast<float>(group%5)*.03125f;
  return Quad{{{{u,v},{u+s,v-s*.5f},{u+s*.25f,v+s},
                {u+s*1.75f,v+s*1.375f}}}};
}
} // namespace ventus::texture
