#include "model.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace ventus::texture {
namespace {

void require(bool condition, const char *message) {
  if (!condition)
    throw std::invalid_argument(message);
}

void check_mode(AddressMode mode) {
  require(mode == AddressMode::Repeat || mode == AddressMode::MirroredRepeat ||
              mode == AddressMode::ClampToEdge || mode == AddressMode::ClampToBorder,
          "unsupported address mode");
}

void check_filters(const Sampling &sampling) {
  require((sampling.mag_filter == Filter::Nearest || sampling.mag_filter == Filter::Bilinear) &&
              (sampling.min_filter == Filter::Nearest || sampling.min_filter == Filter::Bilinear),
          "unsupported min/mag filter");
  require(sampling.border_color == BorderColor::TransparentBlack ||
              sampling.border_color == BorderColor::OpaqueBlack ||
              sampling.border_color == BorderColor::OpaqueWhite, "unsupported border color");
}

Color border_color(BorderColor color) {
  if (color == BorderColor::OpaqueWhite) return {1,1,1,1};
  if (color == BorderColor::OpaqueBlack) return {0,0,0,1};
  return {0,0,0,0};
}

// Reduce before multiplying by image dimensions or converting to an integer.
// This avoids undefined float-to-integer conversions for huge finite UVs.
double reduce(double value, AddressMode mode) {
  if (mode == AddressMode::ClampToEdge)
    return std::clamp(value, 0.0, 1.0);
  if (mode == AddressMode::ClampToBorder)
    return std::clamp(value, -1.0, 2.0);
  const double period = mode == AddressMode::Repeat ? 1.0 : 2.0;
  double reduced = std::fmod(value, period);
  if (reduced < 0)
    reduced += period;
  return reduced;
}

std::uint32_t address(std::int64_t i, std::uint32_t extent,
                      AddressMode mode) {
  const std::int64_t n = extent;
  if (mode == AddressMode::ClampToEdge || mode == AddressMode::ClampToBorder)
    return static_cast<std::uint32_t>(std::clamp(i, std::int64_t{0}, n - 1));
  const std::int64_t period = mode == AddressMode::Repeat ? n : 2 * n;
  std::int64_t wrapped = i % period;
  if (wrapped < 0)
    wrapped += period;
  if (mode == AddressMode::MirroredRepeat && wrapped >= n)
    wrapped = period - 1 - wrapped;
  return static_cast<std::uint32_t>(wrapped);
}

Tap read_at(const Image &image, const Level &level, std::uint32_t x,
            std::uint32_t y, double weight) {
  Tap tap{};
  tap.raw_x = tap.x = x;
  tap.raw_y = tap.y = y;
  tap.byte_offset = level.offset + tap.y * level.row_pitch + tap.x * 4ULL;
  tap.weight = weight;
  // Byte reads work at arbitrary host alignment and do not assume endianness.
  const auto *p = image.data.data() + tap.byte_offset;
  const bool bgra = is_bgra(image.format);
  tap.color = {p[bgra ? 2 : 0] / 255.0f, p[1] / 255.0f,
               p[bgra ? 0 : 2] / 255.0f, p[3] / 255.0f};
  if (is_srgb(image.format)) {
    for (unsigned c = 0; c < 3; ++c) {
      const double encoded = p[bgra && c != 1 ? 2-c : c] / 255.0;
      tap.color[c] = static_cast<float>(encoded <= 0.04045 ? encoded / 12.92 :
                                       std::pow((encoded + 0.055) / 1.055, 2.4));
    }
  }
  return tap;
}

Tap read(const Image &image, const Level &level, const Sampling &sampling,
         std::int64_t raw_x, std::int64_t raw_y, double weight) {
  if ((sampling.address_u == AddressMode::ClampToBorder &&
       (raw_x < 0 || raw_x >= level.width)) ||
      (sampling.address_v == AddressMode::ClampToBorder &&
       (raw_y < 0 || raw_y >= level.height))) {
    Tap tap{};
    tap.raw_x = raw_x; tap.raw_y = raw_y;
    tap.weight = weight; tap.border = true;
    tap.color = border_color(sampling.border_color);
    return tap;
  }
  auto tap = read_at(image, level, address(raw_x, level.width, sampling.address_u),
                     address(raw_y, level.height, sampling.address_v), weight);
  tap.raw_x = raw_x;
  tap.raw_y = raw_y;
  return tap;
}

void validate_view(const Image &image, View view) {
  require(view.level_count > 0 && view.base_mip < image.levels.size() &&
              view.level_count <= image.levels.size() - view.base_mip,
          "view mip range outside image");
  for (auto component : view.components)
    require(component >= Component::Identity && component <= Component::A,
            "unsupported view component mapping");
}

Color swizzle(Color color, ComponentMapping mapping) {
  Color output{};
  for (unsigned c = 0; c < 4; ++c) {
    const auto selector = mapping[c];
    output[c] = selector == Component::Identity ? color[c] :
                selector == Component::Zero ? 0 : selector == Component::One ? 1 :
                color[static_cast<unsigned>(selector) - static_cast<unsigned>(Component::R)];
  }
  return output;
}

void swizzle_result(Result &result, ComponentMapping mapping) {
  result.rgba = swizzle(result.rgba, mapping);
  for (auto &tap : result.taps) tap.color = swizzle(tap.color, mapping);
}

Result sample_validated(const Image &image, std::uint32_t mip,
                        const Sampling &sampling, double u, double v, Filter filter) {
  require(std::isfinite(u) && std::isfinite(v), "UV must be finite");
  check_mode(sampling.address_u);
  check_mode(sampling.address_v);
  require(mip < image.levels.size(), "mip index out of range");
  const auto &level = image.levels[mip];
  const double x = reduce(u, sampling.address_u) * level.width;
  const double y = reduce(v, sampling.address_v) * level.height;
  Result result;
  result.mip = mip;
  result.mip_upper = mip;
  result.input_u = u;
  result.input_v = v;
  result.filter = filter;
  if (filter == Filter::Nearest) {
    result.taps.push_back(read(image, level, sampling,
                              static_cast<std::int64_t>(std::floor(x)),
                              static_cast<std::int64_t>(std::floor(y)), 1));
    result.rgba = result.taps[0].color;
    result.taps[0].mip = mip;
    return result;
  }
  const double sx = x - 0.5;
  const double sy = y - 0.5;
  const double fx = std::floor(sx);
  const double fy = std::floor(sy);
  const auto ix = static_cast<std::int64_t>(fx);
  const auto iy = static_cast<std::int64_t>(fy);
  const double a = sx - fx;
  const double b = sy - fy;
  result.taps = {read(image, level, sampling, ix, iy, (1 - a) * (1 - b)),
                 read(image, level, sampling, ix + 1, iy, a * (1 - b)),
                 read(image, level, sampling, ix, iy + 1, (1 - a) * b),
                 read(image, level, sampling, ix + 1, iy + 1, a * b)};
  for (auto &tap : result.taps) tap.mip = mip;
  for (unsigned c = 0; c < 4; ++c) {
    double value = 0;
    for (const auto &tap : result.taps)
      value += tap.weight * tap.color[c];
    result.rgba[c] = static_cast<float>(value);
  }
  return result;
}

} // namespace

