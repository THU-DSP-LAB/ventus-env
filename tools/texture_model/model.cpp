#include "model.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>

namespace ventus::texture {
static_assert(sizeof(float)==4 && std::numeric_limits<float>::is_iec559,"D32 requires IEEE binary32");
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

void compare_tap(Tap &tap, CompareOp op, float reference) {
  const float depth=tap.color[0];bool passes=false;
  switch(op) {
  case CompareOp::Never:break;
  case CompareOp::Less:passes=reference<depth;break;
  case CompareOp::Equal:passes=reference==depth;break;
  case CompareOp::LessOrEqual:passes=reference<=depth;break;
  case CompareOp::Greater:passes=reference>depth;break;
  case CompareOp::NotEqual:passes=reference!=depth;break;
  case CompareOp::GreaterOrEqual:passes=reference>=depth;break;
  case CompareOp::Always:passes=true;break;
  default:throw std::invalid_argument("unsupported depth compare operation");
  }
  tap.depth_value=depth;tap.compared=true;tap.comparison=passes ? 1 : 0;
  tap.color.fill(tap.comparison);
}

Color border_color(BorderColor color) {
  if (color == BorderColor::OpaqueWhite) return {1,1,1,1};
  if (color == BorderColor::OpaqueBlack) return {0,0,0,1};
  return {0,0,0,0};
}

void check_offset(TexelOffset offset) {
  for (auto value : offset)
    require(value >= min_texel_offset && value <= max_texel_offset, "texel offset outside [-8,7]");
}

// Reduce before multiplying by image dimensions or converting to an integer.
// This avoids undefined float-to-integer conversions for huge finite UVs.
double reduce(double value, AddressMode mode, bool offset = false) {
  // With an offset, coordinates outside [0,1] may reach an interior texel.
  // These wider safe bounds preserve every [-8,7] offset even for 1x1.
  if (offset && (mode == AddressMode::ClampToEdge || mode == AddressMode::ClampToBorder))
    return std::clamp(value, -9.0, 10.0);
  if (mode == AddressMode::ClampToEdge)
    return std::clamp(value, 0.0, 1.0);
  if (mode == AddressMode::ClampToBorder)
    return std::clamp(value, -1.0, 2.0);
  // Preserve small signed coordinates before adding a period. A tiny
  // negative value plus 1 rounds onto the next nearest-texel boundary.
  if (value > -2.0 && value < 2.0)
    return value;
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
  if (is_depth(image.format)) {
    const std::uint32_t bits=static_cast<std::uint32_t>(p[0])|(static_cast<std::uint32_t>(p[1])<<8)|
      (static_cast<std::uint32_t>(p[2])<<16)|(static_cast<std::uint32_t>(p[3])<<24);
    float depth;std::memcpy(&depth,&bits,sizeof(depth));
    require(std::isfinite(depth) && depth>=0 && depth<=1,"depth outside finite [0,1] profile");
    tap.color={depth,0,0,1};tap.depth_value=depth;return tap;
  }
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
  require(view.layer_count > 0 && view.base_layer < image.layer_count &&
              view.layer_count <= image.layer_count - view.base_layer,
          "view layer range outside image");
  if (is_depth(image.format))
    for (unsigned c=0;c<4;++c) require(view.components[c]==Component::Identity || static_cast<unsigned>(view.components[c])==static_cast<unsigned>(Component::R)+c,"depth view requires identity swizzle");
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

// Orthonormal face frames from Vulkan's face-coordinate table.
using CubeAxis = std::array<int,3>;
constexpr std::array<CubeAxis,6> cube_n{{{1,0,0},{-1,0,0},{0,1,0},{0,-1,0},{0,0,1},{0,0,-1}}};
constexpr std::array<CubeAxis,6> cube_u{{{0,0,-1},{0,0,1},{1,0,0},{1,0,0},{1,0,0},{-1,0,0}}};
constexpr std::array<CubeAxis,6> cube_v{{{0,-1,0},{0,-1,0},{0,0,1},{0,0,-1},{0,-1,0},{0,-1,0}}};
int dot(CubeAxis a, CubeAxis b) { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }
double dot(std::array<double,3> a, CubeAxis b) { return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]; }

void validate_cube(const Image &image, View view) {
  validate(image);
  validate_view(image,view);
  require(!image.volume && view.layer_count==6,"Cube view needs exactly six faces");
  for (const auto &level : image.levels)
    require(level.width==level.height,"Cube mip must be square");
}

struct CubeEdge { unsigned face,x,y; };
CubeEdge cube_edge(unsigned face, bool horizontal, bool high, unsigned varying, unsigned n) {
  CubeAxis normal=horizontal ? cube_u[face] : cube_v[face];
  if (!high) for (auto &component : normal) component=-component;
  unsigned neighbor=0;
  while (neighbor<6 && cube_n[neighbor]!=normal) ++neighbor;
  require(neighbor<6,"invalid Cube face basis");
  const auto varying_axis=horizontal ? cube_v[face] : cube_u[face];
  auto coordinate=[&](CubeAxis axis) {
    const int fixed=dot(cube_n[face],axis), orientation=dot(varying_axis,axis);
    if (fixed) return fixed>0 ? n-1 : 0U;
    require(orientation!=0,"invalid Cube edge orientation");
    return orientation>0 ? varying : n-1-varying;
  };
  return {neighbor,coordinate(cube_u[neighbor]),coordinate(cube_v[neighbor])};
}

std::vector<Tap> cube_read(const Image &image, unsigned mip, unsigned base,
                           unsigned face, std::int64_t x, std::int64_t y, double weight) {
  const auto &level=image.levels[mip];
  const auto n=level.width;
  const bool outside_x=x<0 || x>=n, outside_y=y<0 || y>=n;
  const auto cx=static_cast<unsigned>(std::clamp(x,std::int64_t{0},static_cast<std::int64_t>(n-1)));
  const auto cy=static_cast<unsigned>(std::clamp(y,std::int64_t{0},static_cast<std::int64_t>(n-1)));
  std::vector<Tap> taps;
  auto add=[&](unsigned target,unsigned tx,unsigned ty,double w) {
    auto plane=level;plane.offset+=(base+target)*plane.layer_pitch;
    auto tap=read_at(image,plane,tx,ty,w);
    tap.raw_x=x;tap.raw_y=y;tap.mip=mip;tap.layer=base+target;
    taps.push_back(tap);
  };
  if (outside_x && outside_y) {
    const auto across_x=cube_edge(face,true,x>=n,cy,n);
    const auto across_y=cube_edge(face,false,y>=n,cx,n);
    add(face,cx,cy,weight/3);
    add(across_x.face,across_x.x,across_x.y,weight/3);
    add(across_y.face,across_y.x,across_y.y,weight/3);
  } else if (outside_x || outside_y) {
    const auto across=cube_edge(face,outside_x,outside_x ? x>=n : y>=n,outside_x ? cy : cx,n);
    add(across.face,across.x,across.y,weight);
  } else add(face,cx,cy,weight);
  return taps;
}

Result sample_validated(const Image &image, std::uint32_t mip,
                        const Sampling &sampling, double u, double v, Filter filter, TexelOffset offset, std::uint32_t layer, bool cube = false, std::uint32_t cube_base = 0, double w = 0, const float *reference = nullptr) {
  require(std::isfinite(u) && std::isfinite(v), "UV must be finite");
  check_mode(sampling.address_u);
  check_mode(sampling.address_v);
  require(mip < image.levels.size(), "mip index out of range");
  require(layer < image.layer_count, "layer index outside image");
  auto level = image.levels[mip];
  level.offset += layer * level.layer_pitch;
  check_offset(offset);
  const double x = (cube ? u : reduce(u, sampling.address_u, offset[0] != 0)) * level.width;
  const double y = (cube ? v : reduce(v, sampling.address_v, offset[1] != 0)) * level.height;
  Sampling lookup=sampling;
  if (cube) lookup.address_u=lookup.address_v=AddressMode::ClampToEdge;
  Result result;
  result.mip = mip;
  result.mip_upper = mip;
  result.layer = layer;
  result.input_u = u;
  result.input_v = v;
  result.filter = filter;
  result.texel_offset = offset;
  if (image.volume) {
    require(std::isfinite(w), "W must be finite");
    check_mode(sampling.address_w);
    const double z = reduce(w,sampling.address_w)*level.depth;
    result.from_volume=true;result.input_w=w;
    const bool linear=filter==Filter::Bilinear;
    std::array<double,3> position{x-(linear ? .5 : 0),y-(linear ? .5 : 0),z-(linear ? .5 : 0)};
    std::array<std::int64_t,3> low{};std::array<double,3> fraction{};
    for (unsigned c=0;c<3;++c) {low[c]=static_cast<std::int64_t>(std::floor(position[c]));fraction[c]=position[c]-std::floor(position[c]);}
    const unsigned count=linear ? 2 : 1;
    for (unsigned dz=0;dz<count;++dz) for (unsigned dy=0;dy<count;++dy) for (unsigned dx=0;dx<count;++dx) {
      const auto rz=low[2]+dz;const auto zi=address(rz,level.depth,sampling.address_w);
      const double weight=linear ? (dx ? fraction[0] : 1-fraction[0])*(dy ? fraction[1] : 1-fraction[1])*(dz ? fraction[2] : 1-fraction[2]) : 1;
      auto slice=level;slice.offset+=zi*level.slice_pitch;
      auto tap=read(image,slice,sampling,low[0]+dx,low[1]+dy,weight);
      if (sampling.address_w==AddressMode::ClampToBorder && (rz<0 || rz>=level.depth)) {
        tap.border=true;tap.byte_offset=0;tap.color=border_color(sampling.border_color);
      }
      tap.raw_z=rz;tap.z=zi;tap.mip=mip;result.taps.push_back(tap);
    }
    for (unsigned c=0;c<4;++c) {double value=0;for (const auto &tap:result.taps) value+=tap.weight*tap.color[c];result.rgba[c]=static_cast<float>(value);}
    return result;
  }
  if (filter == Filter::Nearest) {
    result.taps.push_back(read(image, level, lookup,
                              static_cast<std::int64_t>(std::floor(x)) + offset[0],
                              static_cast<std::int64_t>(std::floor(y)) + offset[1], 1));
    if(reference) compare_tap(result.taps[0],sampling.compare_op,*reference);
    result.rgba = result.taps[0].color;
    result.taps[0].mip = mip;
    result.taps[0].layer = layer;
    return result;
  }
  const double sx = x - 0.5;
  const double sy = y - 0.5;
  const double fx = std::floor(sx);
  const double fy = std::floor(sy);
  const auto ix = static_cast<std::int64_t>(fx) + offset[0];
  const auto iy = static_cast<std::int64_t>(fy) + offset[1];
  const double a = sx - fx;
  const double b = sy - fy;
  auto add=[&](std::int64_t tx,std::int64_t ty,double weight) {
    if (cube) {
      auto taps=cube_read(image,mip,cube_base,layer-cube_base,tx,ty,weight);
      result.taps.insert(result.taps.end(),taps.begin(),taps.end());
    } else {
      auto tap=read(image,level,sampling,tx,ty,weight);
      tap.mip=mip;tap.layer=layer;if(reference)compare_tap(tap,sampling.compare_op,*reference);result.taps.push_back(tap);
    }
  };
  add(ix,iy,(1-a)*(1-b));add(ix+1,iy,a*(1-b));
  add(ix,iy+1,(1-a)*b);add(ix+1,iy+1,a*b);
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
              image.format == Format::RGBA8_SRGB || image.format == Format::BGRA8_SRGB || is_depth(image.format),
          "unsupported format");
  require(image.layer_count > 0, "image needs at least one layer");
  require(!image.volume || image.layer_count==1,"3D images have one array layer");
  require(!is_depth(image.format) || (!image.volume && image.layer_count==1),"depth profile is single-layer 2D");
  require(!image.levels.empty(), "image needs at least one mip");
  for (std::size_t i = 0; i < image.levels.size(); ++i) {
    const auto &level = image.levels[i];
    require(level.width > 0 && level.height > 0 && level.depth > 0, "mip dimensions must be positive");
    require(image.volume || level.depth==1,"2D level depth must be one");
    if (i > 0) {
      const auto &previous = image.levels[i - 1];
      require(previous.width != 1 || previous.height != 1 || previous.depth != 1,
              "mip chain continues past 1x1");
      require(level.width == std::max(1U, previous.width / 2) &&
                  level.height == std::max(1U, previous.height / 2) &&
                  level.depth == std::max(1U,previous.depth / 2),
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
    if (level.depth > 1) {
      const auto span=(level.height-1ULL)*level.row_pitch+row_bytes;
      require(level.slice_pitch>=span,"slice pitch overlaps texel bytes");
      require(level.depth-1ULL <= (remaining-span)/level.slice_pitch,"backing bytes do not contain all slices");
    }
    if (image.layer_count > 1) {
      const auto span = (level.height - 1ULL) * level.row_pitch + row_bytes;
      require(level.layer_pitch >= span, "layer pitch overlaps texel bytes");
      require(image.layer_count - 1ULL <= (remaining - span) / level.layer_pitch,
              "backing bytes do not contain all layers");
    }
  }
}

Result sample_level(const Image &image, std::uint32_t mip,
                    const Sampling &sampling, double u, double v, TexelOffset offset, std::uint32_t layer) {
  validate(image);
  check_filters(sampling);
  require(!image.volume && !sampling.compare_enable,"single-level helper is noncomparison 2D");
  return sample_validated(image, mip, sampling, u, v, sampling.mag_filter, offset, layer);
}

static Result sample_lod_impl(const Image &image, View view, const Sampling &sampling,
                  double u, double v, double lod, LodRange range, TexelOffset offset, double layer, bool cube, double w = 0, const float *reference = nullptr) {
  validate(image);
  check_filters(sampling);
  validate_view(image, view);
  require(sampling.compare_enable == (reference != nullptr),"comparison state requires comparison helper");
  require(std::isfinite(layer), "sample layer must be finite");
  const double relative_layer = std::clamp(layer, 0.0, static_cast<double>(view.layer_count - 1));
  const auto low_layer = static_cast<std::uint32_t>(std::floor(relative_layer));
  const double layer_fraction = relative_layer - low_layer;
  const auto selected_layer = view.base_layer + low_layer +
      (layer_fraction > 0.5 || (layer_fraction == 0.5 && (low_layer & 1)));
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
  Result result = sample_validated(image, view.base_mip + selected, sampling, u, v, filter, offset, selected_layer, cube, view.base_layer, w, reference);
  if (linear) {
    result.mip_upper = view.base_mip + std::min(selected + 1, view.level_count - 1);
    result.mip_weight = relative - low;
    // Exact integer/end LODs retain the first sample without needless reads.
    if (result.mip_weight > 0 && result.mip_upper != result.mip) {
      auto upper = sample_validated(image, result.mip_upper, sampling, u, v, filter, offset, selected_layer, cube, view.base_layer, w, reference);
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
  result.input_layer = layer;
  result.sampler_lod = clamped;
  result.clamped_lod = relative;
  swizzle_result(result, view.components);
  return result;
}

Result sample_lod(const Image &image, View view, const Sampling &sampling,
                  double u,double v,double lod,LodRange range,TexelOffset offset,double layer) {
  require(!image.volume && !sampling.compare_enable,"use dimension/comparison sampling helper");
  return sample_lod_impl(image,view,sampling,u,v,lod,range,offset,layer,false);
}

Result sample_compare_lod(const Image &image,View view,const Sampling &sampling,
                          double u,double v,double reference,double lod,LodRange range) {
  require(is_depth(image.format) && sampling.compare_enable,"comparison requires D32 and enabled sampler");
  require(std::isfinite(reference) && std::abs(reference)<=std::numeric_limits<float>::max(),"Dref must be finite binary32");
  require(sampling.compare_op>=CompareOp::Never && sampling.compare_op<=CompareOp::Always,"unsupported compare operation");
  const float dref=static_cast<float>(reference);
  auto result=sample_lod_impl(image,view,sampling,u,v,lod,range,{},0,false,0,&dref);
  result.from_compare=true;result.input_dref=dref;return result;
}

Result sample_volume_lod(const Image &image,View view,const Sampling &sampling,
                         double u,double v,double w,double lod,LodRange range) {
  require(image.volume && !sampling.compare_enable,"3D sampling requires noncomparison volume image");
  return sample_lod_impl(image,view,sampling,u,v,lod,range,{},0,false,w);
}
std::array<std::uint32_t,3> texture_volume_size(const Image &image,View view,std::int32_t lod) {
  require(image.volume,"3D size requires volume image");
  const auto size=texture_size(image,view,lod);
  return {size[0],size[1],image.levels[view.base_mip+lod].depth};
}
Result texel_fetch_volume(const Image &image,View view,std::int32_t x,std::int32_t y,std::int32_t z,std::int32_t lod) {
  const auto size=texture_volume_size(image,view,lod);
  require(x>=0 && y>=0 && z>=0 && static_cast<std::uint32_t>(x)<size[0] &&
    static_cast<std::uint32_t>(y)<size[1] && static_cast<std::uint32_t>(z)<size[2],"3D fetch coordinate outside mip");
  const auto mip=view.base_mip+lod;auto level=image.levels[mip];level.offset+=static_cast<std::uint32_t>(z)*level.slice_pitch;
  Result result;result.from_volume=true;result.input_w=z;result.mip=result.mip_upper=mip;
  auto tap=read_at(image,level,x,y,1);tap.z=z;tap.raw_z=z;tap.mip=mip;result.taps.push_back(tap);result.rgba=tap.color;
  swizzle_result(result,view.components);return result;
}

Result sample_cube_lod(const Image &image,View view,const Sampling &sampling,
                       std::array<double,3> direction,double lod,LodRange range) {
  validate_cube(image,view);
  require(!sampling.compare_enable,"Cube comparison is outside this profile");
  for (double component : direction)
    require(std::isfinite(component) && std::abs(component)<=std::numeric_limits<float>::max(),
            "Cube direction must be finite binary32");
  unsigned axis=0;
  if (std::abs(direction[1])>=std::abs(direction[0])) axis=1;
  if (std::abs(direction[2])>=std::abs(direction[axis])) axis=2;
  const auto major=std::abs(direction[axis]);
  require(major>0,"Cube direction must be nonzero");
  const unsigned face=2*axis+(direction[axis]<0);
  const double u=(dot(direction,cube_u[face])/major+1)*0.5;
  const double v=(dot(direction,cube_v[face])/major+1)*0.5;
  auto result=sample_lod_impl(image,view,sampling,u,v,lod,range,{},face,true);
  result.from_cube=true;result.input_direction=direction;result.cube_face=face;
  return result;
}

std::array<std::uint32_t,2> texture_cube_size(const Image &image,View view,std::int32_t lod) {
  validate_cube(image,view);
  return texture_size(image,view,lod);
}

Result sample_grad(const Image &image, View view, const Sampling &sampling,
                   double u, double v, std::array<double,2> dx,
                   std::array<double,2> dy, LodRange range, TexelOffset offset, double layer) {
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
  Result result=sample_lod(image,view,sampling,u,v,lod,range,offset,layer);
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

std::array<std::uint32_t,3> texture_array_size(const Image &image, View view, std::int32_t lod) {
  require(!image.volume,"array query requires 2D image");
  const auto size = texture_size(image,view,lod);
  return {size[0],size[1],view.layer_count};
}

Result texel_fetch(const Image &image, View view, std::int32_t x,
                   std::int32_t y, std::int32_t lod, TexelOffset offset, std::int32_t layer) {
  require(!image.volume,"use texel_fetch_volume for 3D images");
  check_offset(offset);
  const auto size = texture_size(image, view, lod);
  const std::int64_t final_x = static_cast<std::int64_t>(x) + offset[0];
  const std::int64_t final_y = static_cast<std::int64_t>(y) + offset[1];
  require(final_x >= 0 && final_y >= 0 && final_x < size[0] && final_y < size[1], "integer coordinate outside mip");
  require(layer >= 0 && static_cast<std::uint32_t>(layer) < view.layer_count, "integer layer outside view");
  Result result;
  result.mip = result.mip_upper = view.base_mip + lod;
  result.layer = view.base_layer + layer;
  result.input_layer = layer;
  auto level = image.levels[result.mip];
  level.offset += result.layer * level.layer_pitch;
  auto tap = read_at(image, level, final_x, final_y, 1);
  tap.mip = result.mip;
  tap.layer = result.layer;
  result.rgba = tap.color;
  result.taps.push_back(tap);
  result.input_lod = lod;
  result.texel_offset = offset;
  swizzle_result(result, view.components);
  return result;
}

} // namespace ventus::texture
