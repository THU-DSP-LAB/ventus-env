// Standalone mathematical texture model. These types are not a device ABI.
#pragma once

#include <array>
#include <cstdint>
#include <limits>
#include <vector>

namespace ventus::texture {

enum class Format { RGBA8_UNORM, BGRA8_UNORM };
enum class AddressMode { Repeat, MirroredRepeat, ClampToEdge };
enum class Filter { Nearest, Bilinear };
using Color = std::array<float, 4>;

struct Level {
  std::uint32_t width;
  std::uint32_t height;
  std::uint64_t offset;
  std::uint64_t row_pitch;
};

struct Image {
  Format format;
  std::vector<std::uint8_t> data;
  // A partial or complete conventional mip chain, starting at mip 0.
  std::vector<Level> levels;
};

struct Sampling {
  AddressMode address_u = AddressMode::Repeat;
  AddressMode address_v = AddressMode::Repeat;
  // A single filter: the first profile requires equal min/mag filters.
  Filter filter = Filter::Nearest;
};

struct View {
  std::uint32_t base_mip;
  std::uint32_t level_count;
};

struct LodRange {
  double min = 0.0;
  double max = std::numeric_limits<double>::max();
};

struct Tap {
  // Before and after integer texel addressing; raw coordinates follow the
  // periodic reduction of UV, keeping huge finite coordinates representable.
  std::int64_t raw_x;
  std::int64_t raw_y;
  std::uint32_t x;
  std::uint32_t y;
  std::uint64_t byte_offset;
  double weight;
  Color color;
};

struct Result {
  Color rgba{};
  std::uint32_t mip = 0;
  double input_u = 0;
  double input_v = 0;
  double input_lod = 0;
  double clamped_lod = 0;
  std::vector<Tap> taps;
};

// Throws std::invalid_argument for inputs outside this model's profile.
void validate(const Image &image);
Result sample_level(const Image &image, std::uint32_t mip,
                    const Sampling &sampling, double u, double v);
// Explicit LOD is relative to the view. Mip-nearest uses Vulkan's preferred
// tie rule (an exact half chooses the lower mip); no LOD bias is modeled.
Result sample_lod(const Image &image, View view, const Sampling &sampling,
                  double u, double v, double lod, LodRange range = {});

} // namespace ventus::texture