void validate(const Image &image) {
  require(image.format == Format::RGBA8_UNORM || image.format == Format::BGRA8_UNORM ||
              image.format == Format::RGBA8_SRGB || image.format == Format::BGRA8_SRGB,
          "unsupported format");
  require(!image.levels.empty(), "image needs at least one mip");
  for (std::size_t i = 0; i < image.levels.size(); ++i) {
    const auto &level = image.levels[i];
    require(level.width > 0 && level.height > 0, "mip dimensions must be positive");
    if (i > 0) {
      const auto &previous = image.levels[i - 1];
      require(previous.width != 1 || previous.height != 1,
              "mip chain continues past 1x1");
      require(level.width == std::max(1U, previous.width / 2) &&
                  level.height == std::max(1U, previous.height / 2),
              "mip dimensions do not follow the conventional chain");
    }
    const std::uint64_t row_bytes = level.width * 4ULL;
    require(level.row_pitch >= row_bytes, "row pitch is smaller than one row");
    // Only accessed texel bytes must be present; final row padding is optional.
    require(level.offset <= image.data.size(), "mip offset outside backing bytes");
    const std::uint64_t remaining = image.data.size() - level.offset;
    require(row_bytes <= remaining, "backing bytes do not contain the first row");
    if (level.height > 1)
      require(level.row_pitch <= (remaining - row_bytes) / (level.height - 1),
              "backing bytes do not contain all rows");
  }
}

Result sample_level(const Image &image, std::uint32_t mip,
                    const Sampling &sampling, double u, double v) {
  validate(image);
  check_filters(sampling);
  return sample_validated(image, mip, sampling, u, v, sampling.mag_filter);
}

