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
              mode == AddressMode::ClampToEdge,
          "unsupported address mode");
}

// Reduce before multiplying by image dimensions or converting to an integer.
// This avoids undefined float-to-integer conversions for huge finite UVs.
double reduce(double value, AddressMode mode) {
  if (mode == AddressMode::ClampToEdge)
    return std::clamp(value, 0.0, 1.0);
  const double period = mode == AddressMode::Repeat ? 1.0 : 2.0;
  double reduced = std::fmod(value, period);
  if (reduced < 0)
    reduced += period;
  return reduced;
}

std::uint32_t address(std::int64_t i, std::uint32_t extent,
                      AddressMode mode) {
  const std::int64_t n = extent;
  if (mode == AddressMode::ClampToEdge)
    return static_cast<std::uint32_t>(std::clamp(i, std::int64_t{0}, n - 1));
  const std::int64_t period = mode == AddressMode::Repeat ? n : 2 * n;
  std::int64_t wrapped = i % period;
  if (wrapped < 0)
    wrapped += period;
  if (mode == AddressMode::MirroredRepeat && wrapped >= n)
    wrapped = period - 1 - wrapped;
  return static_cast<std::uint32_t>(wrapped);
}

Tap read(const Image &image, const Level &level, const Sampling &sampling,
         std::int64_t raw_x, std::int64_t raw_y, double weight) {
  Tap tap{};
  tap.raw_x = raw_x;
  tap.raw_y = raw_y;
  tap.x = address(raw_x, level.width, sampling.address_u);
  tap.y = address(raw_y, level.height, sampling.address_v);
  tap.byte_offset = level.offset + tap.y * level.row_pitch + tap.x * 4ULL;
  tap.weight = weight;
  // Byte reads work at arbitrary host alignment and do not assume endianness.
  const auto *p = image.data.data() + tap.byte_offset;
  const bool bgra = image.format == Format::BGRA8_UNORM;
  tap.color = {p[bgra ? 2 : 0] / 255.0f, p[1] / 255.0f,
               p[bgra ? 0 : 2] / 255.0f, p[3] / 255.0f};
  return tap;
}

Result sample_validated(const Image &image, std::uint32_t mip,
                        const Sampling &sampling, double u, double v) {
  require(std::isfinite(u) && std::isfinite(v), "UV must be finite");
  check_mode(sampling.address_u);
  check_mode(sampling.address_v);
  require(sampling.filter == Filter::Nearest || sampling.filter == Filter::Bilinear,
          "unsupported filter");
  require(mip < image.levels.size(), "mip index out of range");
  const auto &level = image.levels[mip];
  const double x = reduce(u, sampling.address_u) * level.width;
  const double y = reduce(v, sampling.address_v) * level.height;
  Result result;
  result.mip = mip;
  result.mip_upper = mip;
  result.input_u = u;
  result.input_v = v;
  if (sampling.filter == Filter::Nearest) {
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
  require(image.format == Format::RGBA8_UNORM || image.format == Format::BGRA8_UNORM,
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
  return sample_validated(image, mip, sampling, u, v);
}

Result sample_lod(const Image &image, View view, const Sampling &sampling,
                  double u, double v, double lod, LodRange range) {
  validate(image);
  require(view.level_count > 0 && view.base_mip < image.levels.size() &&
              view.level_count <= image.levels.size() - view.base_mip,
          "view mip range outside image");
  require(std::isfinite(lod), "LOD must be finite");
  require(std::isfinite(range.min) && std::isfinite(range.max) &&
              range.min >= 0 && range.min <= range.max,
          "invalid LOD range");
  const double clamped = std::clamp(lod, range.min, range.max);
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
  Result result = sample_validated(image, view.base_mip + selected, sampling, u, v);
  if (linear) {
    result.mip_upper = view.base_mip + std::min(selected + 1, view.level_count - 1);
    result.mip_weight = relative - low;
    // Exact integer/end LODs retain the first sample without needless reads.
    if (result.mip_weight > 0 && result.mip_upper != result.mip) {
      auto upper = sample_validated(image, result.mip_upper, sampling, u, v);
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
  result.clamped_lod = relative;
  return result;
}

} // namespace ventus::texture
