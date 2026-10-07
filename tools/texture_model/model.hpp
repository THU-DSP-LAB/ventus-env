// Standalone mathematical texture model. These types are not a device ABI.
#pragma once

#include <array>
#include <cstdint>
#include <limits>
#include <vector>

namespace ventus::texture {

enum class Format { RGBA8_UNORM, BGRA8_UNORM, RGBA8_SRGB, BGRA8_SRGB };
constexpr bool is_bgra(Format format) {
  return format == Format::BGRA8_UNORM || format == Format::BGRA8_SRGB;
}
constexpr bool is_srgb(Format format) {
  return format == Format::RGBA8_SRGB || format == Format::BGRA8_SRGB;
}
enum class AddressMode { Repeat, MirroredRepeat, ClampToEdge, ClampToBorder };
enum class BorderColor { TransparentBlack, OpaqueBlack, OpaqueWhite };
enum class Filter { Nearest, Bilinear };
enum class MipFilter { Nearest, Linear };
enum class Component { Identity, Zero, One, R, G, B, A };
using ComponentMapping = std::array<Component, 4>;
using Color = std::array<float, 4>;
using TexelOffset = std::array<std::int32_t, 2>;
inline constexpr std::int32_t min_texel_offset = -8, max_texel_offset = 7;

struct Level {
  std::uint32_t width;
  std::uint32_t height;
  std::uint64_t offset;
  std::uint64_t row_pitch;
  std::uint64_t layer_pitch = 0;
};

struct Image {
  Format format;
  std::vector<std::uint8_t> data;
  // A partial or complete conventional mip chain, starting at mip 0.
  std::vector<Level> levels;
  std::uint32_t layer_count = 1;
};

struct Sampling {
  AddressMode address_u = AddressMode::Repeat;
  AddressMode address_v = AddressMode::Repeat;
  Filter mag_filter = Filter::Nearest;
  Filter min_filter = Filter::Nearest;
  MipFilter mip_filter = MipFilter::Nearest;
  BorderColor border_color = BorderColor::TransparentBlack;
  // E01 finite sampler bias profile. Nonzero bias uses a binary32 LOD sum.
  double lod_bias = 0;
  static constexpr double max_lod_bias = 16;

  Sampling() = default;
  // Existing equal-filter call sites retain their meaning.
  Sampling(AddressMode u, AddressMode v, Filter filter,
           MipFilter mip = MipFilter::Nearest)
      : address_u(u), address_v(v), mag_filter(filter), min_filter(filter), mip_filter(mip) {}
  Sampling(AddressMode u, AddressMode v, Filter mag, Filter min,
           MipFilter mip = MipFilter::Nearest)
      : address_u(u), address_v(v), mag_filter(mag), min_filter(min), mip_filter(mip) {}
};

struct View {
  std::uint32_t base_mip;
  std::uint32_t level_count;
  ComponentMapping components{};
  std::uint32_t base_layer = 0;
  std::uint32_t layer_count = 1;
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
  std::uint32_t mip = 0;
  std::uint32_t layer = 0;
  bool border = false; // No image byte address for a border-replacement tap.
};

struct Result {
  Color rgba{};
  std::uint32_t mip = 0;
  // Absolute image levels, with mip naming the first level for compatibility.
  std::uint32_t mip_upper = 0;
  std::uint32_t layer = 0;
  double input_layer = 0;
  double mip_weight = 0;
  double input_u = 0;
  double input_v = 0;
  double input_lod = 0;
  double sampler_lod = 0;
  double clamped_lod = 0;
  bool from_cube = false;
  std::array<double,3> input_direction{};
  std::uint32_t cube_face = 0;
  bool from_gradients = false;
  double footprint_squared = 0;
  Filter filter = Filter::Nearest;
  TexelOffset texel_offset{0, 0};
  std::vector<Tap> taps;
};

// Throws std::invalid_argument for inputs outside this model's profile.
void validate(const Image &image);
// Direct single-level helper uses mag_filter; min/mip selection needs sample_lod.
Result sample_level(const Image &image, std::uint32_t mip,
                    const Sampling &sampling, double u, double v, TexelOffset offset = {},
                    std::uint32_t layer = 0);
// Explicit LOD is relative to the view. Mip-nearest uses Vulkan's preferred
// tie rule (an exact half chooses the lower mip). Mip-linear blends adjacent
// levels after sampler/view clamps. Sampler bias precedes both clamps;
// zero bias preserves the original double-precision LOD helper behavior.
Result sample_lod(const Image &image, View view, const Sampling &sampling,
                  double u, double v, double lod, LodRange range = {},
                  TexelOffset offset = {}, double layer = 0);
// Six square layer planes in a Cube view, face order +X,-X,+Y,-Y,+Z,-Z.
// Finite nonzero binary32 direction; preferred Z>Y>X major ties. Bilinear
// crosses face edges; a corner tap averages all three incident face corners.
Result sample_cube_lod(const Image &image, View view, const Sampling &sampling,
                       std::array<double,3> direction, double lod, LodRange range = {});
std::array<std::uint32_t,2> texture_cube_size(const Image &image, View view, std::int32_t lod);

// Explicit normalized binary32 coordinate derivatives, isotropic Euclidean
// footprint at view base dimensions. Zero footprint selects the lower clamp.
Result sample_grad(const Image &image, View view, const Sampling &sampling,
                   double u, double v, std::array<double,2> dx,
                   std::array<double,2> dy, LodRange range = {},
                   TexelOffset offset = {}, double layer = 0);
// Integer operations use relative view LOD and never consult a sampler.
// Coordinates and LOD must be in range; invalid input throws.
Result texel_fetch(const Image &image, View view, std::int32_t x,
                   std::int32_t y, std::int32_t lod, TexelOffset offset = {},
                   std::int32_t layer = 0);
std::array<std::uint32_t, 2> texture_size(const Image &image, View view,
                                          std::int32_t lod);
std::array<std::uint32_t, 3> texture_array_size(const Image &image, View view,
                                              std::int32_t lod);
std::uint32_t texture_query_levels(const Image &image, View view);

} // namespace ventus::texture
