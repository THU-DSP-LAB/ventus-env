#include "model.hpp"

#include <algorithm>
#include <cmath>
#include <functional>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>

using namespace ventus::texture;
namespace {

std::size_t checks = 0;
using Bytes = std::array<std::uint8_t, 4>;
constexpr Color red{1, 0, 0, 1};
constexpr Color green{0, 1, 0, 1};
constexpr Color blue{0, 0, 1, 1};
constexpr Color white{1, 1, 1, 1};

void expect(bool good, const std::string &message) {
  ++checks;
  if (!good)
    throw std::runtime_error(message);
}

void expect_color(const Color &actual, const Color &expected,
                  const std::string &message, double tolerance = 2e-7) {
  for (unsigned c = 0; c < 4; ++c)
    expect(std::isfinite(actual[c]) && std::abs(actual[c] - expected[c]) <= tolerance,
           message + " channel " + std::to_string(c));
}

void rejected(const std::function<void()> &action, const std::string &message) {
  try {
    action();
  } catch (const std::invalid_argument &) {
    expect(true, message);
    return;
  }
  expect(false, "input was not rejected: " + message);
}

void put(Image &image, const Level &level, unsigned x, unsigned y, Bytes rgba) {
  if (is_bgra(image.format))
    std::swap(rgba[0], rgba[2]);
  const auto offset = level.offset + y * level.row_pitch + x * 4;
  std::copy(rgba.begin(), rgba.end(), image.data.begin() + offset);
}

Image make_image(unsigned w, unsigned h, Format format = Format::RGBA8_UNORM,
                 unsigned padding = 0, unsigned offset = 0) {
  const Level level{w, h, offset, w * 4ULL + padding};
  Image image{format, {}, {level}};
  image.data.resize(offset + (h - 1) * level.row_pitch + w * 4, 0xcd);
  for (unsigned y = 0; y < h; ++y)
    for (unsigned x = 0; x < w; ++x)
      put(image, level, x, y,
          {static_cast<std::uint8_t>((x * 37 + y * 71 + 5) % 256),
           static_cast<std::uint8_t>((x * 89 + y * 11 + 23) % 256),
           static_cast<std::uint8_t>((x * 13 + y * 53 + 251) % 256),
           static_cast<std::uint8_t>((x * 31 + y * 29 + 19) % 256)});
  return image;
}

Image four_colors(Format format = Format::RGBA8_UNORM) {
  auto image = make_image(2, 2, format, 3, 5);
  const auto &level = image.levels[0];
  put(image, level, 0, 0, {255, 0, 0, 255});
  put(image, level, 1, 0, {0, 255, 0, 255});
  put(image, level, 0, 1, {0, 0, 255, 255});
  put(image, level, 1, 1, {255, 255, 255, 255});
  return image;
}

// Independent oracle: enumerate a virtual extended image and accumulate
// nearest-cell or tent-kernel contributions. Unlike the model, this does not
// reduce UV, select four taps, or use modular mirrored addressing.
std::vector<long double> axis_weights(unsigned n, long double u,
                                      AddressMode mode, Filter filter, int offset = 0) {
  std::vector<long double> weights(n);
  const long double position = u * n + offset;
  // Randomized inputs stay within [-3,3]; leave ample room for their kernels.
  for (int cell = -5 * static_cast<int>(n) - 8; cell <= 5 * static_cast<int>(n) + 8; ++cell) {
    long double weight;
    if (filter == Filter::Nearest)
      weight = position >= cell && position < cell + 1 ? 1 : 0;
    else
      weight = std::max(0.0L, 1 - std::abs(position - (cell + 0.5L)));
    if (weight == 0)
      continue;
    int index = cell;
    if (mode == AddressMode::Repeat) {
      while (index < 0)
        index += n;
      while (index >= static_cast<int>(n))
        index -= n;
    } else if (mode == AddressMode::MirroredRepeat) {
      while (index < 0 || index >= static_cast<int>(n)) {
        if (index < 0)
          index = -1 - index;
        else
          index = 2 * static_cast<int>(n) - 1 - index;
      }
    } else if (mode == AddressMode::ClampToBorder) {
      if (index < 0 || index >= static_cast<int>(n)) continue;
    } else {
      index = std::clamp(index, 0, static_cast<int>(n) - 1);
    }
    weights[index] += weight;
  }
  return weights;
}

Color oracle(const Image &image, unsigned mip, Sampling sampling, double u, double v, TexelOffset offset = {}) {
  const auto &level = image.levels[mip];
  const auto wx = axis_weights(level.width, u, sampling.address_u, sampling.mag_filter, offset[0]);
  const auto wy = axis_weights(level.height, v, sampling.address_v, sampling.mag_filter, offset[1]);
  std::array<long double, 4> total{};
  long double coverage_x=0,coverage_y=0;
  for (auto weight : wx) coverage_x+=weight;
  for (auto weight : wy) coverage_y+=weight;
  for (unsigned y = 0; y < level.height; ++y)
    for (unsigned x = 0; x < level.width; ++x) {
      const auto p = level.offset + y * level.row_pitch + x * 4;
      for (unsigned c = 0; c < 4; ++c) {
        const auto channel = is_bgra(image.format) && (c == 0 || c == 2)
                                 ? 2 - c : c;
        const long double encoded = image.data[p + channel] / 255.0L;
        const long double decoded = is_srgb(image.format) && c < 3
            ? (encoded <= 0.04045L ? encoded / 12.92L :
               std::pow((encoded + 0.055L) / 1.055L, 2.4L)) : encoded;
        total[c] += wx[x] * wy[y] * decoded;
      }
    }
  if (sampling.address_u == AddressMode::ClampToBorder || sampling.address_v == AddressMode::ClampToBorder) {
    const long double outside=1-coverage_x*coverage_y;
    if (sampling.border_color == BorderColor::OpaqueWhite)
      for (auto &value : total) value+=outside;
    else if (sampling.border_color == BorderColor::OpaqueBlack) total[3]+=outside;
  }
  return {static_cast<float>(total[0]), static_cast<float>(total[1]),
          static_cast<float>(total[2]), static_cast<float>(total[3])};
}

void hand_calculated() {
  for (Format format : {Format::RGBA8_UNORM, Format::BGRA8_UNORM}) {
    const auto image = four_colors(format);
    Sampling nearest;
    Sampling linear{AddressMode::Repeat, AddressMode::Repeat, Filter::Bilinear};
    expect_color(sample_level(image, 0, nearest, 0.25, 0.25).rgba, red, "top-left center");
    expect_color(sample_level(image, 0, nearest, 0.75, 0.25).rgba, green, "top-right center");
    expect_color(sample_level(image, 0, nearest, 0.25, 0.75).rgba, blue, "bottom-left center");
    expect_color(sample_level(image, 0, nearest, 0.75, 0.75).rgba, white, "bottom-right center");
    expect_color(sample_level(image, 0, linear, 0.5, 0.5).rgba,
                 {0.5, 0.5, 0.5, 1}, "four-point average");
    expect_color(sample_level(image, 0, linear, 0.375, 0.25).rgba,
                 {0.75, 0.25, 0, 1}, "one-quarter horizontal interpolation");
    expect_color(sample_level(image, 0, linear, 0.375, 0.375).rgba,
                 {0.625, 0.25, 0.25, 1}, "asymmetric 2D interpolation");
    expect_color(sample_level(image, 0, linear, 0.0, 0.25).rgba,
                 {0.5, 0.5, 0, 1}, "repeat seam blends opposite edges");
    linear.address_u = AddressMode::MirroredRepeat;
    expect_color(sample_level(image, 0, linear, 0.0, 0.25).rgba, red, "mirror seam left");
    expect_color(sample_level(image, 0, linear, 1.0, 0.25).rgba, green, "mirror seam right");
    linear.address_u = AddressMode::ClampToEdge;
    expect_color(sample_level(image, 0, linear, -2.0, 0.25).rgba, red, "clamp left");
    expect_color(sample_level(image, 0, linear, 3.0, 0.25).rgba, green, "clamp right");
    nearest.address_u = AddressMode::MirroredRepeat;
    nearest.address_v = AddressMode::Repeat;
    expect_color(sample_level(image, 0, nearest, 1.25, -0.25).rgba, white,
                 "independent U/V modes");
  }
}

void negative_and_boundary_coordinates() {
  const auto image = make_image(4, 1);
  Sampling sampling;
  const std::array<double, 9> coordinates{-1.125, -0.875, -0.625, -0.375,
                                         -0.125, 0.125, 1.125, 1.875, 2.125};
  const std::array<unsigned, 9> repeat{3, 0, 1, 2, 3, 0, 0, 3, 0};
  const std::array<unsigned, 9> mirrored{3, 3, 2, 1, 0, 0, 3, 0, 0};
  for (std::size_t i = 0; i < coordinates.size(); ++i) {
    sampling.address_u = AddressMode::Repeat;
    expect(sample_level(image, 0, sampling, coordinates[i], 0.5).taps[0].x == repeat[i],
           "repeat sequence at " + std::to_string(coordinates[i]));
    sampling.address_u = AddressMode::MirroredRepeat;
    expect(sample_level(image, 0, sampling, coordinates[i], 0.5).taps[0].x == mirrored[i],
           "mirror sequence at " + std::to_string(coordinates[i]));
  }
  sampling.address_u = AddressMode::Repeat;
  expect(sample_level(image, 0, sampling, std::nextafter(0.25, 0.0), 0.5).taps[0].x == 0,
         "nearest immediately before texel boundary");
  expect(sample_level(image, 0, sampling, 0.25, 0.5).taps[0].x == 1,
         "nearest exact texel boundary chooses upper");
  expect(sample_level(image, 0, sampling, std::nextafter(0.25, 1.0), 0.5).taps[0].x == 1,
         "nearest immediately after texel boundary");
  expect(sample_level(image, 0, sampling, -0.0, 0.5).taps[0].x == 0, "negative zero");
  expect(sample_level(image, 0, sampling, 1e300, 0.5).taps[0].x == 0, "huge repeat UV");
  expect(sample_level(image, 0, sampling, -1e300, 0.5).taps[0].x == 0, "huge negative UV");
  sampling.address_u = AddressMode::ClampToEdge;
  expect(sample_level(image, 0, sampling, 1e300, 0.5).taps[0].x == 3, "huge clamp UV");
  expect(sample_level(image, 0, sampling, -1e300, 0.5).taps[0].x == 0, "negative clamp UV");
}

void all_unorm_values() {
  for (auto format : {Format::RGBA8_UNORM, Format::BGRA8_UNORM}) {
    auto image = make_image(1, 1, format);
    for (unsigned value = 0; value <= 255; ++value) {
      put(image, image.levels[0], 0, 0,
          {static_cast<std::uint8_t>(value), static_cast<std::uint8_t>(255 - value),
           static_cast<std::uint8_t>(value), static_cast<std::uint8_t>(255 - value)});
      const Color expected{value / 255.0f, (255 - value) / 255.0f,
                           value / 255.0f, (255 - value) / 255.0f};
      expect_color(sample_level(image, 0, {}, 0.5, 0.5).rgba, expected, "UNORM decode", 0);
    }
  }
}

void randomized_oracle() {
  std::mt19937 generator(20261005);
  std::uniform_real_distribution<double> coordinates(-3, 3);
  const std::array<AddressMode, 3> modes{AddressMode::Repeat, AddressMode::MirroredRepeat,
                                       AddressMode::ClampToEdge};
  const std::array<std::array<unsigned, 2>, 5> sizes{{{1, 1}, {1, 5}, {5, 1},
                                                    {3, 5}, {4, 4}}};
  for (auto size : sizes)
    for (auto format : {Format::RGBA8_UNORM, Format::BGRA8_UNORM}) {
      const auto image = make_image(size[0], size[1], format, 7, 3);
      for (auto u_mode : modes)
        for (auto v_mode : modes)
          for (auto filter : {Filter::Nearest, Filter::Bilinear}) {
            Sampling sampling{u_mode, v_mode, filter};
            for (int trial = 0; trial < 20; ++trial) {
              const double u = coordinates(generator);
              const double v = coordinates(generator);
              const auto result = sample_level(image, 0, sampling, u, v);
              expect_color(result.rgba, oracle(image, 0, sampling, u, v),
                           "independent virtual-image oracle");
              double weight = 0;
              for (const auto &tap : result.taps) {
                weight += tap.weight;
                expect(tap.weight >= 0 && tap.weight <= 1, "valid trace weight");
                expect(tap.byte_offset + 4 <= image.data.size(), "trace within backing bytes");
              }
              expect(std::abs(weight - 1) < 1e-14, "trace weights sum to one");
              // Periodicity/mirror symmetry are independent of the oracle.
              if (u_mode == AddressMode::Repeat)
                expect_color(sample_level(image, 0, sampling, u + 2, v).rgba,
                             result.rgba, "repeat periodicity");
              if (u_mode == AddressMode::MirroredRepeat)
                expect_color(sample_level(image, 0, sampling, -u, v).rgba,
                             result.rgba, "mirror symmetry");
            }
          }
    }
}

Image mip_image() {
  Image image{Format::RGBA8_UNORM, std::vector<std::uint8_t>(128, 0xcd),
              {{5, 3, 3, 23}, {2, 1, 91, 11}, {1, 1, 115, 4}}};
  const std::array<Bytes, 3> colors{{{255, 0, 0, 255}, {0, 255, 0, 255},
                                   {0, 0, 255, 255}}};
  for (unsigned m = 0; m < image.levels.size(); ++m)
    for (unsigned y = 0; y < image.levels[m].height; ++y)
      for (unsigned x = 0; x < image.levels[m].width; ++x)
        put(image, image.levels[m], x, y, colors[m]);
  return image;
}

void lod_and_views() {
  const auto image = mip_image();
  const std::array<double, 9> lods{-100, 0, 0.5, std::nextafter(0.5, 1.0),
                                1, 1.5, std::nextafter(1.5, 2.0), 2, 1e300};
  const std::array<unsigned, 9> mips{0, 0, 0, 1, 1, 1, 2, 2, 2};
  const std::array<Color, 3> colors{red, green, blue};
  for (std::size_t i = 0; i < lods.size(); ++i) {
    const auto result = sample_lod(image, {0, 3}, {}, 0.3, 0.7, lods[i]);
    expect(result.mip == mips[i], "mip-nearest selection including exact ties");
    expect_color(result.rgba, colors[mips[i]], "selected mip data");
  }
  auto result = sample_lod(image, {1, 2}, {}, 0.3, 0.7, 0);
  expect(result.mip == 1, "LOD is relative to view");
  expect_color(result.rgba, green, "view base mip data");
  expect(sample_lod(image, {1, 1}, {}, 0.3, 0.7, 100).mip == 1, "single-mip view");
  expect(sample_lod(image, {0, 3}, {}, 0.3, 0.7, 0, {0.75, 2}).mip == 1,
         "sampler lower clamp");
  expect(sample_lod(image, {0, 3}, {}, 0.3, 0.7, 2, {0, 0.5}).mip == 0,
         "sampler upper clamp before preferred nearest selection");
  expect(sample_lod(image, {0, 3}, {}, 0.3, 0.7, -100, {10, 20}).mip == 2,
         "view availability clamps sampler range");
  // A separate image/sampler pair can be interleaved without hidden global state.
  const auto other = four_colors(Format::BGRA8_UNORM);
  expect_color(sample_level(other, 0, {}, 0.25, 0.25).rgba, red, "second image");
  expect_color(sample_lod(image, {1, 2}, {}, 0.3, 0.7, 0).rgba, green,
               "first image still unchanged");
  Sampling linear{AddressMode::Repeat, AddressMode::ClampToEdge, Filter::Bilinear};
  auto patterned = mip_image();
  put(patterned, patterned.levels[1], 0, 0, {255, 0, 0, 255});
  put(patterned, patterned.levels[1], 1, 0, {0, 0, 255, 255});
  const auto filtered = sample_lod(patterned, {1, 2}, linear, 0.5, 0.5, 0);
  expect_color(filtered.rgba, {0.5, 0, 0.5, 1}, "bilinear on nonzero view mip");
  expect(filtered.taps[0].byte_offset == 91 && filtered.taps[1].byte_offset == 95,
         "view mip uses its own byte offset and dimensions");
  expect_color(sample_lod(patterned, {1, 2}, linear, 0.5, 0.5, 1).rgba, blue,
               "next view mip has independent backing data");
  const auto partial = make_image(7, 3); // A mip chain need not reach 1x1.
  expect_color(sample_lod(partial, {0, 1}, linear, -0.1, 0.1, 20).rgba,
               sample_level(partial, 0, linear, -0.1, 0.1).rgba, "partial chain");
}

// Independent mip oracle: enumerate all view levels with a tent kernel in
// level space, then reuse the independent spatial oracle (not sample_level).
Color mapped_oracle(Color color, ComponentMapping mapping) {
  Color result{};
  for (unsigned c=0;c<4;++c) {
    switch (mapping[c]) {
    case Component::Identity: result[c]=color[c];break;
    case Component::Zero: result[c]=0;break;
    case Component::One: result[c]=1;break;
    case Component::R: result[c]=color[0];break;
    case Component::G: result[c]=color[1];break;
    case Component::B: result[c]=color[2];break;
    case Component::A: result[c]=color[3];break;
    }
  }
  return result;
}

Color lod_oracle(const Image &image, View view, Sampling sampling,
                 double u, double v, double lod, LodRange range, TexelOffset offset = {}) {
  long double position = lod;
  if (position < range.min) position = range.min;
  if (position > range.max) position = range.max;
  sampling.mag_filter = position > 0 ? sampling.min_filter : sampling.mag_filter;
  if (position < 0) position = 0;
  if (position > view.level_count - 1) position = view.level_count - 1;
  std::array<long double, 4> total{};
  for (unsigned level = 0; level < view.level_count; ++level) {
    const auto weight = sampling.mip_filter == MipFilter::Linear
        ? std::max(0.0L, 1 - std::abs(position - level))
        : (position > level - 0.5L && position <= level + 0.5L ? 1.0L : 0.0L);
    if (!weight) continue;
    const auto color = mapped_oracle(oracle(image, view.base_mip + level, sampling, u, v, offset),view.components);
    for (unsigned c = 0; c < 4; ++c) total[c] += weight * color[c];
  }
  return {static_cast<float>(total[0]), static_cast<float>(total[1]),
          static_cast<float>(total[2]), static_cast<float>(total[3])};
}

void lod_bias() {
  const auto solid = mip_image();
  Sampling state;
  state.lod_bias = 1;
  expect_color(sample_lod(solid,{0,3},state,0.3,0.7,0).rgba,green,"sampler bias applies to explicit Lod");
  state.lod_bias = -1;
  expect_color(sample_lod(solid,{0,3},state,0.3,0.7,1).rgba,red,"negative bias before clamp");
  state.lod_bias = 0.5;
  state.mip_filter = MipFilter::Linear;
  expect_color(sample_lod(solid,{0,3},state,0.3,0.7,0).rgba,{0.5,0.5,0,1},"bias before mip weights");
  expect_color(sample_lod(solid,{1,2},state,0.3,0.7,0).rgba,{0,0.5,0.5,1},"bias is relative to view");
  state.lod_bias = 1;
  expect_color(sample_lod(solid,{0,3},state,0.3,0.7,0,{0,0.25}).rgba,{0.75,0.25,0,1},"sampler clamp after bias");
  const std::array<float,11> lods{-100,-1,-0.f,0.f,0.25f,0.5f,std::nextafter(0.5f,1.f),1,1.5f,2,100};
  for (auto bias : {-16.f,-1.f,-0.5f,-0.f,0.f,0.25f,0.5f,1.f,16.f})
    for (auto view : {View{0,3},View{1,2},View{1,1}})
      for (auto mip : {MipFilter::Nearest,MipFilter::Linear})
        for (auto mag : {Filter::Nearest,Filter::Bilinear})
          for (auto min : {Filter::Nearest,Filter::Bilinear})
            for (auto range : {LodRange{0,1000},LodRange{0.75,1.75},LodRange{2,2}})
              for (float lod : lods) {
                Sampling sampling{AddressMode::Repeat,AddressMode::ClampToEdge,mag,min,mip};
                sampling.lod_bias=bias;
                // Explicit binary32 input addition, followed by a separate
                // per-level weighted-color oracle rather than sample_lod.
                const double position=static_cast<float>(lod+bias);
                auto r=sample_lod(solid,view,sampling,0.3,0.7,lod,range);
                expect_color(r.rgba,lod_oracle(solid,view,sampling,0.3,0.7,position,range),"independent bias level oracle");
                const auto expected=std::clamp(position,range.min,range.max);
                expect(r.sampler_lod==expected,"biased lambda diagnostic");
                expect(r.filter==(expected>0 ? min : mag),"bias changes min/mag before view clamp");
              }
  for (double bad : {std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity(),-std::numeric_limits<double>::infinity(),16.01,-16.01}) {
    state.lod_bias=bad;
    rejected([&]{sample_lod(solid,{0,3},state,0,0,0);},"invalid sampler bias");
  }
}

void explicit_gradients() {
  const auto image=mip_image();
  Sampling state;
  auto r=sample_grad(image,{0,3},state,0.3,0.7,{0,0},{0,0});
  expect(r.from_gradients && r.mip==0 && r.sampler_lod==0,"zero footprint lower clamp");
  expect(std::isinf(r.input_lod) && r.input_lod<0,"zero footprint diagnostic minus infinity");
  r=sample_grad(image,{0,3},state,0.3,0.7,{1.f/5,1.f/3},{0,0});
  expect(r.footprint_squared==2 && r.input_lod==0.5 && r.mip==0,"off-axis exact half tie");
  r=sample_grad(image,{0,3},state,0.3,0.7,{std::nextafter(1.f/5,1.f),1.f/3},{0,0});
  expect(r.mip==1,"above half gradient footprint");
  r=sample_grad(image,{1,2},state,0.3,0.7,{0.25,0},{0,0});
  expect(r.input_lod==-1 && r.mip==1,"scale by view base width rather than image mip0");
  state.lod_bias=16;
  expect(sample_grad(image,{0,3},state,0.3,0.7,{0,0},{0,0}).mip==0,"zero footprint remains lower even with max bias");
  const std::array<std::array<float,2>,12> derivatives{{{0,0},{1.f/64,0},{0,-0.25f},{1.f/5,1.f/3},
      {0.5f,-0.5f},{1,2},{0.3f,0.2f},{-16,16},{0x1p-100f,0},
      {std::numeric_limits<float>::denorm_min(),0},{0x1p60f,0},{std::numeric_limits<float>::max(),0}}};
  for (auto d : derivatives)
    for (auto view : {View{0,3},View{1,2},View{1,1}})
      for (auto mip : {MipFilter::Nearest,MipFilter::Linear})
        for (auto mag : {Filter::Nearest,Filter::Bilinear})
          for (auto min : {Filter::Nearest,Filter::Bilinear})
            for (double bias : {-16.,-0.5,0.,0.5,16.})
              for (auto range : {LodRange{0,1000},LodRange{0.75,1.75},LodRange{2,2}}) {
                Sampling sampling{AddressMode::Repeat,AddressMode::ClampToEdge,mag,min,mip};
                sampling.lod_bias=bias;
                const auto &base=image.levels[view.base_mip];
                // Independent Euclidean magnitude via hypot, using binary32
                // coordinate scaling and long-double LOD/weighted color oracle.
                const float xu=d[0]*base.width,yu=d[1]*base.height;
                const float xv=-d[1]*base.width,yv=d[0]*base.height;
                const long double rho=std::max(std::hypot(static_cast<long double>(xu),static_cast<long double>(yu)),
                    std::hypot(static_cast<long double>(xv),static_cast<long double>(yv)));
                const float lod=rho==0 ? -std::numeric_limits<float>::max() :
                    static_cast<float>(std::log2(rho));
                const float biased=lod+static_cast<float>(bias);
                const double clamped=std::clamp(static_cast<double>(biased),range.min,range.max);
                auto actual=sample_grad(image,view,sampling,0.3,0.7,{d[0],d[1]},{-d[1],d[0]},range);
                expect_color(actual.rgba,lod_oracle(image,view,sampling,0.3,0.7,clamped,range),"independent gradient magnitude/level oracle",4e-6);
              }
  for (double bad : {std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity(),1e100})
    for (unsigned axis=0;axis<4;++axis) {
      std::array<double,2> x{},y{};
      (axis<2 ? x[axis] : y[axis-2])=bad;
      rejected([&]{sample_grad(image,{0,3},{},0,0,x,y);},"invalid explicit gradient component");
    }
}

void texel_offsets() {
  auto colors = four_colors();
  Sampling edge{AddressMode::ClampToEdge, AddressMode::ClampToEdge, Filter::Nearest};
  expect_color(sample_level(colors,0,edge,-0.25,0.25,{1,0}).rgba,red,"outside UV plus offset reaches first texel");
  expect_color(sample_level(colors,0,edge,1.25,0.25,{-1,0}).rgba,green,"outside UV plus negative offset reaches last texel");
  Sampling border=edge; border.address_u=AddressMode::ClampToBorder;
  expect_color(sample_level(colors,0,border,-100,0.25,{1,0}).rgba,Color{},"far negative stays border after positive offset");
  Sampling repeat;
  expect_color(sample_level(colors,0,repeat,-std::numeric_limits<float>::denorm_min(),0.25,{1,0}).rgba,red,"integer offset preserves tiny negative nearest boundary");
  for (auto format : {Format::RGBA8_UNORM,Format::BGRA8_UNORM,Format::RGBA8_SRGB,Format::BGRA8_SRGB}) {
    auto image=mip_image(); image.format=format;
    for (unsigned m=0;m<image.levels.size();++m) {
      const auto &level=image.levels[m];
      for (unsigned y=0;y<level.height;++y) for (unsigned x=0;x<level.width;++x)
        put(image,level,x,y,{static_cast<uint8_t>(x*37+y*71+m*53),static_cast<uint8_t>(x*89+y*11+m*97),
                           static_cast<uint8_t>(x*13+y*53+m*127),static_cast<uint8_t>(19+x*31+y*29+m*17)});
    }
    for (auto u : {AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge,AddressMode::ClampToBorder})
      for (auto v : {AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge,AddressMode::ClampToBorder})
        for (auto mag : {Filter::Nearest,Filter::Bilinear}) for (auto min : {Filter::Nearest,Filter::Bilinear})
          for (auto mip : {MipFilter::Nearest,MipFilter::Linear})
            for (auto view : {View{0,3},View{1,2}})
              for (auto offset : {TexelOffset{-8,7},TexelOffset{7,-8},TexelOffset{1,-1},TexelOffset{0,0},TexelOffset{2,2},TexelOffset{-1,0}})
                for (double bias : {-0.5,0.5}) for (double lod : {-1.,0.5,0.75,1.5,4.})
                  for (auto uv : {std::array<double,2>{-2.5,1.25},std::array<double,2>{0.31,0.73},std::array<double,2>{1.5,-0.25}}) {
                    Sampling state{u,v,mag,min,mip}; state.lod_bias=bias;
                    state.border_color=BorderColor::OpaqueWhite;
                    const LodRange range{0,2};
                    const auto actual=sample_lod(image,view,state,uv[0],uv[1],lod,range,offset);
                    expect_color(actual.rgba,lod_oracle(image,view,state,uv[0],uv[1],static_cast<float>(lod)+static_cast<float>(bias),range,offset),"independent per-mip texel offset oracle");
                    expect(actual.texel_offset==offset,"offset trace operand");
                  }
    for (auto offset : {TexelOffset{-8,7},TexelOffset{7,-8},TexelOffset{0,0}})
      for (int lod=0;lod<3;++lod) {
        const auto size=texture_size(image,{0,3},lod);
        for (int x : {0,static_cast<int>(size[0])-1}) for (int y : {0,static_cast<int>(size[1])-1})
          expect_color(texel_fetch(image,{0,3},x-offset[0],y-offset[1],lod,offset).rgba,
                       texel_fetch(image,{0,3},x,y,lod).rgba,"fetch offset final legal coordinate",0);
      }
    Sampling state{AddressMode::MirroredRepeat,AddressMode::ClampToBorder,Filter::Bilinear,MipFilter::Linear};
    const auto g=sample_grad(image,{0,3},state,0.31,0.73,{0.3,0.2},{-0.1,0.4});
    const auto shifted=sample_grad(image,{0,3},state,0.31,0.73,{0.3,0.2},{-0.1,0.4},{0,1000},{7,-8});
    expect(g.footprint_squared==shifted.footprint_squared && g.mip==shifted.mip && g.mip_weight==shifted.mip_weight,"offset does not alter gradient LOD");
    expect_color(shifted.rgba,lod_oracle(image,{0,3},state,0.31,0.73,static_cast<float>(shifted.input_lod),{0,1000},{7,-8}),"gradient offset independent texel oracle");
  }
  for (auto bad : {TexelOffset{-9,0},TexelOffset{0,8},TexelOffset{INT32_MAX,0},TexelOffset{0,INT32_MIN}}) {
    rejected([&]{sample_level(colors,0,{},0,0,bad);},"invalid level offset");
    rejected([&]{sample_lod(colors,{0,1},{},0,0,0,{},bad);},"invalid LOD offset");
    rejected([&]{sample_grad(colors,{0,1},{},0,0,{0,0},{0,0},{},bad);},"invalid gradient offset");
    rejected([&]{texel_fetch(colors,{0,1},0,0,0,bad);},"invalid fetch offset");
  }
  rejected([&]{texel_fetch(colors,{0,1},0,0,0,{-1,0});},"fetch final coordinate invalid");
  rejected([&]{texel_fetch(colors,{0,1},INT32_MAX,0,0,{7,0});},"fetch offset overflow cannot become legal");
}

void mip_linear() {
  auto solid = mip_image();
  Sampling state{AddressMode::Repeat, AddressMode::ClampToEdge,
                 Filter::Nearest, MipFilter::Linear};
  expect_color(sample_lod(solid, {0, 3}, state, 0.3, 0.7, 0.25).rgba,
               {0.75, 0.25, 0, 1}, "quarter LOD blend");
  expect_color(sample_lod(solid, {1, 2}, state, 0.3, 0.7, 0.5).rgba,
               {0, 0.5, 0.5, 1}, "relative view half blend");
  expect_color(sample_lod(solid, {0, 3}, state, 0.3, 0.7, 0, {0.75, 1.75}).rgba,
               {0.25, 0.75, 0, 1}, "sampler clamp before mip weights");
  expect_color(sample_lod(solid, {0, 3}, state, 0.3, 0.7, 100, {0, 0.5}).rgba,
               {0.5, 0.5, 0, 1}, "upper clamp before mip weights");
  put(solid, solid.levels[1], 0, 0, {255, 0, 0, 255});
  put(solid, solid.levels[1], 1, 0, {0, 0, 255, 255});
  state.mag_filter = state.min_filter = Filter::Bilinear;
  expect_color(sample_lod(solid, {1, 2}, state, 0.5, 0.5, 0.5).rgba,
               {0.25, 0, 0.75, 1}, "trilinear combines level-specific bilinear");
  for (const auto &level : solid.levels)
    for (unsigned y = 0; y < level.height; ++y)
      for (unsigned x = 0; x < level.width; ++x)
        put(solid, level, x, y, {31, 127, 193, 211});
  for (auto filter : {Filter::Nearest, Filter::Bilinear}) {
    state.mag_filter = state.min_filter = filter;
    const auto expected = sample_level(solid, 0, state, 0.3, 0.7).rgba;
    for (double lod : {0.25, 0.5, 0.75, 1.25, 1.75})
      expect_color(sample_lod(solid, {0, 3}, state, 0.3, 0.7, lod).rgba,
                   expected, "identical mip colors retain exact single-level result", 0);
  }
  std::mt19937 generator(0x4d4950);
  std::uniform_real_distribution<double> coordinate(-2.5, 2.5);
  const std::array<double, 13> lods{-100, -0.0, std::nextafter(1.0, 0.0), 1,
      std::nextafter(1.0, 2.0), 0.25, 0.5, 0.75, 1.25, 1.5, 1.75, 2, 1e300};
  for (Format format : {Format::RGBA8_UNORM, Format::BGRA8_UNORM}) {
    Image image{format, {}, {}};
    for (unsigned m = 0, w = 7, h = 5; m < 3; ++m, w = std::max(1U, w/2), h = std::max(1U, h/2)) {
      auto part = make_image(w, h, format, 8, 16);
      // Distinct level content including alpha, with independent padding.
      for (unsigned y = 0; y < h; ++y)
        for (unsigned x = 0; x < w; ++x)
          put(part, part.levels[0], x, y, {static_cast<uint8_t>(m*59+x*17),
              static_cast<uint8_t>(m*43+y*29), static_cast<uint8_t>(m*71+x+y),
              static_cast<uint8_t>(m*97+x*13+y*7)});
      auto level = part.levels[0];
      level.offset += image.data.size();
      image.data.insert(image.data.end(), part.data.begin(), part.data.end());
      image.levels.push_back(level);
    }
    for (auto u_mode : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
      for (auto v_mode : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
        for (auto filter : {Filter::Nearest, Filter::Bilinear})
          for (View view : {View{0,3}, View{1,2}, View{1,1}})
            for (LodRange range : {LodRange{0,1000}, LodRange{0.75,1.75}, LodRange{10,20}})
              for (double lod : lods) {
                const Sampling sampling{u_mode,v_mode,filter,MipFilter::Linear};
                const auto u = coordinate(generator), v = coordinate(generator);
                const auto result = sample_lod(image,view,sampling,u,v,lod,range);
                expect_color(result.rgba,lod_oracle(image,view,sampling,u,v,lod,range),
                             "independent mip/spatial oracle");
                double sum = 0;
                for (const auto &tap : result.taps) {
                  sum += tap.weight;
                  expect(tap.mip >= view.base_mip && tap.mip < view.base_mip + view.level_count,
                         "tap remains inside view");
                }
                expect(std::abs(sum - 1) < 1e-14, "combined mip/tap weights sum to one");
                expect(result.mip_upper >= result.mip && result.mip_upper - result.mip <= 1,
                       "adjacent trace levels");
                if (result.mip_weight == 0)
                  expect_color(result.rgba,sample_level(image,result.mip,sampling,u,v).rgba,
                               "integer/clamped LOD retains exact single-level result",0);
              }
  }
}

void min_mag() {
  auto image = mip_image();
  put(image, image.levels[1], 0, 0, {255, 0, 0, 255});
  put(image, image.levels[1], 1, 0, {0, 0, 255, 255});
  Sampling state{AddressMode::Repeat, AddressMode::ClampToEdge,
                 Filter::Nearest, Filter::Bilinear};
  const Color purple{0.5f, 0, 0.5f, 1};
  expect_color(sample_lod(image, {1, 1}, state, 0.5, 0.5, 0).rgba, blue,
               "zero lambda selects mag");
  expect_color(sample_lod(image, {1, 1}, state, 0.5, 0.5, 0.25).rgba, purple,
               "single-level view remains minified at positive lambda");
  expect_color(sample_lod(image, {1, 2}, state, 0.5, 0.5, 0.25).rgba, purple,
               "nearest rounding to mip zero does not select mag");
  expect_color(sample_lod(image, {1, 2}, state, 0.5, 0.5, -100, {0.25, 1}).rgba, purple,
               "positive sampler minimum selects min before view clamp");
  expect_color(sample_lod(image, {1, 2}, state, 0.5, 0.5, 100, {0, 0}).rgba, blue,
               "zero sampler maximum selects mag");
  std::swap(state.mag_filter, state.min_filter);
  expect_color(sample_lod(image, {1, 1}, state, 0.5, 0.5, 0).rgba, purple,
               "reverse filters at zero lambda");
  expect_color(sample_lod(image, {1, 1}, state, 0.5, 0.5, 0.25).rgba, blue,
               "reverse filters at positive lambda");
  const std::array<double, 11> lods{-100, std::nextafter(0.0,-1.0), -0.0, 0,
      std::nextafter(0.0,1.0), 0.25, 0.5, 0.75, 1, 2, 1e300};
  for (auto format : {Format::RGBA8_UNORM, Format::BGRA8_UNORM}) {
    auto fixture = mip_image();
    // Interpret the same bytes in both channel orders; the oracle decodes separately.
    fixture.format = format;
    for (unsigned m = 0; m < fixture.levels.size(); ++m) {
      const auto &level = fixture.levels[m];
      for (unsigned y = 0; y < level.height; ++y)
        for (unsigned x = 0; x < level.width; ++x)
          put(fixture,level,x,y,{static_cast<uint8_t>(x*37+m*59),
              static_cast<uint8_t>(y*29+m*43),static_cast<uint8_t>(x+y*71+m*19),
              static_cast<uint8_t>(x*13+y*7+m*97)});
    }
    for (auto u : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
      for (auto v : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
        for (auto mag : {Filter::Nearest, Filter::Bilinear})
          for (auto min : {Filter::Nearest, Filter::Bilinear})
            for (auto mip : {MipFilter::Nearest, MipFilter::Linear})
              for (View view : {View{0,3}, View{1,2}, View{1,1}})
                for (LodRange range : {LodRange{0,1000}, LodRange{0.75,1.75}, LodRange{0,0}})
                  for (double lod : lods) {
                    Sampling sampling{u,v,mag,min,mip};
                    const auto result=sample_lod(fixture,view,sampling,-0.125,0.375,lod,range);
                    expect_color(result.rgba,lod_oracle(fixture,view,sampling,-0.125,0.375,lod,range),
                                 "independent min/mag and mip oracle");
                    expect(result.filter == (result.sampler_lod > 0 ? min : mag),
                           "trace records filter before view clamp");
                  }
  }
  rejected([&] { state.min_filter=static_cast<Filter>(42);
                 sample_lod(image,{0,3},state,0.3,0.7,0); }, "unknown inactive min filter");
}

void srgb() {
  for (auto format : {Format::RGBA8_SRGB, Format::BGRA8_SRGB}) {
    auto pixel = make_image(1,1,format,8,16);
    for (unsigned code = 0; code < 256; ++code) {
      const Bytes bytes{static_cast<uint8_t>(code),static_cast<uint8_t>(255-code),
                        static_cast<uint8_t>((code*37)%256),static_cast<uint8_t>(code)};
      put(pixel,pixel.levels[0],0,0,bytes);
      const auto result=sample_level(pixel,0,{},0.5,0.5);
      expect_color(result.rgba,oracle(pixel,0,{},0.5,0.5),"all sRGB byte codes and channel order");
      expect(result.rgba[3] == code/255.0f,"sRGB alpha retains exact UNORM decoding");
    }
    auto image=make_image(2,1,format,8,16);
    put(image,image.levels[0],0,0,{0,0,0,0});
    put(image,image.levels[0],1,0,{255,255,255,255});
    Sampling state{AddressMode::Repeat,AddressMode::ClampToEdge,Filter::Bilinear,MipFilter::Linear};
    expect_color(sample_lod(image,{0,1},state,0.5,0.5,0).rgba,{0.5,0.5,0.5,0.5},
                 "decode RGB before spatial filtering; alpha stays linear",0);
    auto upper=make_image(1,1,format,4,8);
    put(upper,upper.levels[0],0,0,{128,128,128,128});
    auto level=upper.levels[0];level.offset+=image.data.size();
    image.data.insert(image.data.end(),upper.data.begin(),upper.data.end());image.levels.push_back(level);
    const auto decoded=oracle(upper,0,{},0.5,0.5);
    Color expected{};
    for (unsigned c=0;c<4;++c) expected[c]=0.25f+decoded[c]*0.5f;
    expect_color(sample_lod(image,{0,2},state,0.5,0.5,0.5).rgba,expected,
                 "decode before spatial and cross-mip filtering");
    auto patterned=mip_image();patterned.format=format;
    for (unsigned m=0;m<patterned.levels.size();++m) {
      const auto &mip=patterned.levels[m];
      for (unsigned y=0;y<mip.height;++y)
        for (unsigned x=0;x<mip.width;++x)
          put(patterned,mip,x,y,{static_cast<uint8_t>((x*73+y*11+m*37)%256),
              static_cast<uint8_t>((x*17+y*59+m*83)%256),
              static_cast<uint8_t>((x*31+y*43+m*101)%256),
              static_cast<uint8_t>((x*97+y*7+m*29)%256)});
    }
    for (auto u : {AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge})
      for (auto v : {AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge})
        for (auto mag : {Filter::Nearest,Filter::Bilinear})
          for (auto min : {Filter::Nearest,Filter::Bilinear})
            for (auto mip : {MipFilter::Nearest,MipFilter::Linear})
              for (View view : {View{0,3},View{1,2},View{1,1}})
                for (LodRange range : {LodRange{0,1000},LodRange{0.75,1.75}})
                  for (double lod : {-10.0,-0.0,0.25,0.5,0.75,1.0,1.5,1000.0})
                    for (const auto &uv : {std::array<double,2>{-0.125,1.125},
                                          std::array<double,2>{0.375,0.5}}) {
                      Sampling sampling{u,v,mag,min,mip};
                      expect_color(sample_lod(patterned,view,sampling,uv[0],uv[1],lod,range).rgba,
                          lod_oracle(patterned,view,sampling,uv[0],uv[1],lod,range),
                          "independent sRGB/min-mag/mip/address/view oracle");
                    }
  }
}

void fetch_and_queries() {
  for (auto format : {Format::RGBA8_UNORM,Format::BGRA8_UNORM,
                      Format::RGBA8_SRGB,Format::BGRA8_SRGB})
    for (auto size : {std::array<unsigned,2>{5,3},{1,8},{8,1},{1,1},{256,1}}) {
      Image image{format,{}, {}};
      unsigned w=size[0],h=size[1];
      for (;;) {
        auto part=make_image(w,h,format,7,3);
        auto level=part.levels[0];level.offset+=image.data.size();
        image.data.insert(image.data.end(),part.data.begin(),part.data.end());
        image.levels.push_back(level);
        if (w==1 && h==1) break;
        w=std::max(1U,w/2);h=std::max(1U,h/2);
      }
      for (unsigned base=0;base<image.levels.size();++base)
        for (unsigned count=1;count<=image.levels.size()-base;++count) {
          const View view{base,count};
          expect(texture_query_levels(image,view)==count,"levels from view, independent of sampler");
          for (unsigned lod=0;lod<count;++lod) {
            const auto &level=image.levels[base+lod];
            expect(texture_size(image,view,lod)==std::array<uint32_t,2>{level.width,level.height},
                   "exact view-relative mip dimensions");
            for (unsigned y=0;y<level.height;++y)
              for (unsigned x=0;x<level.width;++x) {
                const auto result=texel_fetch(image,view,x,y,lod);
                Color expected{};
                const Bytes bytes{static_cast<uint8_t>((x*37+y*71+5)%256),
                                  static_cast<uint8_t>((x*89+y*11+23)%256),
                                  static_cast<uint8_t>((x*13+y*53+251)%256),
                                  static_cast<uint8_t>((x*31+y*29+19)%256)};
                for (unsigned c=0;c<4;++c) {
                  const long double e=bytes[c]/255.0L;
                  expected[c]=static_cast<float>(is_srgb(format) && c<3 ?
                      (e<=0.04045L ? e/12.92L : std::pow((e+0.055L)/1.055L,2.4L)) : e);
                }
                expect_color(result.rgba,expected,"independent integer fetch byte/color oracle");
                expect(result.mip==base+lod && result.taps.size()==1 && result.taps[0].weight==1,
                       "fetch one tap in exact mip");
                expect(result.taps[0].byte_offset==level.offset+y*level.row_pitch+x*4,
                       "fetch exact padded byte address");
                expect_color(result.rgba,sample_lod(image,view,{},(x+0.5)/level.width,
                             (y+0.5)/level.height,lod).rgba,"fetch vs nearest at valid center");
              }
          }
          for (int32_t lod : {-1,static_cast<int32_t>(count),std::numeric_limits<int32_t>::max()}) {
            rejected([&]{texture_size(image,view,lod);},"query illegal mip");
            rejected([&]{texel_fetch(image,view,0,0,lod);},"fetch illegal mip, no sampler clamp");
          }
          rejected([&]{texel_fetch(image,view,-1,0,0);},"fetch negative x, no wrapping");
          rejected([&]{texel_fetch(image,view,0,-1,0);},"fetch negative y");
          rejected([&]{texel_fetch(image,view,image.levels[base].width,0,0);},"fetch past width");
          rejected([&]{texel_fetch(image,view,0,image.levels[base].height,0);},"fetch past height");
        }
      rejected([&]{texture_query_levels(image,{0,0});},"query invalid view");
      rejected([&]{texture_size(image,{static_cast<uint32_t>(image.levels.size()),1},0);},"size invalid view");
    }
}

void view_swizzle() {
  for (auto format : {Format::RGBA8_UNORM,Format::BGRA8_UNORM,
                      Format::RGBA8_SRGB,Format::BGRA8_SRGB}) {
    auto image=mip_image();image.format=format;
    for (unsigned m=0;m<image.levels.size();++m) {
      const auto &level=image.levels[m];
      for (unsigned y=0;y<level.height;++y)
        for (unsigned x=0;x<level.width;++x)
          put(image,level,x,y,{static_cast<uint8_t>((10+x*41+y*17+m*67)%256),
              static_cast<uint8_t>((128+x*13+y*59+m*97)%256),
              static_cast<uint8_t>((221+x*37+y*7+m*31)%256),
              static_cast<uint8_t>((57+x*97+y*23+m*11)%256)});
    }
    for (unsigned code=0;code<2401;++code) {
      ComponentMapping mapping{};unsigned remaining=code;
      for (auto &c : mapping) {c=static_cast<Component>(remaining%7);remaining/=7;}
      View view{0,3,mapping};
      for (auto filter : {Filter::Nearest,Filter::Bilinear})
        for (auto mip : {MipFilter::Nearest,MipFilter::Linear}) {
          Sampling state{AddressMode::Repeat,AddressMode::MirroredRepeat,filter,mip};
          expect_color(sample_lod(image,view,state,-0.125,0.375,0.75).rgba,
              lod_oracle(image,view,state,-0.125,0.375,0.75,{}),"all2401 mappings independent sample oracle");
        }
      view.base_mip=1;view.level_count=2;
      expect_color(texel_fetch(image,view,0,0,0).rgba,
          mapped_oracle(oracle(image,1,{},0.25,0.5),mapping),"all2401 mappings decoded fetch oracle");
      const auto result=texel_fetch(image,view,0,0,0);
      expect_color(result.taps[0].color,result.rgba,"fetch trace mapped color",0);
      expect(texture_size(image,view,1)==std::array<uint32_t,2>{1,1} &&
             texture_query_levels(image,view)==2,"swizzle does not affect property queries");
      for (unsigned c=0;c<4;++c)
        if (mapping[c]==Component::Zero || mapping[c]==Component::One)
          expect(result.rgba[c]==(mapping[c]==Component::One ? 1 : 0),"swizzle constants exact float0/1");
    }
    View bad{0,3};bad.components[2]=static_cast<Component>(99);
    rejected([&]{sample_lod(image,bad,{},0.5,0.5,0);},"unknown view component");
    rejected([&]{texel_fetch(image,bad,0,0,0);},"fetch unknown view component");
  }
}

void clamp_to_border() {
  auto solid=make_image(1,1);
  put(solid,solid.levels[0],0,0,{255,0,0,255});
  Sampling state{AddressMode::ClampToBorder,AddressMode::ClampToBorder,Filter::Bilinear,MipFilter::Linear};
  expect_color(sample_lod(solid,{0,1},state,0,0,0).rgba,{0.25,0,0,0.25},"three border taps at corner",0);
  state.border_color=BorderColor::OpaqueBlack;
  expect_color(sample_lod(solid,{0,1},state,0,0,0).rgba,{0.25,0,0,1},"opaque black preserves alpha",0);
  state.border_color=BorderColor::OpaqueWhite;
  expect_color(sample_lod(solid,{0,1},state,0,0,0).rgba,{1,0.75,0.75,1},"opaque white corner",0);
  for (auto border : {BorderColor::TransparentBlack,BorderColor::OpaqueBlack,BorderColor::OpaqueWhite}) {
    state.border_color=border;
    const Color expected=border==BorderColor::OpaqueWhite ? Color{1,1,1,1} :
                         border==BorderColor::OpaqueBlack ? Color{0,0,0,1} : Color{0,0,0,0};
    for (double u : {-std::numeric_limits<double>::max(),-1e300,1e300,std::numeric_limits<double>::max()}) {
      const auto result=sample_lod(solid,{0,1},state,u,-u,0);
      expect_color(result.rgba,expected,"huge finite coordinates entirely border",0);
      for (const auto &tap : result.taps) expect(tap.border,"huge border tap has no physical image read");
    }
  }
  for (auto format : {Format::RGBA8_UNORM,Format::BGRA8_UNORM,Format::RGBA8_SRGB,Format::BGRA8_SRGB}) {
    auto image=mip_image();image.format=format;
    for (unsigned m=0;m<image.levels.size();++m)
      for (unsigned y=0;y<image.levels[m].height;++y)
        for (unsigned x=0;x<image.levels[m].width;++x)
          put(image,image.levels[m],x,y,{static_cast<uint8_t>(37*x+11*y+31*m),
              static_cast<uint8_t>(19*x+71*y+43*m),static_cast<uint8_t>(51*x+17*y+67*m),
              static_cast<uint8_t>(23*x+53*y+97*m)});
    for (auto u : {AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge,AddressMode::ClampToBorder})
      for (auto v : {AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge,AddressMode::ClampToBorder}) {
        if (u!=AddressMode::ClampToBorder && v!=AddressMode::ClampToBorder) continue;
        for (auto border : {BorderColor::TransparentBlack,BorderColor::OpaqueBlack,BorderColor::OpaqueWhite})
          for (auto mag : {Filter::Nearest,Filter::Bilinear})
            for (auto min : {Filter::Nearest,Filter::Bilinear})
              for (auto mip : {MipFilter::Nearest,MipFilter::Linear})
                for (View view : {View{0,3},View{1,2},View{2,1}})
                  for (double lod : {-10.,0.,0.25,0.5,0.75,1.,1.5,1000.})
                    for (auto uv : {std::array<double,2>{-0.125,0.375},{0,0},{1,1},{0.5,0.5},{1.125,-0.125},{-2,2}}) {
                      Sampling sampling{u,v,mag,min,mip};sampling.border_color=border;
                      if (border!=BorderColor::OpaqueBlack) view.components={Component::A,Component::Zero,Component::One,Component::R};
                      const auto result=sample_lod(image,view,sampling,uv[0],uv[1],lod);
                      expect_color(result.rgba,lod_oracle(image,view,sampling,uv[0],uv[1],lod,{}),
                          "independent border extended-image oracle before filter/mip/swizzle");
                      double weight=0;
                      for (const auto &tap : result.taps) {
                        weight+=tap.weight;
                        if (!tap.border) expect(tap.byte_offset+4<=image.data.size(),"physical border profile tap within backing");
                      }
                      expect(std::abs(weight-1)<1e-14,"border and physical weights sum to one");
                    }
      }
    View mapped{0,3,{Component::A,Component::R,Component::G,Component::B}};
    state.border_color=BorderColor::OpaqueBlack;
    rejected([&]{sample_lod(image,mapped,state,0,0,0);},"core opaque black + nonidentity swizzle undefined");
    expect_color(texel_fetch(image,mapped,0,0,0).rgba,mapped_oracle(oracle(image,0,{},0.1,1.0/6),mapped.components),
                 "fetch has no border sampler");
  }
  state.border_color=static_cast<BorderColor>(99);
  rejected([&]{sample_lod(solid,{0,1},state,0,0,0);},"unknown border color");
}

void invalid_inputs() {
  const auto image = four_colors();
  for (double invalid : {std::numeric_limits<double>::infinity(),
                         -std::numeric_limits<double>::infinity(),
                         std::numeric_limits<double>::quiet_NaN()}) {
    rejected([&] { sample_level(image, 0, {}, invalid, 0); }, "invalid U");
    rejected([&] { sample_level(image, 0, {}, 0, invalid); }, "invalid V");
    rejected([&] { sample_lod(image, {0, 1}, {}, 0, 0, invalid); }, "invalid LOD");
    rejected([&] { sample_lod(image, {0, 1}, {}, 0, 0, 0, {0, invalid}); },
             "invalid LOD clamp");
  }
  for (View view : {View{0, 0}, View{1, 1}, View{0, 2},
                    View{0xffffffffU, 0xffffffffU}})
    rejected([&] { sample_lod(image, view, {}, 0, 0, 0); }, "invalid view");
  for (LodRange range : {LodRange{-1, 1}, LodRange{2, 1}})
    rejected([&] { sample_lod(image, {0, 1}, {}, 0, 0, 0, range); }, "invalid clamp range");
  rejected([&] { sample_level(image, 1, {}, 0, 0); }, "invalid mip index");
  auto malformed = image;
  malformed.format = static_cast<Format>(99);
  rejected([&] { validate(malformed); }, "unknown format");
  malformed = image;
  malformed.levels.clear();
  rejected([&] { validate(malformed); }, "no mip");
  for (Level bad : {Level{0, 2, 0, 8}, Level{2, 0, 0, 8}, Level{2, 2, 0, 7},
                    Level{2, 2, 100, 8},
                    Level{2, 2, 0, std::numeric_limits<std::uint64_t>::max()}}) {
    malformed = image;
    malformed.levels[0] = bad;
    rejected([&] { validate(malformed); }, "invalid storage layout");
  }
  malformed = image;
  malformed.data.pop_back();
  rejected([&] { validate(malformed); }, "truncated backing data");
  malformed = mip_image();
  malformed.levels[1].width = 3;
  rejected([&] { validate(malformed); }, "malformed mip dimensions");
  malformed = make_image(1, 1);
  malformed.levels.push_back(malformed.levels[0]);
  rejected([&] { validate(malformed); }, "mip after 1x1");
  rejected([&] { sample_level(image, 0, {static_cast<AddressMode>(99),
                                        AddressMode::Repeat, Filter::Nearest}, 0, 0); },
           "unknown U address mode");
  rejected([&] { sample_level(image, 0, {AddressMode::Repeat,
                                        static_cast<AddressMode>(99), Filter::Nearest}, 0, 0); },
           "unknown V address mode");
  rejected([&] { sample_level(image, 0, {AddressMode::Repeat, AddressMode::Repeat,
                                        static_cast<Filter>(99)}, 0, 0); }, "unknown filter");
  rejected([&] { sample_lod(image, {0,1}, {AddressMode::Repeat, AddressMode::Repeat,
                 Filter::Nearest, static_cast<MipFilter>(99)}, 0, 0, 0); }, "unknown mip filter");
}

} // namespace

int main() {
  const std::array<std::pair<const char *, void (*)()>, 15> suites{{
      {"hand-calculated filtering and seams", hand_calculated},
      {"negative UV, boundaries and huge finite UV", negative_and_boundary_coordinates},
      {"all 256 UNORM values in both formats", all_unorm_values},
      {"3600 independent-oracle samples and properties", randomized_oracle},
      {"explicit LOD, view ranges and multiple images", lod_and_views},
      {"mip-linear/trilinear independent oracle and trace", mip_linear},
      {"sampler LOD bias before clamp/filter/view", lod_bias},
      {"explicit 2D gradients and independent Euclidean oracle", explicit_gradients},
      {"constant sample/fetch offsets and independent texel oracle", texel_offsets},
      {"independent min/mag selection before view clamp", min_mag},
      {"sRGB decoding before filtering and unchanged alpha", srgb},
      {"integer texel fetch and view-relative property queries", fetch_and_queries},
      {"view component mappings after decode for sample/fetch", view_swizzle},
      {"per-tap core float border replacement before filtering", clamp_to_border},
      {"invalid inputs and storage arithmetic", invalid_inputs}}};
  try {
    for (const auto &suite : suites) {
      suite.second();
      std::cout << "PASS " << suite.first << '\n';
    }
    std::cout << "PASS texture model: " << suites.size() << " suites, " << checks
              << " checks\n";
  } catch (const std::exception &error) {
    std::cerr << "FAIL " << error.what() << '\n';
    return 1;
  }
}