Result sample_lod(const Image &image, View view, const Sampling &sampling,
                  double u, double v, double lod, LodRange range) {
  validate(image);
  check_filters(sampling);
  validate_view(image, view);
  if (sampling.border_color == BorderColor::OpaqueBlack)
    for (unsigned c=0;c<4;++c)
      require(view.components[c] == Component::Identity ||
                  static_cast<unsigned>(view.components[c]) == static_cast<unsigned>(Component::R)+c,
              "opaque black with nonidentity swizzle is outside the core sampling profile");
  require(std::isfinite(lod), "LOD must be finite");
  require(std::isfinite(range.min) && std::isfinite(range.max) &&
              range.min >= 0 && range.min <= range.max,
          "invalid LOD range");
  require(std::isfinite(sampling.lod_bias) &&
              std::abs(sampling.lod_bias) <= Sampling::max_lod_bias,
          "invalid sampler LOD bias");
  // Match the software GPU binary32 addition at mip/filter boundaries. Keep
  // the original zero-bias high-precision helper for existing callers.
  const double biased = sampling.lod_bias == 0 ? lod :
      static_cast<double>(static_cast<float>(lod) + static_cast<float>(sampling.lod_bias));
  const double clamped = std::clamp(biased, range.min, range.max);
  // Vulkan chooses min/mag using lambda after sampler clamp, before view
  // availability and mip rounding. Even a one-level view can be minified.
  const Filter filter = clamped <= 0 ? sampling.mag_filter : sampling.min_filter;
  const double relative = std::clamp(clamped, 0.0,
                                    static_cast<double>(view.level_count - 1));
  // Preferred nearest(d') = ceil(d' + 0.5) - 1. Exact half selects lower.
  // Express the decision without addition that could round nextafter(0.5, 1)
  // back onto the half boundary.
  const double low = std::floor(relative);
  require(sampling.mip_filter == MipFilter::Nearest ||
              sampling.mip_filter == MipFilter::Linear, "unsupported mip filter");
  const bool linear = sampling.mip_filter == MipFilter::Linear;
  const auto selected = static_cast<std::uint32_t>(low + (!linear && relative - low > 0.5));
  Result result = sample_validated(image, view.base_mip + selected, sampling, u, v, filter);
  if (linear) {
    result.mip_upper = view.base_mip + std::min(selected + 1, view.level_count - 1);
    result.mip_weight = relative - low;
    // Exact integer/end LODs retain the first sample without needless reads.
    if (result.mip_weight > 0 && result.mip_upper != result.mip) {
      auto upper = sample_validated(image, result.mip_upper, sampling, u, v, filter);
      for (unsigned c = 0; c < 4; ++c)
        result.rgba[c] = static_cast<float>((1 - result.mip_weight) * result.rgba[c] +
                                           result.mip_weight * upper.rgba[c]);
      for (auto &tap : result.taps) tap.weight *= 1 - result.mip_weight;
      for (auto &tap : upper.taps) {
        tap.weight *= result.mip_weight;
        result.taps.push_back(tap);
      }
    }
  }
  result.input_lod = lod;
  result.sampler_lod = clamped;
  result.clamped_lod = relative;
  swizzle_result(result, view.components);
  return result;
}

Result sample_grad(const Image &image, View view, const Sampling &sampling,
                   double u, double v, std::array<double,2> dx,
                   std::array<double,2> dy, LodRange range) {
  validate(image);
  validate_view(image, view);
  const auto &base=image.levels[view.base_mip];
  auto square=[&](std::array<double,2> derivative) {
    for (double d : derivative)
      require(std::isfinite(d) && std::abs(d)<=std::numeric_limits<float>::max(),
              "gradient must be finite binary32");
    const float x=static_cast<float>(derivative[0])*static_cast<float>(base.width);
    const float y=static_cast<float>(derivative[1])*static_cast<float>(base.height);
    return static_cast<float>(x*x+y*y);
  };
  const float squared=std::max(square(dx),square(dy));
  // Infinite/zero binary32 footprint has an unambiguous sampler clamp, but
  // sample_lod keeps its explicit finite-input contract. Use finite sentinels
  // for that call and preserve the derived limiting LOD in diagnostics.
  const double derived=squared==0 ? -std::numeric_limits<double>::infinity() :
      0.5*std::log2(static_cast<double>(squared));
  const double lod=std::isfinite(derived) ? static_cast<float>(derived) :
      (squared==0 ? -std::numeric_limits<float>::max() : std::numeric_limits<float>::max());
  Result result=sample_lod(image,view,sampling,u,v,lod,range);
  result.from_gradients=true;
  result.footprint_squared=squared;
  result.input_lod=derived;
  return result;
}

std::uint32_t texture_query_levels(const Image &image, View view) {
  validate(image);
  validate_view(image, view);
  return view.level_count;
}

std::array<std::uint32_t, 2> texture_size(const Image &image, View view,
                                         std::int32_t lod) {
  texture_query_levels(image, view);
  require(lod >= 0 && static_cast<std::uint32_t>(lod) < view.level_count,
          "integer LOD outside view");
  const auto &level = image.levels[view.base_mip + lod];
  return {level.width, level.height};
}

Result texel_fetch(const Image &image, View view, std::int32_t x,
                   std::int32_t y, std::int32_t lod) {
  const auto size = texture_size(image, view, lod);
  require(x >= 0 && y >= 0 && static_cast<std::uint32_t>(x) < size[0] &&
              static_cast<std::uint32_t>(y) < size[1], "integer coordinate outside mip");
  Result result;
  result.mip = result.mip_upper = view.base_mip + lod;
  auto tap = read_at(image, image.levels[result.mip], x, y, 1);
  tap.mip = result.mip;
  result.rgba = tap.color;
  result.taps.push_back(tap);
  result.input_lod = lod;
  swizzle_result(result, view.components);
  return result;
}

} // namespace ventus::texture
