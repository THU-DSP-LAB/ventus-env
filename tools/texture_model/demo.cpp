#include "model.hpp"

#include <iomanip>
#include <iostream>

using namespace ventus::texture;

int main() {
  // Two texels, red and blue. At u=0 repeat mixes the opposite edges,
  // whereas mirror and clamp both keep the red edge.
  const Image image{Format::RGBA8_UNORM,
                    {255, 0, 0, 255, 0, 0, 255, 255}, {{2, 1, 0, 8}}};
  const AddressMode modes[]{AddressMode::Repeat, AddressMode::MirroredRepeat,
                             AddressMode::ClampToEdge};
  const char *names[]{"repeat", "mirrored-repeat", "clamp-to-edge"};
  std::cout << std::fixed << std::setprecision(4);
  for (unsigned i = 0; i < 3; ++i) {
    const Sampling sampling{modes[i], AddressMode::ClampToEdge, Filter::Bilinear};
    const auto result = sample_level(image, 0, sampling, 0, 0.5);
    std::cout << names[i] << " u=0 v=0.5 mip=" << result.mip << " rgba=(";
    for (unsigned c = 0; c < 4; ++c)
      std::cout << (c ? ", " : "") << result.rgba[c];
    std::cout << ")\n";
    for (const auto &tap : result.taps)
      std::cout << "  raw=(" << tap.raw_x << ',' << tap.raw_y << ") addressed=("
                << tap.x << ',' << tap.y << ") byte_offset=" << tap.byte_offset
                << " weight=" << tap.weight << '\n';
  }
}
