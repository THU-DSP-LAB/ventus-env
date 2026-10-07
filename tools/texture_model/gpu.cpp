// Numeric Vulkan -> SPIR-V -> Ventus ELF -> Spike regression. The CPU model
// supplies expectations only; actual floats come from SSBO and buffer-reference stores.
#include "model.hpp"

#include <vulkan/vulkan.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

using namespace ventus::texture;
namespace {

double tolerance = 1e-6; // Explicit-Lod bound; gradient-only bound is set before running.
static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559,
              "numeric readback requires IEEE 754 binary32");

const std::array<ComponentMapping,9> mappings{{{},
    {Component::R,Component::G,Component::B,Component::A},
    {Component::B,Component::G,Component::R,Component::A},
    {Component::A,Component::A,Component::A,Component::A},
    {Component::R,Component::R,Component::R,Component::R},
    {Component::A,Component::B,Component::G,Component::R},
    {Component::Zero,Component::One,Component::R,Component::A},
    {Component::One,Component::Zero,Component::B,Component::G},
    {Component::G,Component::R,Component::Zero,Component::One}}};

VkComponentMapping vk_mapping(ComponentMapping mapping) {
  return {static_cast<VkComponentSwizzle>(mapping[0]),static_cast<VkComponentSwizzle>(mapping[1]),
          static_cast<VkComponentSwizzle>(mapping[2]),static_cast<VkComponentSwizzle>(mapping[3])};
}

unsigned packed_mapping(ComponentMapping mapping) {
  unsigned bits=0;
  for (unsigned c=0;c<4;++c) bits|=static_cast<unsigned>(mapping[c])<<(4*c);
  return bits;
}

void check(VkResult result, const char *operation) {
  if (result != VK_SUCCESS)
    throw std::runtime_error(std::string(operation) + ": VkResult=" + std::to_string(result));
}

std::uint64_t align_up(std::uint64_t value, std::uint64_t alignment) {
  return (value + alignment - 1) / alignment * alignment;
}

struct Buffer {
  VkBuffer handle;
  VkDeviceMemory memory;
  void *map;
};

struct Texture {
  VkImage handle;
  VkImageView view;
  VkDeviceMemory memory;
  unsigned layers = 1;
  bool cube_view = false;
  bool volume_view = false;
  bool array_view = false;
  bool linear = false;
  bool depth_view = false;
};

class Experiment {
public:
  VkInstance instance = VK_NULL_HANDLE;
  VkPhysicalDevice physical = VK_NULL_HANDLE;
  VkDevice device = VK_NULL_HANDLE;
  VkQueue queue = VK_NULL_HANDLE;
  VkCommandPool command_pool = VK_NULL_HANDLE;
  VkCommandBuffer command = VK_NULL_HANDLE;
  VkDescriptorSetLayout set_layout = VK_NULL_HANDLE;
  VkDescriptorPool descriptor_pool = VK_NULL_HANDLE;
  VkDescriptorSet descriptor_set = VK_NULL_HANDLE;
  VkDescriptorSetLayout sampler_layout = VK_NULL_HANDLE;
  VkDescriptorSet sampler_set = VK_NULL_HANDLE;
  bool separate = false, samplerless = false;
  unsigned array_length = 1, array_index = 0, array_binding = 2;
  bool uniform_index = false, nonuniform_index = false;
  unsigned lane_invocations = 0, lane_active_a = 0, lane_active_b = 0, lane_mixes = 0;
  unsigned initial_array_index = 0;
  std::array<unsigned, 4> index_counts{};
  bool array_mode = false, image_a_initialized = false, image_b_initialized = false;
  bool sampler_a_initialized = false, sampler_b_initialized = false, copy_elements = false;
  unsigned element_writes = 0, element_copies = 0, consecutive_copies = 0;
  unsigned separate_sampler_bindings = 0;
  VkPipelineLayout pipeline_layout = VK_NULL_HANDLE;
  VkPipeline pipeline = VK_NULL_HANDLE;
  VkAccelerationStructureKHR tlas = VK_NULL_HANDLE;
  VkStridedDeviceAddressRegionKHR raygen{}, miss{}, empty{};
  VkPhysicalDeviceMemoryProperties memory_properties{};
  PFN_vkCreateAccelerationStructureKHR create_as = nullptr;
  PFN_vkDestroyAccelerationStructureKHR destroy_as = nullptr;
  PFN_vkGetAccelerationStructureBuildSizesKHR as_sizes = nullptr;
  PFN_vkCmdBuildAccelerationStructuresKHR build_as = nullptr;
  PFN_vkCreateRayTracingPipelinesKHR create_pipeline = nullptr;
  PFN_vkGetRayTracingShaderGroupHandlesKHR group_handles = nullptr;
  PFN_vkCmdTraceRaysKHR trace = nullptr;
  std::vector<Buffer> buffers;
  std::vector<Texture> textures;
  std::vector<VkImageView> extra_views;
  std::vector<VkSampler> samplers;
  std::vector<VkShaderModule> modules;

  ~Experiment() {
    if (device) {
      vkDeviceWaitIdle(device);
      if (command_pool) vkDestroyCommandPool(device, command_pool, nullptr);
      if (descriptor_pool) vkDestroyDescriptorPool(device, descriptor_pool, nullptr);
      if (pipeline) vkDestroyPipeline(device, pipeline, nullptr);
      if (pipeline_layout) vkDestroyPipelineLayout(device, pipeline_layout, nullptr);
      if (set_layout) vkDestroyDescriptorSetLayout(device, set_layout, nullptr);
      if (sampler_layout) vkDestroyDescriptorSetLayout(device, sampler_layout, nullptr);
      if (tlas && destroy_as) destroy_as(device, tlas, nullptr);
      for (auto module : modules) vkDestroyShaderModule(device, module, nullptr);
      for (auto sampler : samplers) vkDestroySampler(device, sampler, nullptr);
      for (auto view : extra_views) vkDestroyImageView(device, view, nullptr);
      for (auto texture : textures) {
        vkDestroyImageView(device, texture.view, nullptr);
        vkDestroyImage(device, texture.handle, nullptr);
        vkFreeMemory(device, texture.memory, nullptr);
      }
      for (auto buffer : buffers) {
        vkUnmapMemory(device, buffer.memory);
        vkDestroyBuffer(device, buffer.handle, nullptr);
        vkFreeMemory(device, buffer.memory, nullptr);
      }
      vkDestroyDevice(device, nullptr);
    }
    if (instance) vkDestroyInstance(instance, nullptr);
  }

  template <typename T> T proc(const char *name) {
    auto address = vkGetDeviceProcAddr(device, name);
    if (!address) throw std::runtime_error(std::string("missing Vulkan entrypoint: ") + name);
    return reinterpret_cast<T>(address);
  }

  void initialize() {
    uniform_index = std::getenv("VENTUS_TEXTURE_UNIFORM_INDEX") != nullptr;
    nonuniform_index = std::getenv("VENTUS_TEXTURE_NONUNIFORM_INDEX") != nullptr;
    VkApplicationInfo application{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    application.pApplicationName = "ventus-texture-model-comparison";
    application.apiVersion = VK_API_VERSION_1_2;
    VkInstanceCreateInfo info{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    info.pApplicationInfo = &application;
    check(vkCreateInstance(&info, nullptr, &instance), "vkCreateInstance");
    std::uint32_t count = 0;
    check(vkEnumeratePhysicalDevices(instance, &count, nullptr), "enumerate device count");
    if (count != 1) throw std::runtime_error("experiment requires exactly one Ventus device");
    check(vkEnumeratePhysicalDevices(instance, &count, &physical), "enumerate device");
    VkPhysicalDeviceProperties properties{};
    vkGetPhysicalDeviceProperties(physical, &properties);
    std::string name = properties.deviceName;
    if (name.find("Ventus") == std::string::npos && name.find("ventus") == std::string::npos)
      throw std::runtime_error("selected device is not Ventus: " + name);
    std::cout << "DEVICE " << name << std::endl;
    vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, nullptr);
    std::vector<VkQueueFamilyProperties> families(count);
    vkGetPhysicalDeviceQueueFamilyProperties(physical, &count, families.data());
    std::uint32_t family = 0;
    while (family < count && !(families[family].queueFlags & VK_QUEUE_COMPUTE_BIT)) ++family;
    if (family == count) throw std::runtime_error("no compute-capable RT queue");
    float priority = 1;
    VkDeviceQueueCreateInfo queue_info{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    queue_info.queueFamilyIndex = family;
    queue_info.queueCount = 1;
    queue_info.pQueuePriorities = &priority;
    VkPhysicalDeviceAccelerationStructureFeaturesKHR acceleration{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR};
    acceleration.accelerationStructure = VK_TRUE;
    VkPhysicalDeviceBufferDeviceAddressFeatures bda{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_BUFFER_DEVICE_ADDRESS_FEATURES};
    bda.pNext = &acceleration;
    bda.bufferDeviceAddress = VK_TRUE;
    VkPhysicalDeviceRayTracingPipelineFeaturesKHR rt{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RAY_TRACING_PIPELINE_FEATURES_KHR};
    rt.pNext = &bda;
    rt.rayTracingPipeline = VK_TRUE;
    const char *extensions[]{VK_KHR_ACCELERATION_STRUCTURE_EXTENSION_NAME,
                             VK_KHR_RAY_TRACING_PIPELINE_EXTENSION_NAME,
                             VK_KHR_DEFERRED_HOST_OPERATIONS_EXTENSION_NAME};
    VkDeviceCreateInfo device_info{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    VkPhysicalDeviceFeatures core_features{};
    core_features.shaderInt64 = VK_TRUE;
    VkPhysicalDeviceDescriptorIndexingFeatures enabled_indexing{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES};
    if (uniform_index) {
      VkPhysicalDeviceFeatures features{};
      vkGetPhysicalDeviceFeatures(physical, &features);
      VkPhysicalDeviceDescriptorIndexingFeatures indexing{
          VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES};
      VkPhysicalDeviceFeatures2 features2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2};
      features2.pNext = &indexing;
      vkGetPhysicalDeviceFeatures2(physical, &features2);
      VkPhysicalDeviceDescriptorIndexingProperties index_properties{
          VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_PROPERTIES};
      VkPhysicalDeviceProperties2 properties2{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};
      properties2.pNext = &index_properties;
      vkGetPhysicalDeviceProperties2(physical, &properties2);
      if (index_properties.shaderSampledImageArrayNonUniformIndexingNative ||
          index_properties.quadDivergentImplicitLod)
        throw std::runtime_error("unexpected native or implicit-LOD descriptor promise");
      if (!features.shaderSampledImageArrayDynamicIndexing ||
          !features2.features.shaderSampledImageArrayDynamicIndexing ||
          !indexing.shaderSampledImageArrayNonUniformIndexing || indexing.runtimeDescriptorArray ||
          indexing.shaderStorageImageArrayNonUniformIndexing || indexing.shaderStorageBufferArrayNonUniformIndexing ||
          indexing.descriptorBindingPartiallyBound || indexing.descriptorBindingVariableDescriptorCount ||
          features.shaderStorageImageArrayDynamicIndexing || features.shaderStorageBufferArrayDynamicIndexing ||
          features.shaderUniformBufferArrayDynamicIndexing)
        throw std::runtime_error("unexpected sampled array feature boundary");
      core_features.shaderSampledImageArrayDynamicIndexing = VK_TRUE;
      if (nonuniform_index) {
        enabled_indexing.shaderSampledImageArrayNonUniformIndexing = VK_TRUE;
        acceleration.pNext = &enabled_indexing;
      }
    }
    device_info.pEnabledFeatures = &core_features;
    device_info.pNext = &rt;
    device_info.queueCreateInfoCount = 1;
    device_info.pQueueCreateInfos = &queue_info;
    device_info.enabledExtensionCount = 3;
    device_info.ppEnabledExtensionNames = extensions;
    check(vkCreateDevice(physical, &device_info, nullptr, &device), "vkCreateDevice");
    if (uniform_index)
      std::cout << "FEATURE sampled_dynamic_query=1 sampled_dynamic_query2=1 sampled_dynamic_enabled=1"
                << " sampled_nonuniform_query=1 sampled_nonuniform_enabled=" << nonuniform_index
                << " runtime_array=0 partially_bound=0 variable_count=0"
                   " storage_dynamic=0 uniform_buffer_dynamic=0 sampled_native=0 implicit_divergent_lod=0" << std::endl;
    vkGetDeviceQueue(device, family, 0, &queue);
    create_as = proc<PFN_vkCreateAccelerationStructureKHR>("vkCreateAccelerationStructureKHR");
    destroy_as = proc<PFN_vkDestroyAccelerationStructureKHR>("vkDestroyAccelerationStructureKHR");
    as_sizes = proc<PFN_vkGetAccelerationStructureBuildSizesKHR>("vkGetAccelerationStructureBuildSizesKHR");
    build_as = proc<PFN_vkCmdBuildAccelerationStructuresKHR>("vkCmdBuildAccelerationStructuresKHR");
    create_pipeline = proc<PFN_vkCreateRayTracingPipelinesKHR>("vkCreateRayTracingPipelinesKHR");
    group_handles = proc<PFN_vkGetRayTracingShaderGroupHandlesKHR>("vkGetRayTracingShaderGroupHandlesKHR");
    trace = proc<PFN_vkCmdTraceRaysKHR>("vkCmdTraceRaysKHR");
    VkCommandPoolCreateInfo pool_info{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pool_info.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pool_info.queueFamilyIndex = family;
    check(vkCreateCommandPool(device, &pool_info, nullptr, &command_pool), "create command pool");
    VkCommandBufferAllocateInfo allocate{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    allocate.commandPool = command_pool;
    allocate.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocate.commandBufferCount = 1;
    check(vkAllocateCommandBuffers(device, &allocate, &command), "allocate command buffer");
  }

  VkDeviceMemory allocate(VkMemoryRequirements requirements, bool addressable) {
    std::uint32_t type = 0;
    const auto wanted = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    while (type < memory_properties.memoryTypeCount &&
           (!(requirements.memoryTypeBits & (1U << type)) ||
            (memory_properties.memoryTypes[type].propertyFlags & wanted) != wanted)) ++type;
    if (type == memory_properties.memoryTypeCount)
      throw std::runtime_error("profile requires host-visible coherent backing memory");
    VkMemoryAllocateFlagsInfo flags{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO};
    flags.flags = VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT;
    VkMemoryAllocateInfo info{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    info.pNext = addressable ? &flags : nullptr;
    info.allocationSize = requirements.size;
    info.memoryTypeIndex = type;
    VkDeviceMemory memory;
    check(vkAllocateMemory(device, &info, nullptr, &memory), "allocate memory");
    return memory;
  }

  Buffer buffer(std::uint64_t size, VkBufferUsageFlags usage) {
    VkBufferCreateInfo info{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    info.size = size;
    info.usage = usage;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    Buffer buffer{};
    check(vkCreateBuffer(device, &info, nullptr, &buffer.handle), "create buffer");
    VkMemoryRequirements requirements{};
    vkGetBufferMemoryRequirements(device, buffer.handle, &requirements);
    buffer.memory = allocate(requirements, usage & VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
    check(vkBindBufferMemory(device, buffer.handle, buffer.memory, 0), "bind buffer");
    check(vkMapMemory(device, buffer.memory, 0, VK_WHOLE_SIZE, 0, &buffer.map), "map buffer");
    std::memset(buffer.map, 0, size);
    buffers.push_back(buffer);
    return buffer;
  }

  VkDeviceAddress address(Buffer buffer) {
    VkBufferDeviceAddressInfo info{VK_STRUCTURE_TYPE_BUFFER_DEVICE_ADDRESS_INFO};
    info.buffer = buffer.handle;
    const auto result = vkGetBufferDeviceAddress(device, &info);
    if (!result) throw std::runtime_error("buffer has no device address");
    return result;
  }

  static bool depth() {return std::getenv("VENTUS_TEXTURE_DEPTH")!=nullptr;}
  static bool depth_compare() {return std::getenv("VENTUS_TEXTURE_DEPTH_COMPARE")!=nullptr;}
  static void depth_state(Sampling &state,bool second=false) {
    if (!depth_compare()) return;
    state.compare_enable=true;
    const unsigned op=std::stoul(std::getenv("VENTUS_TEXTURE_COMPARE_OP"));
    state.compare_op=static_cast<CompareOp>((op+(second ? 2 : 0))%8);
  }
  static float dref(unsigned i,bool second=false) {
    static const std::array<float,12> values{-1,0,.125f,.375f,std::nextafter(.5f,0.f),.5f,
      std::nextafter(.5f,1.f),.875f,1,1.125f,-std::numeric_limits<float>::max(),std::numeric_limits<float>::max()};
    return values[(second ? 7*i+3 : i)%values.size()];
  }
  static bool compare(float reference,float texel,CompareOp op) {
    const std::array<bool,8> truth{false,reference<texel,reference==texel,reference<=texel,
      reference>texel,reference!=texel,reference>=texel,true};
    return truth.at(static_cast<unsigned>(op));
  }
  static Result depth_reference(const Image &image,View view,Sampling state,double u,double v,float reference,double lod,LodRange range) {
    auto result=sample_compare_lod(image,view,state,u,v,reference,lod,range);
    if(std::getenv("VENTUS_TEXTURE_DEPTH_ORDER_MISMATCH")) {
      state.compare_enable=false;
      const auto filtered=sample_lod(image,view,state,u,v,lod,range);
      result.rgba.fill(compare(reference,filtered.rgba[0],state.compare_op) ? 1.f : 0.f);
    }
    return result;
  }
  static bool volume() { return std::getenv("VENTUS_TEXTURE_VOLUME_DEPTH")!=nullptr; }
  static unsigned volume_depth() { const auto text=std::getenv("VENTUS_TEXTURE_VOLUME_DEPTH");return text ? static_cast<unsigned>(std::stoul(text)) : 1; }
  static float volume_w(unsigned i,bool second=false) {
    const std::array<float,16> values{0,1,-1,2,.5f,.25f,.75f,-.125f,1.125f,-.0f,65536.125f,-65536.375f,.0625f,.9375f,.375f,.625f};
    return values[(second ? i*7+3 : i)%values.size()];
  }
  static void volume_state(Sampling &state) {if(volume()) state.address_w=static_cast<AddressMode>((static_cast<unsigned>(state.address_u)+2*static_cast<unsigned>(state.address_v)+static_cast<unsigned>(state.mag_filter))%4);}
  static Result volume_reference(const Image &image,View view,Sampling state,double u,double v,double w,double lod,LodRange range) {
    auto result=sample_volume_lod(image,view,state,u,v,w,lod,range);
    if(std::getenv("VENTUS_TEXTURE_VOLUME_FILTER_MISMATCH")) {
      const auto depth=image.levels[result.mip].depth;
      w=(std::floor(w*depth)+.5)/depth;
      return sample_volume_lod(image,view,state,u,v,w,lod,range);
    }
    return result;
  }
  static bool cube() { return std::getenv("VENTUS_TEXTURE_CUBE_COUNT") != nullptr; }
  static unsigned cube_count() { const auto text=std::getenv("VENTUS_TEXTURE_CUBE_COUNT");return text ? static_cast<unsigned>(std::stoul(text)) : 1; }
  static bool image_array() { return std::getenv("VENTUS_TEXTURE_IMAGE_LAYERS") != nullptr; }
  static unsigned image_layers() {
    const auto text = std::getenv("VENTUS_TEXTURE_IMAGE_LAYERS");
    return cube() ? 6*cube_count() : text ? static_cast<unsigned>(std::stoul(text)) : 1;
  }
  static bool array_sampling() { return image_array() && !std::getenv("VENTUS_TEXTURE_SINGLE_LAYER_VIEW"); }
  static View layer_view(View view, unsigned width, unsigned height, unsigned extra = 0) {
    if (cube()) {
      const auto text=std::getenv("VENTUS_TEXTURE_CUBE_BASE_LAYER");
      view.base_layer=text ? static_cast<unsigned>(std::stoul(text)) : ((view.base_mip+width+height+extra)%cube_count())*6;
      view.layer_count=6;
    } else if (image_array()) {
      view.base_layer = (view.base_mip + width + height + extra) % image_layers();
      view.layer_count = array_sampling() ? image_layers() - view.base_layer : 1;
    }
    return view;
  }
  static std::array<float,3> cube_direction(unsigned i,bool second=false) {
    if (second) i=(i*7+23)%64;
    if (i==54) return {1,1,0};
    if (i==55) return {1,0,1};
    if (i==56) return {0,1,1};
    if (i==57) return {1,std::nextafter(1.f,0.f),0};
    if (i==58) return {1,std::nextafter(1.f,2.f),0};
    if (i==59) return {std::numeric_limits<float>::max(),0,-std::numeric_limits<float>::max()};
    if (i==60) return {std::numeric_limits<float>::denorm_min(),0,std::numeric_limits<float>::denorm_min()};
    if (i>=61) return i==61 ? std::array<float,3>{1,.96875f,.96875f} : i==62 ? std::array<float,3>{-.96875f,1,.96875f} : std::array<float,3>{.96875f,-.96875f,-1};
    static const std::array<std::array<float,2>,9> uv{{{0,0},{-.75f,.25f},{.5f,-.5f},{-.96875f,.5f},{.96875f,-.25f},{.25f,-.96875f},{-.5f,.96875f},{-1,-1},{1,1}}};
    const auto a=uv[i%9][0],b=uv[i%9][1];
    switch(i/9) {
    case 0:return {1,-b,-a};case 1:return {-1,-b,a};case 2:return {a,1,b};
    case 3:return {a,-1,-b};case 4:return {a,-b,1};default:return {-a,-b,-1};
    }
  }
  static std::array<double,3> cube_model_direction(unsigned i,bool second=false) {
    const auto d=cube_direction(i,second);return {d[0],d[1],d[2]};
  }

  static Result cube_reference(const Image &image,View view,Sampling state,std::array<double,3> direction,double lod,LodRange range) {
    auto result=sample_cube_lod(image,view,state,direction,lod,range);
    if (std::getenv("VENTUS_TEXTURE_INJECT_CUBE_SEAM_MISMATCH")) {
      state.address_u=state.address_v=AddressMode::ClampToEdge;
      auto clamp=sample_lod(image,view,state,result.input_u,result.input_v,lod,range,{},result.cube_face);
      clamp.from_cube=true;clamp.cube_face=result.cube_face;clamp.input_direction=direction;
      return clamp;
    }
    return result;
  }

  static float layer_request(unsigned i, bool second = false) {
    static const std::array<float,16> values{-100,0,0.25f,0.5f,std::nextafter(0.5f,1.f),
      0.75f,1,1.5f,std::nextafter(1.5f,2.f),1.75f,2,2.5f,
      std::nextafter(2.5f,3.f),3,100,std::numeric_limits<float>::max()};
    return values[(second ? i * 7 + 3 : i) % values.size()];
  }

  Texture texture(unsigned width, unsigned height, VkFormat format, VkImageUsageFlags usage,
                  unsigned levels = 1, unsigned base = 0, unsigned count = 1) {
    VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    const bool sampled = usage & VK_IMAGE_USAGE_SAMPLED_BIT;
    info.imageType = sampled && volume() ? VK_IMAGE_TYPE_3D : VK_IMAGE_TYPE_2D;
    info.format = format;
    info.extent = {width, height, sampled ? volume_depth() : 1};
    info.mipLevels = levels;
    info.arrayLayers = sampled ? image_layers() : 1;
    info.flags = sampled && cube() ? VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT : 0;
    info.samples = VK_SAMPLE_COUNT_1_BIT;
    info.tiling = sampled && std::getenv("VENTUS_TEXTURE_LINEAR_IMAGE") ? VK_IMAGE_TILING_LINEAR : VK_IMAGE_TILING_OPTIMAL;
    info.usage = usage;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    Texture texture{};
    texture.depth_view = format==VK_FORMAT_D32_SFLOAT;
    texture.linear = info.tiling == VK_IMAGE_TILING_LINEAR;
    texture.layers = info.arrayLayers;
    texture.array_view = sampled && array_sampling();
    texture.cube_view = sampled && cube();
    texture.volume_view = sampled && volume();
    check(vkCreateImage(device, &info, nullptr, &texture.handle), "create image");
    VkMemoryRequirements requirements{};
    vkGetImageMemoryRequirements(device, texture.handle, &requirements);
    texture.memory = allocate(requirements, false);
    check(vkBindImageMemory(device, texture.handle, texture.memory, 0), "bind image");
    VkImageViewCreateInfo view{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    view.image = texture.handle;
    view.viewType = texture.volume_view ? VK_IMAGE_VIEW_TYPE_3D : texture.cube_view ? VK_IMAGE_VIEW_TYPE_CUBE : texture.array_view ? VK_IMAGE_VIEW_TYPE_2D_ARRAY : VK_IMAGE_VIEW_TYPE_2D;
    view.format = format;
    const auto range = sampled ? layer_view(View{base, count},width,height) : View{base,count};
    view.subresourceRange = {texture.depth_view ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, base, count, range.base_layer, range.layer_count};
    check(vkCreateImageView(device, &view, nullptr, &texture.view), "create image view");
    textures.push_back(texture);
    return texture;
  }

  Texture additional_view(Texture image, VkFormat format, View range) {
    VkImageViewCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    info.image = image.handle;
    info.viewType = image.volume_view ? VK_IMAGE_VIEW_TYPE_3D : image.cube_view ? VK_IMAGE_VIEW_TYPE_CUBE : image.array_view ? VK_IMAGE_VIEW_TYPE_2D_ARRAY : VK_IMAGE_VIEW_TYPE_2D;
    info.format = format;
    info.components = vk_mapping(range.components);
    info.subresourceRange = {image.depth_view ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, range.base_mip, range.level_count, range.base_layer, range.layer_count};
    check(vkCreateImageView(device, &info, nullptr, &image.view), "create additional image view");
    extra_views.push_back(image.view);
    return image;
  }

  void begin() {
    check(vkResetCommandBuffer(command, 0), "reset command");
    VkCommandBufferBeginInfo info{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    info.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    check(vkBeginCommandBuffer(command, &info), "begin command");
  }

  void submit() {
    check(vkEndCommandBuffer(command), "end command");
    VkSubmitInfo info{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    info.commandBufferCount = 1;
    info.pCommandBuffers = &command;
    check(vkQueueSubmit(queue, 1, &info, VK_NULL_HANDLE), "queue submit");
    check(vkQueueWaitIdle(queue), "queue wait");
  }

  void transition(Texture image, VkImageLayout old_layout, VkImageLayout new_layout,
                  VkPipelineStageFlags src, VkPipelineStageFlags dst,
                  VkAccessFlags src_access, VkAccessFlags dst_access) {
    VkImageMemoryBarrier barrier{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    barrier.srcAccessMask = src_access;
    barrier.dstAccessMask = dst_access;
    barrier.oldLayout = old_layout;
    barrier.newLayout = new_layout;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.image = image.handle;
    barrier.subresourceRange = {image.depth_view ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, 0, VK_REMAINING_MIP_LEVELS, 0, image.layers};
    vkCmdPipelineBarrier(command, src, dst, 0, 0, nullptr, 0, nullptr, 1, &barrier);
  }

  void empty_tlas() {
    auto input = buffer(64, VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR |
                            VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
    VkAccelerationStructureGeometryKHR geometry{VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR};
    geometry.geometryType = VK_GEOMETRY_TYPE_INSTANCES_KHR;
    geometry.geometry.instances.sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_INSTANCES_DATA_KHR;
    geometry.geometry.instances.data.deviceAddress = address(input);
    VkAccelerationStructureBuildGeometryInfoKHR build{VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_GEOMETRY_INFO_KHR};
    build.type = VK_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL_KHR;
    build.mode = VK_BUILD_ACCELERATION_STRUCTURE_MODE_BUILD_KHR;
    build.geometryCount = 1;
    build.pGeometries = &geometry;
    std::uint32_t primitives = 0;
    VkAccelerationStructureBuildSizesInfoKHR sizes{VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_SIZES_INFO_KHR};
    as_sizes(device, VK_ACCELERATION_STRUCTURE_BUILD_TYPE_DEVICE_KHR, &build, &primitives, &sizes);
    auto storage = buffer(sizes.accelerationStructureSize,
                          VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_STORAGE_BIT_KHR |
                          VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
    VkAccelerationStructureCreateInfoKHR info{VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_CREATE_INFO_KHR};
    info.buffer = storage.handle;
    info.size = sizes.accelerationStructureSize;
    info.type = VK_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL_KHR;
    check(create_as(device, &info, nullptr, &tlas), "create empty TLAS");
    VkPhysicalDeviceAccelerationStructurePropertiesKHR properties{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_PROPERTIES_KHR};
    VkPhysicalDeviceProperties2 props{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};
    props.pNext = &properties;
    vkGetPhysicalDeviceProperties2(physical, &props);
    const auto alignment = properties.minAccelerationStructureScratchOffsetAlignment;
    if (!alignment) throw std::runtime_error("invalid scratch alignment");
    auto scratch = buffer(std::max<std::uint64_t>(sizes.buildScratchSize, 1) + alignment,
                          VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
    build.dstAccelerationStructure = tlas;
    build.scratchData.deviceAddress = align_up(address(scratch), alignment);
    VkAccelerationStructureBuildRangeInfoKHR range{};
    const auto *range_ptr = &range;
    begin();
    build_as(command, 1, &build, &range_ptr);
    submit();
  }

  VkShaderModule module(const char *path) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    const auto bytes = stream.tellg();
    if (!stream || bytes <= 0 || bytes % 4) throw std::runtime_error("bad SPIR-V file");
    std::vector<std::uint32_t> code(static_cast<std::size_t>(bytes) / 4);
    stream.seekg(0);
    stream.read(reinterpret_cast<char *>(code.data()), bytes);
    if (!stream) throw std::runtime_error("truncated SPIR-V file");
    VkShaderModuleCreateInfo info{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    info.codeSize = code.size() * 4;
    info.pCode = code.data();
    VkShaderModule module;
    check(vkCreateShaderModule(device, &info, nullptr, &module), "create shader module");
    modules.push_back(module);
    return module;
  }

  void setup_pipeline(const char *rgen, const char *rmiss, bool multiple_bindings = false,
                      bool separate_images = false, bool no_sampler = false) {
    separate = separate_images;
    samplerless = no_sampler;
    std::vector<VkDescriptorType> types{
        VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
        VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER};
    if (const char *length = std::getenv("VENTUS_TEXTURE_ARRAY_LENGTH")) {
      array_mode = true;
      array_length = std::stoul(length);
      array_index = std::stoul(std::getenv("VENTUS_TEXTURE_ARRAY_INDEX"));
      initial_array_index = array_index;
      array_binding = std::stoul(std::getenv("VENTUS_TEXTURE_ARRAY_BINDING"));
    }
    if (separate) types[2] = VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE;
    if (multiple_bindings) types.push_back(types[2]);
    std::vector<VkDescriptorSetLayoutBinding> bindings(types.size());
    for (unsigned i = 0; i < bindings.size(); ++i)
      bindings[i] = {i == 6 ? 13 : i == 2 ? array_binding : i, types[i], i == 2 || i == 6 ? array_length : 1, VK_SHADER_STAGE_RAYGEN_BIT_KHR, nullptr};
    VkDescriptorSetLayoutCreateInfo layout{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    layout.bindingCount = bindings.size();
    layout.pBindings = bindings.data();
    check(vkCreateDescriptorSetLayout(device, &layout, nullptr, &set_layout), "create descriptor layout");
    std::vector<VkDescriptorPoolSize> sizes{{types[0], 1}, {types[1], 1},
        {types[2], array_length * (multiple_bindings ? 2U : 1U)}, {types[3], 3}};
    if (separate && !samplerless) {
      const VkDescriptorSetLayoutBinding states[]{{6, VK_DESCRIPTOR_TYPE_SAMPLER, array_length,
          VK_SHADER_STAGE_RAYGEN_BIT_KHR, nullptr}, {7, VK_DESCRIPTOR_TYPE_SAMPLER, array_length,
          VK_SHADER_STAGE_RAYGEN_BIT_KHR, nullptr}};
      VkDescriptorSetLayoutCreateInfo state_layout{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
      state_layout.bindingCount = multiple_bindings ? 2 : 1;
      state_layout.pBindings = states;
      check(vkCreateDescriptorSetLayout(device, &state_layout, nullptr, &sampler_layout), "create sampler layout");
      sizes.push_back({VK_DESCRIPTOR_TYPE_SAMPLER, array_length * (multiple_bindings ? 2U : 1U)});
      separate_sampler_bindings = multiple_bindings ? 2 : 1;
    }
    VkDescriptorPoolCreateInfo pool{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    pool.maxSets = sampler_layout ? 2 : 1;
    if (array_mode) pool.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    pool.poolSizeCount = sizes.size();
    pool.pPoolSizes = sizes.data();
    check(vkCreateDescriptorPool(device, &pool, nullptr, &descriptor_pool), "create descriptor pool");
    VkDescriptorSetAllocateInfo allocate{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    allocate.descriptorPool = descriptor_pool;
    allocate.descriptorSetCount = 1;
    allocate.pSetLayouts = &set_layout;
    check(vkAllocateDescriptorSets(device, &allocate, &descriptor_set), "allocate descriptor set");
    if (sampler_layout) {
      allocate.pSetLayouts = &sampler_layout;
      check(vkAllocateDescriptorSets(device, &allocate, &sampler_set), "allocate sampler set");
    }
    const VkDescriptorSetLayout layouts[]{set_layout, sampler_layout};
    VkPipelineLayoutCreateInfo pipeline_info{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    pipeline_info.setLayoutCount = sampler_layout ? 2 : 1;
    pipeline_info.pSetLayouts = layouts;
    const VkPushConstantRange selectors{VK_SHADER_STAGE_RAYGEN_BIT_KHR, 0, 16};
    if (uniform_index) {
      pipeline_info.pushConstantRangeCount = 1;
      pipeline_info.pPushConstantRanges = &selectors;
    }
    check(vkCreatePipelineLayout(device, &pipeline_info, nullptr, &pipeline_layout), "create pipeline layout");
    VkPipelineShaderStageCreateInfo stages[2]{};
    VkRayTracingShaderGroupCreateInfoKHR groups[2]{};
    const char *paths[]{rgen, rmiss};
    for (unsigned i = 0; i < 2; ++i) {
      stages[i].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
      stages[i].stage = i ? VK_SHADER_STAGE_MISS_BIT_KHR : VK_SHADER_STAGE_RAYGEN_BIT_KHR;
      stages[i].module = module(paths[i]);
      stages[i].pName = "main";
      groups[i].sType = VK_STRUCTURE_TYPE_RAY_TRACING_SHADER_GROUP_CREATE_INFO_KHR;
      groups[i].type = VK_RAY_TRACING_SHADER_GROUP_TYPE_GENERAL_KHR;
      groups[i].generalShader = i;
      groups[i].closestHitShader = groups[i].anyHitShader = groups[i].intersectionShader = VK_SHADER_UNUSED_KHR;
    }
    VkRayTracingPipelineCreateInfoKHR info{VK_STRUCTURE_TYPE_RAY_TRACING_PIPELINE_CREATE_INFO_KHR};
    info.stageCount = info.groupCount = 2;
    info.pStages = stages;
    info.pGroups = groups;
    info.maxPipelineRayRecursionDepth = 1;
    info.layout = pipeline_layout;
    check(create_pipeline(device, VK_NULL_HANDLE, VK_NULL_HANDLE, 1, &info, nullptr, &pipeline),
          "create RT pipeline");
    VkPhysicalDeviceRayTracingPipelinePropertiesKHR properties{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RAY_TRACING_PIPELINE_PROPERTIES_KHR};
    VkPhysicalDeviceProperties2 props{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};
    props.pNext = &properties;
    vkGetPhysicalDeviceProperties2(physical, &props);
    const auto stride = align_up(properties.shaderGroupHandleSize, properties.shaderGroupBaseAlignment);
    auto sbt = buffer(2 * stride + properties.shaderGroupBaseAlignment,
                      VK_BUFFER_USAGE_SHADER_BINDING_TABLE_BIT_KHR | VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
    const auto base = align_up(address(sbt), properties.shaderGroupBaseAlignment);
    std::vector<std::uint8_t> handles(2 * properties.shaderGroupHandleSize);
    check(group_handles(device, pipeline, 0, 2, handles.size(), handles.data()), "get SBT handles");
    auto *map = static_cast<std::uint8_t *>(sbt.map) + base - address(sbt);
    for (unsigned i = 0; i < 2; ++i)
      std::memcpy(map + i * stride, handles.data() + i * properties.shaderGroupHandleSize,
                  properties.shaderGroupHandleSize);
    raygen = {base, stride, stride};
    miss = {base + stride, stride, stride};
  }

  static TexelOffset texel_offset() {
    if (const char *text=std::getenv("VENTUS_TEXTURE_OFFSET")) {
      char *end=nullptr;
      const long x=std::strtol(text,&end,10);
      if (!end || *end!=',') throw std::runtime_error("invalid texel offset");
      const long y=std::strtol(end+1,&end,10);
      if (!end || *end!='\0' || x < -8 || x > 7 || y < -8 || y > 7)
        throw std::runtime_error("texel offset outside [-8,7]");
      return {static_cast<int32_t>(x),static_cast<int32_t>(y)};
    }
    return {};
  }

  static bool gradients() { return std::getenv("VENTUS_TEXTURE_GRADIENTS")!=nullptr; }

  static std::array<float,4> gradient_request(unsigned i) {
    if (const char *text=std::getenv("VENTUS_TEXTURE_CONSTANT_GRADIENT")) {
      std::array<float,4> value{};
      for (unsigned c=0;c<4;++c) {
        char *end=nullptr;
        value[c]=std::strtof(text,&end);
        if (!end || (c<3 ? *end!=',' : *end!='\0')) throw std::runtime_error("invalid constant gradient");
        text=end+1;
      }
      return value;
    }
    static const std::array<std::array<float,2>,32> corpus{{
      {0,0},{1.f/64,0},{0,1.f/64},{1.f/32,1.f/32},{-1.f/16,0},{0,-1.f/16},
      {1.f/8,0},{1.f/8,1.f/8},{0.25f,-0.25f},{0.5f,0},{0,0.5f},{0.5f,-0.5f},
      {1,0},{1,1},{2,-2},{4,0},{16,-16},{-0.f,0.f},
      {0x1p-16f,0},{0x1p-15f,0x1p-16f},{0x1p-30f,0},{0x1p-100f,0},
      {0x1p20f,0},{0x1p60f,0x1p60f},{std::numeric_limits<float>::max(),0},
      {std::numeric_limits<float>::denorm_min(),0},{0.3f,0.2f},{-0.1f,0.25f},
      {0.999f,1.001f},{0.249f,0.251f},{0x1p-10f,0x1p-12f},{8,-0.5f}
    }};
    const auto d=corpus[i%corpus.size()];
    if (i%corpus.size()==29) return {0,0,0.25f,0.125f};
    if (i%corpus.size()==30) return {0.125f,0,0,0};
    if (i%corpus.size()==31) return {0.125f,0.125f,0,0};
    return {d[0],d[1],-d[1],d[0]};
  }

  static std::vector<std::array<float,4>> gradient_inputs(const std::vector<std::array<float,4>> &requests, bool multi = false) {
    if (multi && depth_compare()) {
      std::vector<std::array<float,4>> packed;
      for (unsigned i=0;i<requests.size();++i) {
        packed.push_back({requests[i][0],requests[i][1],requests[i][2],dref(i)});
        packed.push_back({requests[i][1],requests[i][0],requests[i][3],dref(i,true)});
      }
      return packed;
    }
    if (cube() || volume()) {
      std::vector<std::array<float,4>> packed;
      for (unsigned i=0;i<requests.size();++i) {
        const auto a=volume() ? std::array<float,3>{requests[i][0],requests[i][1],volume_w(i)} : cube_direction(i);
        const auto b=volume() ? std::array<float,3>{requests[i][1],requests[i][0],volume_w(i,true)} : cube_direction(i,true);
        packed.push_back({a[0],a[1],a[2],requests[i][2]});
        if (multi) packed.push_back({b[0],b[1],b[2],requests[i][3]});
      }
      return packed;
    }
    if (!gradients() && !(multi && array_sampling())) return requests;
    std::vector<std::array<float,4>> packed;
    for (unsigned i=0;i<requests.size();++i) {
      packed.push_back(requests[i]);
      const auto d=gradient_request(i);
      packed.push_back({gradients() ? d[2] : 0,gradients() ? d[3] : 0,
                        array_sampling() ? layer_request(i) : 0,
                        array_sampling() ? layer_request(i,true) : 0});
    }
    return packed;
  }

  static float requested_bias() {
    const char *text=std::getenv("VENTUS_TEXTURE_LOD_BIAS");
    return text ? std::strtof(text,nullptr) : 0;
  }

  VkSampler sampler(Sampling sampling, LodRange range = {0, 0},
                    VkSamplerMipmapMode mode = VK_SAMPLER_MIPMAP_MODE_NEAREST) {
    const VkSamplerAddressMode modes[]{VK_SAMPLER_ADDRESS_MODE_REPEAT,
                                      VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT,
                                      VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE,
                                      VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER};
    const VkBorderColor colors[]{VK_BORDER_COLOR_FLOAT_TRANSPARENT_BLACK,
                                 VK_BORDER_COLOR_FLOAT_OPAQUE_BLACK,VK_BORDER_COLOR_FLOAT_OPAQUE_WHITE};
    VkSamplerCreateInfo info{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
    info.magFilter = sampling.mag_filter == Filter::Nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
    info.minFilter = sampling.min_filter == Filter::Nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
    info.mipmapMode = mode;
    info.addressModeU = modes[static_cast<unsigned>(sampling.address_u)];
    info.addressModeV = modes[static_cast<unsigned>(sampling.address_v)];
    info.addressModeW = modes[static_cast<unsigned>(sampling.address_w)];
    info.mipLodBias = std::getenv("VENTUS_TEXTURE_INJECT_BIAS_MISMATCH") ? 0 : sampling.lod_bias;
    info.minLod = range.min;
    info.maxLod = range.max;
    info.compareEnable = sampling.compare_enable;
    info.compareOp = static_cast<VkCompareOp>(sampling.compare_op);
    info.borderColor = colors[static_cast<unsigned>(sampling.border_color)];
    VkSampler result;
    check(vkCreateSampler(device, &info, nullptr, &result), "create sampler");
    samplers.push_back(result);
    return result;
  }

  void array_lifecycle_checks() {
    if (!array_mode) return;
    const VkDescriptorSet old[]{descriptor_set, sampler_set};
    check(vkFreeDescriptorSets(device, descriptor_pool, sampler_set ? 2 : 1, old), "free array sets");
    descriptor_set = sampler_set = VK_NULL_HANDLE;
    auto allocate = [&]() {
      VkDescriptorSetAllocateInfo info{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
      info.descriptorPool = descriptor_pool;
      info.descriptorSetCount = 1;
      info.pSetLayouts = &set_layout;
      check(vkAllocateDescriptorSets(device, &info, &descriptor_set), "reallocate array image set");
      if (sampler_layout) {
        info.pSetLayouts = &sampler_layout;
        check(vkAllocateDescriptorSets(device, &info, &sampler_set), "reallocate array sampler set");
      }
    };
    allocate();
    const VkDescriptorSetLayout overflow_layouts[]{set_layout, set_layout};
    VkDescriptorSet overflow[]{descriptor_set, descriptor_set};
    VkDescriptorSetAllocateInfo excessive{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    excessive.descriptorPool = descriptor_pool;
    excessive.descriptorSetCount = 2;
    excessive.pSetLayouts = overflow_layouts;
    if (vkAllocateDescriptorSets(device, &excessive, overflow) != VK_ERROR_OUT_OF_POOL_MEMORY ||
        overflow[0] != VK_NULL_HANDLE || overflow[1] != VK_NULL_HANDLE)
      throw std::runtime_error("failed pool allocation must clear every returned handle");
    check(vkResetDescriptorPool(device, descriptor_pool, 0), "reset array pool");
    descriptor_set = sampler_set = VK_NULL_HANDLE;
    allocate();
    std::cout << "LIFECYCLE free_reallocate=1 reset_reallocate=1 failed_allocate_null=1" << std::endl;
  }

  void select_array_index(unsigned case_number, bool multi) {
    if (!uniform_index) return;
    // Phase 6 repeats phase 5 without descriptor writes or selector changes.
    const unsigned selector_case = case_number - (multi && case_number % 8 == 6 ? 1U : 0U);
    array_index = (initial_array_index + selector_case) % array_length;
    ++index_counts[array_index];
  }

  void print_uniform_profile() {
    if (!uniform_index) return;
    std::cout << "UNIFORM pipelines=1";
    for (unsigned i = 0; i < array_length; ++i)
      std::cout << " index" << i << '=' << index_counts[i];
    std::cout << std::endl;
    if (nonuniform_index)
      std::cout << "LANES invocations=" << lane_invocations << " active_a=" << lane_active_a
                << " active_b=" << lane_active_b << " inactive_a=" << lane_invocations - lane_active_a
                << " inactive_b=" << lane_invocations - lane_active_b << " reconverged_mixes=" << lane_mixes << std::endl;
  }

  void bind_descriptors() {
    const VkDescriptorSet sets[]{descriptor_set, sampler_set};
    vkCmdBindDescriptorSets(command, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR,
        pipeline_layout, 0, sampler_set ? 2 : 1, sets, 0, nullptr);
    if (uniform_index) {
      const unsigned pushed_index = std::getenv("VENTUS_TEXTURE_INJECT_INDEX_MISMATCH") ? 0 : array_index;
      const std::array<std::uint32_t, 4> selectors{pushed_index, array_length - 1 - pushed_index, 0, 0};
      vkCmdPushConstants(command, pipeline_layout, VK_SHADER_STAGE_RAYGEN_BIT_KHR,
                         0, sizeof(selectors), selectors.data());
    }
  }

  void image_descriptor(VkDescriptorSet set, VkDescriptorType type, unsigned binding,
                        const VkDescriptorImageInfo &image, bool &initialized,
                        unsigned selected) {
    VkWriteDescriptorSet write{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
    write.dstSet = set;
    write.dstBinding = binding;
    write.descriptorType = type;
    std::vector<VkDescriptorImageInfo> all(array_length, image);
    write.descriptorCount = initialized ? 1 : array_length;
    write.dstArrayElement = initialized ? selected : 0;
    write.pImageInfo = initialized ? &image : all.data();
    const bool copying = array_mode && array_length > 1 && copy_elements && initialized;
    if (!copying) {
      vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);
      ++element_writes;
    }
    initialized = true;
    if (copying) {
      // The adjacent-binding profile copies two descriptors across a binding
      // boundary; selected is the second destination, so copying only element0
      // cannot accidentally satisfy the numerical comparison.
      const bool consecutive = array_binding == 12 && array_length == 4 &&
          (binding == 12 || binding == 6) && selected > 0 && selected < 3;
      const unsigned donor = consecutive ? array_length - 1 : (selected + 1) % array_length;
      const VkDescriptorImageInfo donors[]{image, image};
      write.dstArrayElement = donor;
      write.descriptorCount = consecutive ? 2 : 1;
      write.pImageInfo = donors;
      vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);
      ++element_writes;
      VkCopyDescriptorSet copy{VK_STRUCTURE_TYPE_COPY_DESCRIPTOR_SET};
      copy.srcSet = copy.dstSet = set;
      copy.srcBinding = copy.dstBinding = binding;
      copy.srcArrayElement = donor;
      copy.dstArrayElement = consecutive ? selected - 1 : selected;
      copy.descriptorCount = consecutive ? 2 : 1;
      vkUpdateDescriptorSets(device, 0, nullptr, 1, &copy);
      if (consecutive) ++consecutive_copies;
      ++element_copies;
    }
  }

  void sampler_descriptor(VkSampler sampler, unsigned binding) {
    VkDescriptorImageInfo image{sampler, VK_NULL_HANDLE, VK_IMAGE_LAYOUT_UNDEFINED};
    bool &initialized = binding == 6 ? sampler_a_initialized : sampler_b_initialized;
    image_descriptor(sampler_set, VK_DESCRIPTOR_TYPE_SAMPLER, binding, image,
                     initialized, array_length - 1 - array_index);
  }

  void descriptors(Texture sampled, VkSampler sampler, Texture carrier, Buffer uv, Buffer direct, Buffer output_address,
                   std::uint64_t request_count, unsigned input_stride = 8, unsigned output_stride = 32) {
    VkWriteDescriptorSetAccelerationStructureKHR acceleration{
        VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET_ACCELERATION_STRUCTURE_KHR};
    acceleration.accelerationStructureCount = 1;
    acceleration.pAccelerationStructures = &tlas;
    const VkDescriptorImageInfo images[]{{VK_NULL_HANDLE, carrier.view, VK_IMAGE_LAYOUT_GENERAL},
                                         {separate ? VK_NULL_HANDLE : sampler, sampled.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL}};
    const VkDescriptorBufferInfo data[]{{uv.handle, 0, request_count * input_stride},
                                      {direct.handle, 0, request_count * output_stride},
                                      {output_address.handle, 0, 8}};
    const VkDescriptorType types[]{VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR,
                                    VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                                    separate ? VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE : VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
                                    VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                                    VK_DESCRIPTOR_TYPE_STORAGE_BUFFER};
    VkWriteDescriptorSet writes[6]{};
    for (unsigned i = 0; i < 6; ++i) {
      writes[i].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
      writes[i].dstSet = descriptor_set;
      writes[i].dstBinding = i;
      writes[i].descriptorCount = 1;
      writes[i].descriptorType = types[i];
    }
    writes[0].pNext = &acceleration;
    writes[1].pImageInfo = &images[0];
    writes[2].pImageInfo = &images[1];
    writes[3].pBufferInfo = &data[0];
    writes[4].pBufferInfo = &data[1];
    writes[5].pBufferInfo = &data[2];
    // The image binding is updated independently; the adjacent SSBO at b3
    // retains its own descriptor and checks the array-address collision case.
    vkUpdateDescriptorSets(device, 2, writes, 0, nullptr);
    vkUpdateDescriptorSets(device, 3, writes + 3, 0, nullptr);
    image_descriptor(descriptor_set, types[2], array_binding, images[1], image_a_initialized, array_index);
    if (separate && !samplerless) sampler_descriptor(sampler, 6);
  }

  void secondary_descriptor(Texture sampled, VkSampler sampler) {
    VkDescriptorImageInfo image{separate ? VK_NULL_HANDLE : sampler, sampled.view,
                                 VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
    image_descriptor(descriptor_set, separate ? VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE :
        VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, 13, image, image_b_initialized, array_index);
    if (separate) sampler_descriptor(sampler, 7);
  }
};

VkFormat vk_format(Format format) {
  switch (format) {
  case Format::RGBA8_UNORM: return VK_FORMAT_R8G8B8A8_UNORM;
  case Format::BGRA8_UNORM: return VK_FORMAT_B8G8R8A8_UNORM;
  case Format::RGBA8_SRGB: return VK_FORMAT_R8G8B8A8_SRGB;
  case Format::BGRA8_SRGB: return VK_FORMAT_B8G8R8A8_SRGB;
  case Format::D32_SFLOAT: return VK_FORMAT_D32_SFLOAT;
  }
  throw std::runtime_error("unknown fixture format");
}

void put_depth(Image &image,std::uint64_t offset,float value) {
  std::uint32_t bits;std::memcpy(&bits,&value,4);
  for(unsigned b=0;b<4;++b) image.data[offset+b]=(bits>>(8*b))&255;
}
float get_depth(const Image &image,std::uint64_t offset) {
  std::uint32_t bits=0;for(unsigned b=0;b<4;++b) bits|=std::uint32_t(image.data[offset+b])<<(8*b);
  float value;std::memcpy(&value,&bits,4);return value;
}

Image fixture(unsigned width, unsigned height, Format format, unsigned depth = 1) {
  // Padded rows and layer planes exercise independent Vulkan source pitches.
  Level level{width, height, 16, (width + 3) * 4ULL};
  const unsigned layers=Experiment::image_layers();
  if (layers>1) level.layer_pitch=(height+2)*level.row_pitch;
  level.depth=depth;level.slice_pitch=(height+2)*level.row_pitch;
  Image image{format, std::vector<std::uint8_t>(16 + (Experiment::volume() ? depth*level.slice_pitch : layers>1 ? layers*level.layer_pitch : height*level.row_pitch),0xcd), {level}};
  image.layer_count=layers;image.volume=Experiment::volume();
  for (unsigned layer=0;layer<(image.volume ? depth : layers);++layer)
    for (unsigned y=0;y<height;++y)
      for (unsigned x=0;x<width;++x) {
        std::array<std::uint8_t,4> color{
          static_cast<std::uint8_t>((x*37+y*71+5+layer*67)%256),
          static_cast<std::uint8_t>((x*89+y*11+23+layer*101)%256),
          static_cast<std::uint8_t>((x*13+y*53+251+layer*43)%256),
          static_cast<std::uint8_t>((x*31+y*29+19+layer*79)%256)};
        if(is_depth(format)) {
          put_depth(image,level.offset+y*level.row_pitch+x*4,static_cast<float>((x*3+y*5+1)%9)/8);
          continue;
        }
        if (is_bgra(format)) std::swap(color[0],color[2]);
        std::copy(color.begin(),color.end(),image.data.begin()+level.offset+layer*(image.volume ? level.slice_pitch : level.layer_pitch)+y*level.row_pitch+x*4);
      }
  return image;
}

Image mip_fixture(unsigned width, unsigned height, unsigned levels, Format format) {
  Image image{format, {}, {}};
  image.layer_count=Experiment::image_layers();image.volume=Experiment::volume();
  unsigned depth=Experiment::volume_depth();
  for (unsigned level = 0; level < levels; ++level) {
    auto part = fixture(width, height, format, depth);
    const auto start = image.data.size();
    // Distinct mip content and poisoned padding make incorrect level selection visible.
    for (unsigned layer=0;layer<(image.volume ? depth : image.layer_count);++layer)
    for (unsigned y = 0; y < height; ++y)
      for (unsigned x = 0; x < width; ++x)
        for (unsigned c = 0; c < 4; ++c) {
          auto &byte = part.data[part.levels[0].offset + layer*(image.volume ? part.levels[0].slice_pitch : part.levels[0].layer_pitch) + y * part.levels[0].row_pitch + x * 4 + c];
          if(is_depth(format)) {
            if(c==0) put_depth(part,part.levels[0].offset+y*part.levels[0].row_pitch+x*4,
                static_cast<float>((x*3+y*5+1+level*2)%9)/8);
          } else byte = static_cast<std::uint8_t>(byte + level * (47 + c * 19));
        }
    image.data.insert(image.data.end(), part.data.begin(), part.data.end());
    auto mip = part.levels[0];
    mip.offset += start;
    image.levels.push_back(mip);
    width = std::max(1U, width / 2); height = std::max(1U, height / 2);depth=std::max(1U,depth/2);
  }
  validate(image);
  return image;
}

std::vector<std::array<float, 2>> requests() {
  std::vector<std::array<float, 2>> result{
      {0, 0}, {1, 1}, {-1, -1}, {2, 2}, {0.5f, 0.5f}, {0.25f, 0.25f},
      {0.75f, 0.25f}, {0.25f, 0.75f}, {0.75f, 0.75f}, {-0.125f, 1.125f},
      {1.875f, -1.125f}, {0, 0.5f}, {1, 0.5f}, {0.5f, 0}, {0.5f, 1},
      {-0.0f, 0.5f}, {65536.125f, -65536.375f}, {-65536.125f, 65536.375f}};
  // Dyadic UVs avoid ambiguous nearest boundaries caused by sub-texel
  // precision differences between the double reference and float GPU path.
  while (result.size() < 64) {
    const unsigned i = result.size();
    result.push_back({(static_cast<int>(i * 37 % 385) - 192) / 64.0f,
                      (static_cast<int>(i * 83 % 385) - 192) / 64.0f});
  }
  return result;
}

void loaded_libraries() {
  std::ifstream maps("/proc/self/maps");
  std::set<std::string> paths;
  for (std::string line; std::getline(maps, line); ) {
    if (line.find("libvulkan_ventus.so") == std::string::npos &&
        line.find("libspike_driver.so") == std::string::npos &&
        line.find("libspike_main.so") == std::string::npos) continue;
    const auto start = line.find('/');
    if (start != std::string::npos) paths.insert(line.substr(start));
  }
  for (const auto &path : paths) std::cout << "LIBRARY " << path << '\n';
}

void sampler_checks(Experiment &experiment, unsigned &count) {
  VkSamplerCreateInfo base{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
  base.magFilter = base.minFilter = VK_FILTER_NEAREST;
  base.addressModeU = base.addressModeV = VK_SAMPLER_ADDRESS_MODE_REPEAT;
  base.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
  // Inactive fields and mipmapMode must not reject the valid LOD0 defaults.
  base.mipmapMode = VK_SAMPLER_MIPMAP_MODE_LINEAR;
  base.compareOp = VK_COMPARE_OP_GREATER;
  base.maxAnisotropy = 16;
  auto verify = [&](const char *label, VkSamplerCreateInfo info, bool accepted) {
    VkSampler sampler = VK_NULL_HANDLE;
    const auto result = vkCreateSampler(experiment.device, &info, nullptr, &sampler);
    if (result == VK_SUCCESS) vkDestroySampler(experiment.device, sampler, nullptr);
    if (result != (accepted ? VK_SUCCESS : VK_ERROR_FEATURE_NOT_PRESENT))
      throw std::runtime_error(std::string("sampler check failed: ") + label);
    ++count;
    std::cout << "CHECK sampler=" << label << " result=" << result << std::endl;
  };
  verify("inactive-defaults", base, true);
  auto changed = base; changed.minFilter = VK_FILTER_CUBIC_EXT; verify("unsupported-min-filter", changed, false);
  changed = base; changed.magFilter = changed.minFilter = VK_FILTER_CUBIC_EXT; verify("cubic", changed, false);
  changed = base; changed.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER; changed.borderColor=VK_BORDER_COLOR_INT_OPAQUE_BLACK; verify("integer-border-u", changed, false);
  changed = base; changed.addressModeV = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER; changed.borderColor=static_cast<VkBorderColor>(99); verify("invalid-border-v", changed, false);
  changed = base; changed.addressModeV = VK_SAMPLER_ADDRESS_MODE_MIRROR_CLAMP_TO_EDGE; verify("mirror-clamp-v", changed, false);
  changed = base; changed.mipLodBias = 0.5f; verify("bias", changed, true);
  changed = base; changed.mipLodBias = -0.5f; verify("negative-bias",changed,true);
  VkPhysicalDeviceProperties properties{};
  vkGetPhysicalDeviceProperties(experiment.physical,&properties);
  if (properties.limits.maxSamplerLodBias!=Sampling::max_lod_bias) throw std::runtime_error("sampler bias limit mismatch");
  for (float bias : {-16.f,16.f}) { changed=base; changed.mipLodBias=bias; verify("bias-limit",changed,true); }
  for (float bias : {-16.01f,16.01f,std::numeric_limits<float>::infinity()}) { changed=base; changed.mipLodBias=bias; verify("bad-bias",changed,false); }
  changed = base; changed.mipLodBias = std::numeric_limits<float>::quiet_NaN(); verify("nan-bias", changed, false);
  changed = base; changed.compareEnable = VK_TRUE; verify("comparison", changed, true);
  changed.compareOp=static_cast<VkCompareOp>(99); verify("invalid-comparison-op",changed,false);
  changed = base; changed.anisotropyEnable = VK_TRUE; verify("anisotropy", changed, false);
  changed = base; changed.unnormalizedCoordinates = VK_TRUE; verify("unnormalized", changed, false);
  changed = base; changed.minLod = -1; verify("negative-min-lod", changed, false);
  changed = base; changed.minLod = 2; changed.maxLod = 1; verify("reversed-lod", changed, false);
  changed = base; changed.maxLod = std::numeric_limits<float>::infinity(); verify("infinite-lod", changed, false);
  changed=base;changed.addressModeW=VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT;verify("mirror-w",changed,true);
  changed=base;changed.addressModeW=VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER;changed.borderColor=VK_BORDER_COLOR_FLOAT_OPAQUE_WHITE;verify("border-w",changed,true);
  changed.borderColor=VK_BORDER_COLOR_INT_OPAQUE_WHITE;verify("integer-border-w",changed,false);
  changed=base;changed.addressModeW=VK_SAMPLER_ADDRESS_MODE_MIRROR_CLAMP_TO_EDGE;verify("mirror-clamp-w",changed,false);
}

void image_view_checks(Experiment &experiment, unsigned &images, unsigned &views) {
  VkImageFormatProperties properties{};
  check(vkGetPhysicalDeviceImageFormatProperties(experiment.physical, VK_FORMAT_R8G8B8A8_UNORM,
      VK_IMAGE_TYPE_2D, VK_IMAGE_TILING_OPTIMAL, VK_IMAGE_USAGE_SAMPLED_BIT, 0, &properties), "query mip limits");
  if (properties.maxMipLevels != 15 || properties.maxArrayLayers != 2048)
    throw std::runtime_error("inconsistent advertised 2D mip limits");
  VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
  info.imageType = VK_IMAGE_TYPE_2D; info.format = VK_FORMAT_R8G8B8A8_UNORM;
  info.extent = {8,4,1}; info.mipLevels = 4; info.arrayLayers = 1;
  info.samples = VK_SAMPLE_COUNT_1_BIT; info.tiling = VK_IMAGE_TILING_OPTIMAL;
  info.usage = VK_IMAGE_USAGE_SAMPLED_BIT;
  auto image_check = [&](const char *name, VkImageCreateInfo value, bool supported) {
    VkImage image = VK_NULL_HANDLE;
    const auto result = vkCreateImage(experiment.device, &value, nullptr, &image);
    if (image) vkDestroyImage(experiment.device, image, nullptr);
    if (result != (supported ? VK_SUCCESS : VK_ERROR_FEATURE_NOT_PRESENT))
      throw std::runtime_error(std::string("image profile check failed: ") + name);
    ++images;
    std::cout << "CHECK image=" << name << " result=" << result << std::endl;
  };
  image_check("complete-mips", info, true);
  auto changed = info; changed.mipLevels = 2; image_check("partial-mips", changed, true);
  changed = info; changed.mipLevels = 5; image_check("too-many-mips", changed, false);
  changed = info; changed.arrayLayers = 2049; image_check("too-many-array-layers", changed, false);
  changed = info; changed.imageType = VK_IMAGE_TYPE_1D; image_check("1d", changed, false);
  changed = info; changed.imageType = VK_IMAGE_TYPE_3D; changed.extent.depth = 2; image_check("3d", changed, true);
  changed = info; changed.samples = VK_SAMPLE_COUNT_4_BIT; image_check("multisample", changed, false);
  changed = info; changed.flags = VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT; image_check("cube", changed, false);
  for (unsigned count : {1U,2U,4U}) {
    changed=info; changed.arrayLayers=count;
    image_check(("supported-layers-"+std::to_string(count)).c_str(),changed,true);
  }
  changed=info;changed.arrayLayers=2;changed.usage|=VK_IMAGE_USAGE_STORAGE_BIT;
  image_check("storage-array",changed,false);
  changed=info;changed.arrayLayers=2;changed.usage|=VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
  image_check("attachment-array",changed,false);
  changed=info;changed.extent={8,8,1};changed.flags=VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT;changed.arrayLayers=6;
  image_check("cube-six-faces",changed,true);
  changed.arrayLayers=12;image_check("cube-twelve-planes",changed,true);
  changed.arrayLayers=5;image_check("cube-fewer-than-six",changed,false);
  changed.arrayLayers=6;changed.usage|=VK_IMAGE_USAGE_STORAGE_BIT;image_check("cube-storage",changed,false);
  changed.usage=info.usage|VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;image_check("cube-attachment",changed,false);
  auto volume=info;volume.imageType=VK_IMAGE_TYPE_3D;volume.extent={1,1,8};volume.mipLevels=4;
  image_check("volume-depth-mips",volume,true);
  changed=volume;changed.extent.depth=0;image_check("volume-zero-depth",changed,false);
  changed=volume;changed.extent.depth=2049;image_check("volume-depth-limit",changed,false);
  changed=volume;changed.arrayLayers=2;image_check("volume-array-layer",changed,false);
  changed=volume;changed.usage|=VK_IMAGE_USAGE_STORAGE_BIT;image_check("volume-storage",changed,false);
  changed=volume;changed.usage|=VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;image_check("volume-attachment",changed,false);
  changed=volume;changed.flags=VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT;image_check("volume-cube-flag",changed,false);
  changed=volume;changed.format=VK_FORMAT_D32_SFLOAT;image_check("volume-depth-format",changed,false);
  changed=volume;changed.extent.width=2049;image_check("volume-width-limit",changed,false);
  auto texture = experiment.texture(8,4,info.format,info.usage,4,0,4);
  VkImageViewCreateInfo view{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
  view.image = texture.handle; view.viewType = VK_IMAGE_VIEW_TYPE_2D; view.format = info.format;
  view.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT,1,VK_REMAINING_MIP_LEVELS,0,VK_REMAINING_ARRAY_LAYERS};
  auto view_check = [&](const char *name, VkImageViewCreateInfo value, bool supported) {
    VkImageView handle = VK_NULL_HANDLE;
    const auto result = vkCreateImageView(experiment.device, &value, nullptr, &handle);
    if (handle) vkDestroyImageView(experiment.device, handle, nullptr);
    if (result != (supported ? VK_SUCCESS : VK_ERROR_FEATURE_NOT_PRESENT))
      throw std::runtime_error(std::string("view profile check failed: ") + name);
    ++views;
    std::cout << "CHECK view=" << name << " result=" << result << std::endl;
  };
  view_check("remaining-levels-layers", view, true);
  auto other = view; other.subresourceRange.levelCount = 1; view_check("single-level", other, true);
  other = view; other.components = {VK_COMPONENT_SWIZZLE_R,VK_COMPONENT_SWIZZLE_G,VK_COMPONENT_SWIZZLE_B,VK_COMPONENT_SWIZZLE_A}; view_check("explicit-identity", other, true);
  other = view; other.subresourceRange.baseMipLevel = 4; view_check("base-outside-image", other, false);
  other = view; other.subresourceRange.levelCount = 4; view_check("range-outside-image", other, false);
  other = view; other.subresourceRange.baseArrayLayer = 1; view_check("layer", other, false);
  other = view; other.viewType = VK_IMAGE_VIEW_TYPE_3D; view_check("3d-view", other, false);
  other = view; other.format = VK_FORMAT_B8G8R8A8_UNORM; view_check("format-reinterpretation", other, false);
  other = view; other.components.r = static_cast<VkComponentSwizzle>(99); view_check("invalid-swizzle", other, false);
  other = view; other.subresourceRange.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT; view_check("depth-aspect", other, false);
  VkImage array=VK_NULL_HANDLE;
  changed=info;changed.arrayLayers=4;
  check(vkCreateImage(experiment.device,&changed,nullptr,&array),"create array range fixture");
  view.image=array;view.viewType=VK_IMAGE_VIEW_TYPE_2D_ARRAY;
  view.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,1,2,1,3};
  view_check("array-layer-subrange",view,true);
  other=view;other.subresourceRange.layerCount=VK_REMAINING_ARRAY_LAYERS;
  view_check("array-remaining-layers",other,true);
  other=view;other.subresourceRange.baseArrayLayer=0;other.subresourceRange.layerCount=4;
  view_check("array-full-range",other,true);
  other=view;other.viewType=VK_IMAGE_VIEW_TYPE_2D;other.subresourceRange.baseArrayLayer=2;other.subresourceRange.layerCount=1;
  view_check("2d-nonzero-layer",other,true);
  other=view;other.subresourceRange.layerCount=0;view_check("zero-layer-count",other,false);
  other=view;other.subresourceRange.layerCount=4;view_check("layer-range-outside-image",other,false);
  other=view;other.viewType=VK_IMAGE_VIEW_TYPE_2D;view_check("2d-multiple-layers",other,false);
  vkDestroyImage(experiment.device,array,nullptr);
  VkImage cube=VK_NULL_HANDLE,unflagged=VK_NULL_HANDLE;
  changed=info;changed.extent={8,8,1};changed.arrayLayers=12;
  check(vkCreateImage(experiment.device,&changed,nullptr,&unflagged),"create unflagged square array");
  view.image=unflagged;view.viewType=VK_IMAGE_VIEW_TYPE_CUBE;
  view.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,4,0,6};
  view_check("cube-view-missing-create-flag",view,false);
  vkDestroyImage(experiment.device,unflagged,nullptr);
  changed.flags=VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT;
  check(vkCreateImage(experiment.device,&changed,nullptr,&cube),"create cube range fixture");
  view.image=cube;view_check("cube-first-six",view,true);
  other=view;other.subresourceRange.baseArrayLayer=6;view_check("cube-second-six",other,true);
  other=view;other.subresourceRange.baseArrayLayer=1;view_check("cube-offset-six",other,true);
  other=view;other.subresourceRange.baseArrayLayer=6;other.subresourceRange.layerCount=VK_REMAINING_ARRAY_LAYERS;
  view_check("cube-remaining-six",other,true);
  other=view;other.subresourceRange.layerCount=5;view_check("cube-wrong-face-count",other,false);
  other=view;other.subresourceRange.layerCount=VK_REMAINING_ARRAY_LAYERS;view_check("cube-remaining-twelve",other,false);
  other=view;other.viewType=VK_IMAGE_VIEW_TYPE_CUBE_ARRAY;other.subresourceRange.layerCount=12;view_check("cube-array-view",other,false);
  other=view;other.subresourceRange.baseArrayLayer=7;view_check("cube-range-outside-image",other,false);
  vkDestroyImage(experiment.device,cube,nullptr);
  VkImage volume_image=VK_NULL_HANDLE;check(vkCreateImage(experiment.device,&volume,nullptr,&volume_image),"create volume view fixture");
  view.image=volume_image;view.viewType=VK_IMAGE_VIEW_TYPE_3D;view.subresourceRange={VK_IMAGE_ASPECT_COLOR_BIT,0,4,0,1};
  view_check("volume-full-view",view,true);
  other=view;other.subresourceRange.baseMipLevel=1;other.subresourceRange.levelCount=2;view_check("volume-mip-subrange",other,true);
  other=view;other.subresourceRange.baseMipLevel=2;other.subresourceRange.levelCount=VK_REMAINING_MIP_LEVELS;view_check("volume-remaining-mips",other,true);
  other=view;other.viewType=VK_IMAGE_VIEW_TYPE_2D;view_check("volume-2d-view",other,false);
  other=view;other.viewType=VK_IMAGE_VIEW_TYPE_2D_ARRAY;view_check("volume-array-view",other,false);
  other=view;other.subresourceRange.baseArrayLayer=1;view_check("volume-view-layer",other,false);
  other=view;other.subresourceRange.baseMipLevel=4;view_check("volume-view-mip-outside",other,false);
  vkDestroyImage(experiment.device,volume_image,nullptr);
  auto depth_info=info;depth_info.format=VK_FORMAT_D32_SFLOAT;depth_info.usage|=VK_IMAGE_USAGE_TRANSFER_DST_BIT;
  image_check("depth-mips",depth_info,true);
  changed=depth_info;changed.tiling=VK_IMAGE_TILING_LINEAR;image_check("depth-linear",changed,true);
  changed=depth_info;changed.arrayLayers=2;image_check("depth-array",changed,false);
  changed=depth_info;changed.usage|=VK_IMAGE_USAGE_STORAGE_BIT;image_check("depth-storage",changed,false);
  changed=depth_info;changed.usage|=VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;image_check("depth-color-attachment",changed,false);
  changed=depth_info;changed.tiling=VK_IMAGE_TILING_LINEAR;changed.usage|=VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;image_check("depth-linear-attachment",changed,false);
  changed=depth_info;changed.mipLevels=5;image_check("depth-too-many-mips",changed,false);
  changed=depth_info;changed.extent={8,8,1};changed.arrayLayers=6;changed.flags=VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT;image_check("depth-cube",changed,false);
  VkImage depth_image{};check(vkCreateImage(experiment.device,&depth_info,nullptr,&depth_image),"create depth views fixture");
  view.image=depth_image;view.viewType=VK_IMAGE_VIEW_TYPE_2D;view.format=depth_info.format;
  view.subresourceRange={VK_IMAGE_ASPECT_DEPTH_BIT,0,4,0,1};view_check("depth-full",view,true);
  other=view;other.subresourceRange.baseMipLevel=1;other.subresourceRange.levelCount=VK_REMAINING_MIP_LEVELS;view_check("depth-remaining-mips",other,true);
  other=view;other.components={VK_COMPONENT_SWIZZLE_R,VK_COMPONENT_SWIZZLE_G,VK_COMPONENT_SWIZZLE_B,VK_COMPONENT_SWIZZLE_A};view_check("depth-explicit-identity",other,true);
  other=view;other.components.r=VK_COMPONENT_SWIZZLE_B;view_check("depth-swizzle",other,false);
  other=view;other.subresourceRange.aspectMask=VK_IMAGE_ASPECT_COLOR_BIT;view_check("depth-color-aspect",other,false);
  other=view;other.subresourceRange.aspectMask=VK_IMAGE_ASPECT_DEPTH_BIT|VK_IMAGE_ASPECT_STENCIL_BIT;view_check("depth-stencil-aspect",other,false);
  other=view;other.viewType=VK_IMAGE_VIEW_TYPE_2D_ARRAY;view_check("depth-array-view",other,false);
  other=view;other.subresourceRange.baseMipLevel=4;view_check("depth-outside-mip",other,false);
  vkDestroyImage(experiment.device,depth_image,nullptr);



}

unsigned volume_copy_checks(Experiment &experiment) {
  VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
  info.imageType=VK_IMAGE_TYPE_3D;info.extent={2,2,2};info.mipLevels=1;info.arrayLayers=1;
  info.format=VK_FORMAT_R8G8B8A8_UNORM;info.samples=VK_SAMPLE_COUNT_1_BIT;info.tiling=VK_IMAGE_TILING_LINEAR;
  info.usage=VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT;
  VkImage image{};check(vkCreateImage(experiment.device,&info,nullptr,&image),"create volume copy fixture");
  VkMemoryRequirements requirements{};vkGetImageMemoryRequirements(experiment.device,image,&requirements);
  const auto memory=experiment.allocate(requirements,false);check(vkBindImageMemory(experiment.device,image,memory,0),"bind volume copy fixture");
  void *mapped{};check(vkMapMemory(experiment.device,memory,0,VK_WHOLE_SIZE,0,&mapped),"map volume copy fixture");
  auto source=experiment.buffer(64,VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
  const std::array<uint8_t,4> pixel{13,29,47,61};std::memcpy(source.map,pixel.data(),4);
  VkBufferImageCopy valid{};valid.imageSubresource={VK_IMAGE_ASPECT_COLOR_BIT,0,0,1};valid.imageOffset={1,1,1};valid.imageExtent={1,1,1};
  VkImageSubresource subresource{VK_IMAGE_ASPECT_COLOR_BIT,0,0};VkSubresourceLayout layout{};vkGetImageSubresourceLayout(experiment.device,image,&subresource,&layout);
  const auto target=layout.offset+layout.depthPitch+layout.rowPitch+4;
  if(target+4>requirements.size)throw std::runtime_error("volume queried layout outside allocation");
  for(unsigned trial=0;trial<6;++trial) {
    std::memset(mapped,0xcd,requirements.size);auto invalid=valid;
    switch(trial) {
    case 0:invalid.imageOffset.z=2;break;
    case 1:invalid.imageExtent.depth=2;break;
    case 2:invalid.bufferOffset=60;invalid.imageOffset={0,0,0};invalid.imageExtent={2,2,2};break;
    case 3:invalid.imageSubresource.layerCount=2;break;
    case 4:invalid.imageOffset.z=-1;break;
    default:break;
    }
    const VkBufferImageCopy regions[]{valid,invalid};experiment.begin();
    experiment.transition(Texture{image,VK_NULL_HANDLE,memory},VK_IMAGE_LAYOUT_UNDEFINED,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,VK_PIPELINE_STAGE_TRANSFER_BIT,0,VK_ACCESS_TRANSFER_WRITE_BIT);
    vkCmdCopyBufferToImage(experiment.command,source.handle,image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,trial==5 ? 1 : 2,regions);
    check(vkEndCommandBuffer(experiment.command),"end volume copy check");VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};submit.commandBufferCount=1;submit.pCommandBuffers=&experiment.command;
    const auto result=vkQueueSubmit(experiment.queue,1,&submit,VK_NULL_HANDLE);
    if(result!=(trial==5 ? VK_SUCCESS : VK_ERROR_FEATURE_NOT_PRESENT))throw std::runtime_error("volume copy validation result mismatch");
    check(vkQueueWaitIdle(experiment.queue),"wait volume copy check");
    for(uint64_t i=0;i<requirements.size;++i) {
      const auto expected=trial==5 && i>=target && i<target+4 ? pixel[i-target] : 0xcd;
      if(static_cast<const uint8_t *>(mapped)[i]!=expected)throw std::runtime_error("volume copy validation changed unexpected bytes");
    }
    std::cout<<"CHECK volume-copy="<<trial<<" result="<<result<<" bytes_verified=1"<<std::endl;
  }
  vkUnmapMemory(experiment.device,memory);vkDestroyImage(experiment.device,image,nullptr);vkFreeMemory(experiment.device,memory,nullptr);
  return 6;
}

unsigned depth_copy_checks(Experiment &experiment) {
  VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};info.imageType=VK_IMAGE_TYPE_2D;
  info.format=VK_FORMAT_D32_SFLOAT;info.extent={2,1,1};info.mipLevels=info.arrayLayers=1;
  info.samples=VK_SAMPLE_COUNT_1_BIT;info.tiling=VK_IMAGE_TILING_LINEAR;
  info.usage=VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT;
  VkImage image{};check(vkCreateImage(experiment.device,&info,nullptr,&image),"depth copy fixture");
  VkMemoryRequirements requirements{};vkGetImageMemoryRequirements(experiment.device,image,&requirements);
  auto memory=experiment.allocate(requirements,false);check(vkBindImageMemory(experiment.device,image,memory,0),"bind depth copy");
  void *mapped{};check(vkMapMemory(experiment.device,memory,0,VK_WHOLE_SIZE,0,&mapped),"map depth copy");
  auto source=experiment.buffer(8,VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
  const std::array<float,2> depths{.375f,.875f};std::memcpy(source.map,depths.data(),8);
  const VkImageSubresource subresource{VK_IMAGE_ASPECT_DEPTH_BIT,0,0};VkSubresourceLayout layout{};
  vkGetImageSubresourceLayout(experiment.device,image,&subresource,&layout);
  for(unsigned trial=0;trial<3;++trial) {
    std::memset(mapped,0xcd,requirements.size);
    VkBufferImageCopy regions[2]{};regions[0].imageSubresource={VK_IMAGE_ASPECT_DEPTH_BIT,0,0,1};regions[0].imageExtent={1,1,1};
    regions[1]=regions[0];regions[1].bufferOffset=4;regions[1].imageOffset.x=1;
    if(trial==0) regions[1].imageSubresource.aspectMask=VK_IMAGE_ASPECT_COLOR_BIT;
    if(trial==1) regions[1].bufferOffset=8;
    experiment.begin();vkCmdCopyBufferToImage(experiment.command,source.handle,image,VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,2,regions);
    check(vkEndCommandBuffer(experiment.command),"end depth copy check");
    VkSubmitInfo submit{VK_STRUCTURE_TYPE_SUBMIT_INFO};submit.commandBufferCount=1;submit.pCommandBuffers=&experiment.command;
    const auto result=vkQueueSubmit(experiment.queue,1,&submit,VK_NULL_HANDLE);
    if(result!=(trial==2 ? VK_SUCCESS : VK_ERROR_FEATURE_NOT_PRESENT))throw std::runtime_error("depth copy result mismatch");
    check(vkQueueWaitIdle(experiment.queue),"wait depth copy check");
    for(std::uint64_t i=0;i<requirements.size;++i) {
      const auto expected=trial==2 && i>=layout.offset && i<layout.offset+8 ? static_cast<std::uint8_t *>(source.map)[i-layout.offset] : 0xcd;
      if(static_cast<std::uint8_t *>(mapped)[i]!=expected)throw std::runtime_error("depth copy modified unexpected bytes");
    }
    std::cout<<"CHECK depth-copy="<<trial<<" result="<<result<<" all_regions_before_write=1"<<std::endl;
  }
  vkUnmapMemory(experiment.device,memory);vkDestroyImage(experiment.device,image,nullptr);vkFreeMemory(experiment.device,memory,nullptr);
  return 3;
}

int profile_checks(const char *rgen, const char *rmiss, int bad_count, char **bad_shaders) {
  unsigned samplers = 0, shaders = 0, images = 0, views = 0;
  Experiment baseline;
  baseline.initialize();
  VkPhysicalDeviceProperties properties{};
  vkGetPhysicalDeviceProperties(baseline.physical, &properties);
  if (properties.limits.minTexelOffset != min_texel_offset ||
      properties.limits.maxTexelOffset != max_texel_offset ||
      properties.limits.maxImageDimensionCube != 16384 || properties.limits.maxImageDimension3D != 2048)
    throw std::runtime_error("advertised texel offset limits disagree with software profile");
  VkImageFormatProperties volume_properties{};
  check(vkGetPhysicalDeviceImageFormatProperties(baseline.physical,VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_TYPE_3D,VK_IMAGE_TILING_LINEAR,VK_IMAGE_USAGE_SAMPLED_BIT,0,&volume_properties),"query 3D limits");
  if(volume_properties.maxExtent.width!=2048 || volume_properties.maxExtent.height!=2048 || volume_properties.maxExtent.depth!=2048 || volume_properties.maxMipLevels!=12 || volume_properties.maxArrayLayers!=1 ||
     vkGetPhysicalDeviceImageFormatProperties(baseline.physical,VK_FORMAT_R8G8B8A8_UNORM,VK_IMAGE_TYPE_3D,VK_IMAGE_TILING_OPTIMAL,VK_IMAGE_USAGE_STORAGE_BIT,0,&volume_properties)!=VK_ERROR_FORMAT_NOT_SUPPORTED)
    throw std::runtime_error("3D image format profile mismatch");
  sampler_checks(baseline, samplers);
  image_view_checks(baseline, images, views);
  const unsigned copies=volume_copy_checks(baseline)+depth_copy_checks(baseline);
  baseline.setup_pipeline(rgen, rmiss);
  for (int i = 0; i < bad_count; ++i) {
    Experiment experiment;
    experiment.initialize();
    bool rejected = false;
    try { experiment.setup_pipeline(bad_shaders[i], rmiss); }
    catch (const std::runtime_error &error) {
      const std::string message = error.what();
      if (message != "create RT pipeline: VkResult=-8" &&
          message != "create RT pipeline: VkResult=-3") throw;
      std::cout << "CHECK compiler-error=" << message << std::endl;
      rejected = true;
    }
    if (!rejected) throw std::runtime_error(std::string("unsupported shader accepted: ") + bad_shaders[i]);
    ++shaders;
    std::cout << "CHECK shader=" << bad_shaders[i] << " result=rejected" << std::endl;
  }
  loaded_libraries();
  std::cout << "CHECKS samplers=" << samplers << " shaders=" << shaders << " images=" << images << " views=" << views << " copies=" << copies << " limits=5 baseline=pass errors=0" << std::endl;
  return 0;
}

unsigned srgb_format_checks(Experiment &experiment) {
  unsigned checks = 0;
  for (auto format : {VK_FORMAT_R8G8B8A8_UNORM,VK_FORMAT_B8G8R8A8_UNORM,
                     VK_FORMAT_R8G8B8A8_SRGB,VK_FORMAT_B8G8R8A8_SRGB}) {
    const bool srgb = format == VK_FORMAT_R8G8B8A8_SRGB || format == VK_FORMAT_B8G8R8A8_SRGB;
    VkFormatProperties3 extended{VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_3};
    VkFormatProperties2 properties{VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2,&extended};
    vkGetPhysicalDeviceFormatProperties2(experiment.physical,format,&properties);
    const auto expected = VK_FORMAT_FEATURE_2_SAMPLED_IMAGE_BIT |
        VK_FORMAT_FEATURE_2_SAMPLED_IMAGE_FILTER_LINEAR_BIT |
        VK_FORMAT_FEATURE_2_TRANSFER_SRC_BIT | VK_FORMAT_FEATURE_2_TRANSFER_DST_BIT;
    if ((extended.linearTilingFeatures & expected) != expected ||
        (extended.optimalTilingFeatures & expected) != expected ||
        (properties.formatProperties.linearTilingFeatures & expected) != expected ||
        (properties.formatProperties.optimalTilingFeatures & expected) != expected ||
        (srgb && ((extended.linearTilingFeatures | extended.optimalTilingFeatures) & VK_FORMAT_FEATURE_2_STORAGE_IMAGE_BIT)))
      throw std::runtime_error("sampled format feature query inconsistent");
    ++checks;
    for (auto tiling : {VK_IMAGE_TILING_LINEAR,VK_IMAGE_TILING_OPTIMAL}) {
      VkImageFormatProperties limits{};
      check(vkGetPhysicalDeviceImageFormatProperties(experiment.physical,format,VK_IMAGE_TYPE_2D,
          tiling,VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,0,&limits),"query sampled color format");
      if (limits.maxMipLevels != 15 || limits.maxArrayLayers != 2048 || limits.sampleCounts != VK_SAMPLE_COUNT_1_BIT)
        throw std::runtime_error("sampled format limits inconsistent");
      ++checks;
      if (srgb) {
        if (vkGetPhysicalDeviceImageFormatProperties(experiment.physical,format,VK_IMAGE_TYPE_2D,
            tiling,VK_IMAGE_USAGE_STORAGE_BIT,0,&limits) != VK_ERROR_FORMAT_NOT_SUPPORTED)
          throw std::runtime_error("sRGB storage query silently accepted");
        ++checks;
      }
    }
    if (srgb) {
      VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
      info.imageType=VK_IMAGE_TYPE_2D;info.format=format;info.extent={2,1,1};
      info.mipLevels=1;info.arrayLayers=1;info.samples=VK_SAMPLE_COUNT_1_BIT;
      info.tiling=VK_IMAGE_TILING_OPTIMAL;info.usage=VK_IMAGE_USAGE_STORAGE_BIT;
      VkImage image=VK_NULL_HANDLE;
      const auto result=vkCreateImage(experiment.device,&info,nullptr,&image);
      if (image) vkDestroyImage(experiment.device,image,nullptr);
      if (result != VK_ERROR_FEATURE_NOT_PRESENT) throw std::runtime_error("sRGB storage image silently accepted");
      ++checks;
    }
  }
  std::cout << "CHECK formats=" << checks << " errors=0" << std::endl;
  return checks;
}

unsigned depth_format_checks(Experiment &experiment) {
  VkFormatProperties3 p{VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_3};
  VkFormatProperties2 props2{VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2};props2.pNext=&p;
  vkGetPhysicalDeviceFormatProperties2(experiment.physical,VK_FORMAT_D32_SFLOAT,&props2);
  const VkFormatFeatureFlags2 required=VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT|VK_FORMAT_FEATURE_SAMPLED_IMAGE_FILTER_LINEAR_BIT|
      VK_FORMAT_FEATURE_2_SAMPLED_IMAGE_DEPTH_COMPARISON_BIT|VK_FORMAT_FEATURE_TRANSFER_SRC_BIT|VK_FORMAT_FEATURE_TRANSFER_DST_BIT;
  if((p.linearTilingFeatures&required)!=required || (p.optimalTilingFeatures&required)!=required)
    throw std::runtime_error("D32 format features incomplete");
  unsigned count=2;
  for(auto tiling:{VK_IMAGE_TILING_LINEAR,VK_IMAGE_TILING_OPTIMAL}) {
    VkImageFormatProperties props{};
    check(vkGetPhysicalDeviceImageFormatProperties(experiment.physical,VK_FORMAT_D32_SFLOAT,VK_IMAGE_TYPE_2D,
        tiling,VK_IMAGE_USAGE_SAMPLED_BIT|VK_IMAGE_USAGE_TRANSFER_DST_BIT,0,&props),"depth sampled caps");
    if(props.maxMipLevels!=15 || props.maxArrayLayers!=1) throw std::runtime_error("D32 image limits differ");
    ++count;
    for(auto usage:{VK_IMAGE_USAGE_STORAGE_BIT,VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT}) {
      const auto result=vkGetPhysicalDeviceImageFormatProperties(experiment.physical,VK_FORMAT_D32_SFLOAT,VK_IMAGE_TYPE_2D,
          tiling,VK_IMAGE_USAGE_SAMPLED_BIT|usage,0,&props);
      if(result!=VK_ERROR_FORMAT_NOT_SUPPORTED) throw std::runtime_error("D32 invalid usage accepted");
      ++count;
    }
  }
  return count;
}
void check_linear_layout(Experiment &,Texture,const Image &);

int run(const char *rgen, const char *rmiss, const char *csv_path, unsigned limit,
        bool inject_mismatch, bool mips = false, bool mip_linear = false, bool min_mag = false,
        bool srgb = false, bool sweep = false, bool fetch_query = false,
        bool inject_query_mismatch = false, bool view_swizzle = false, bool border_mode = false,
        bool separate = false) {
  Experiment experiment;
  experiment.initialize();
  if(Experiment::depth()) {view_swizzle=false;srgb=false;}
  const unsigned format_checks = Experiment::depth() ? depth_format_checks(experiment) : srgb || fetch_query || view_swizzle || Experiment::cube() || Experiment::volume() ? srgb_format_checks(experiment) : 0;
  experiment.empty_tlas();
  experiment.setup_pipeline(rgen, rmiss, false, separate, separate && fetch_query);
  auto uv = requests();
  if (std::getenv("VENTUS_TEXTURE_OFFSET")) uv[0] = {-std::numeric_limits<float>::denorm_min(), 0.25f};
  if (sweep) {
    uv.clear();
    for (unsigned i=0;i<256;++i) uv.push_back({(i+0.5f)/256,0.5f});
  } else if (mips) {
    uv[18] = {std::numeric_limits<float>::max(), -std::numeric_limits<float>::max()};
    uv[19] = {-8388608.f, 16777216.f};
  }
  const std::array<float, 32> nearest_lods{-100, 0, 0.49f, 0.5f, std::nextafter(0.5f, 1.f),
      0.51f, 1, 1.49f, 1.5f, std::nextafter(1.5f, 2.f), 1.51f, 2, 2.5f, 3, 10,
      std::numeric_limits<float>::max(), 4,4.5f,5,6,7,8,9,10,11,12,13,13.49f,13.5f,
      std::nextafter(13.5f,14.f),13.51f,14};
  const std::array<float, 32> linear_lods{-100, -0.f, 0.25f, 0.5f, 0.75f,
      std::nextafter(1.f,0.f), 1, std::nextafter(1.f,2.f), 1.25f, 1.5f, 1.75f,
      std::nextafter(2.f,1.f), 2, std::nextafter(2.f,3.f), 2.5f,
      std::numeric_limits<float>::max(), 3,3.5f,4,4.5f,5,6,7,8,9,10,11,12,13,
      13.25f,13.75f,14};
  const std::array<float,32> min_mag_lods{-100, -std::numeric_limits<float>::max(),
      -std::numeric_limits<float>::denorm_min(), -0.f, 0,
      std::numeric_limits<float>::denorm_min(), 0.25f, 0.49f, 0.5f,
      std::nextafter(0.5f,1.f),0.75f,std::nextafter(1.f,0.f),1,
      std::nextafter(1.f,2.f),1.5f,2,2.5f,3,4,5,6,7,8,9,10,11,12,13,
      13.25f,13.75f,14,std::numeric_limits<float>::max()};
  const auto &lods = min_mag ? min_mag_lods : mip_linear ? linear_lods : nearest_lods;
  std::vector<std::array<float, 4>> mip_inputs;
  for (unsigned i = 0; i < uv.size(); ++i)
    mip_inputs.push_back({uv[i][0], uv[i][1], Experiment::gradients() ? Experiment::gradient_request(i)[0] : sweep ? 0 : lods[i % lods.size()],
                          Experiment::gradients() ? Experiment::gradient_request(i)[1] : Experiment::array_sampling() ? Experiment::layer_request(i) : Experiment::depth_compare() ? Experiment::dref(i) : 0});
  const auto gradient_inputs=Experiment::gradient_inputs(mip_inputs);
  const unsigned input_stride=Experiment::gradients() ? 32 : mips ? 16 : 8;
  auto inputs = experiment.buffer(uv.size() * input_stride, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  std::memcpy(inputs.map, (Experiment::gradients() || Experiment::cube() || (Experiment::volume() && !fetch_query)) ? static_cast<const void *>(gradient_inputs.data()) :
      mips ? static_cast<const void *>(mip_inputs.data()) : static_cast<const void *>(uv.data()), uv.size() * input_stride);
  const unsigned output_floats=(fetch_query && Experiment::array_sampling()) || Experiment::volume() ? 12 : 8;
  auto direct = experiment.buffer(uv.size() * output_floats * 4, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  auto indirect = experiment.buffer(uv.size() * output_floats * 4, VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
  auto output_address = experiment.buffer(8, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  const auto indirect_address = experiment.address(indirect);
  std::memcpy(output_address.map, &indirect_address, sizeof(indirect_address));
  auto carrier = experiment.texture(uv.size(), 1, VK_FORMAT_R8G8B8A8_UNORM,
                                    VK_IMAGE_USAGE_STORAGE_BIT);
  experiment.begin();
  experiment.transition(carrier, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL,
                        VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
                        0, VK_ACCESS_SHADER_WRITE_BIT);
  experiment.submit();
  std::ofstream csv(csv_path);
  if (!csv) throw std::runtime_error("cannot open numeric result CSV");
  csv << "case,path,width,height,format,address_u,address_v,mag_filter,min_filter,selected_filter,sampler_lod,request,u,v,input_lod,view_base,view_count,selected_mip,mip_upper,mip_weight,mip_filter,view_mapping,border_color,sampler_bias,footprint_squared,derived_lod,gradient_v,gradient_dyu,gradient_dyv,channel,reference,gpu,abs_error,input_layer,view_base_layer,view_layer_count,selected_layer,direction_x,direction_y,direction_z,cube_face,input_w,address_w,selected_depth\n";
  csv << std::setprecision(10);
  unsigned cases = 0;
  std::uint64_t samples = 0, components = 0, mismatches = 0, markers_failed = 0, buffer_failed = 0;
  double max_error = 0;
  std::uint64_t query_components = 0, query_failed = 0;
  std::array<unsigned,9> mapping_counts{};
  std::array<unsigned,3> border_counts{};
  std::ofstream query_csv;
  const bool query_mode=fetch_query || Experiment::cube() || Experiment::volume();
  if (query_mode) {
    query_csv.open(std::string(csv_path) + ".queries.csv");
    if (!query_csv) throw std::runtime_error("cannot open query CSV");
    query_csv << "case,path,request,lod,view_base,view_count,property,expected,actual\n";
  }
  struct Config { unsigned width, height, levels, base, count; LodRange range; };
  std::vector<Config> configurations = sweep ? std::vector<Config>{{256,1,1,0,1,{0,1000}}} : mips ? std::vector<Config>{
      {5,3,3,0,3,{0,1000}}, {5,3,3,1,VK_REMAINING_MIP_LEVELS,{0,1000}},
      {8,4,4,0,4,{0.75,1.75}}, {8,4,4,1,2,{2,2}},
      {1,8,3,0,3,{0,0.5}}, {1,8,3,2,1,{0,1000}},
      {1,1,1,0,1,{2,2}}, {3,5,2,1,1,{0,1000}},
      {16384,1,15,0,15,{0,1000}}, {16384,1,15,13,VK_REMAINING_MIP_LEVELS,{0,1000}}} : std::vector<Config>{
      {1,1,1,0,1,{0,0}}, {1,5,1,0,1,{0,0}}, {5,1,1,0,1,{0,0}},
      {3,5,1,0,1,{0,0}}, {4,4,1,0,1,{0,0}}};
  if (Experiment::cube()) configurations={
    {5,5,3,0,3,{0,1000}},{5,5,3,1,VK_REMAINING_MIP_LEVELS,{0,1000}},
    {8,8,4,0,4,{.75,1.75}},{8,8,4,1,2,{2,2}},
    {2,2,2,0,2,{0,.5}},{2,2,2,1,1,{0,1000}},
    {1,1,1,0,1,{2,2}},{3,3,2,1,1,{0,1000}},
    {16,16,5,0,5,{0,1000}},{16,16,5,3,VK_REMAINING_MIP_LEVELS,{0,1000}}};
  if (Experiment::volume()) {configurations[8]={2048,1,12,0,12,{0,1000}};configurations[9]={2048,1,12,10,VK_REMAINING_MIP_LEVELS,{0,1000}};}
  if (Experiment::gradients()) configurations[0]={8,8,4,0,4,{0,1000}};
  const unsigned total_cases = (border_mode ? configurations.size()*4*7*8*3 :
      configurations.size() * (min_mag ? 144 : 36) * (fetch_query || view_swizzle ? 2 : 1)) / (Experiment::depth() ? (border_mode || fetch_query ? 4 : 2) : 1);
  const std::vector<Format> formats = Experiment::depth() ? std::vector<Format>{Format::D32_SFLOAT} : fetch_query || view_swizzle ? std::vector<Format>{Format::RGBA8_UNORM,Format::BGRA8_UNORM,Format::RGBA8_SRGB,Format::BGRA8_SRGB}
      : srgb ? std::vector<Format>{Format::RGBA8_SRGB,Format::BGRA8_SRGB}
             : std::vector<Format>{Format::RGBA8_UNORM,Format::BGRA8_UNORM};
  for (auto config : configurations)
    for (auto format : formats) {
      const std::array<unsigned, 2> size{config.width, config.height};
      const View view=Experiment::layer_view(View{config.base, config.count == VK_REMAINING_MIP_LEVELS ? config.levels - config.base : config.count},size[0],size[1]);
      auto image = mip_fixture(size[0], size[1], config.levels, format);
      if (fetch_query) {
        // Four corners of every accessible mip, then interior texels where
        // enough requests remain. All inputs are legal signed integer values.
        for (unsigned i=0;i<uv.size();++i) {
          const unsigned lod=i%view.level_count;
          const auto &level=image.levels[view.base_mip+lod];
          const unsigned group=i/view.level_count;
          const unsigned x=group<4 ? ((group&1) ? level.width-1 : 0) : (i*37)%level.width;
          const unsigned y=group<4 ? ((group&2) ? level.height-1 : 0) : (i*17)%level.height;
          uv[i]={static_cast<float>(static_cast<int>(x)-Experiment::texel_offset()[0]),
                 static_cast<float>(static_cast<int>(y)-Experiment::texel_offset()[1])};
          mip_inputs[i]={uv[i][0],uv[i][1],static_cast<float>(lod),Experiment::volume() ? static_cast<float>(i%image.levels[view.base_mip+lod].depth) : Experiment::array_sampling() ? static_cast<float>(i%view.layer_count) : 0};
        }
        std::memcpy(inputs.map,mip_inputs.data(),mip_inputs.size()*sizeof(mip_inputs[0]));
      }
      if (sweep) {
        for (unsigned x=0;x<256;++x) {
          std::array<uint8_t,4> color{static_cast<uint8_t>(x),static_cast<uint8_t>(255-x),
                                    static_cast<uint8_t>((x*37)%256),static_cast<uint8_t>(x)};
          if (is_bgra(format)) std::swap(color[0],color[2]);
          std::copy(color.begin(),color.end(),image.data.begin()+image.levels[0].offset+x*4);
        }
      }
      auto texture = experiment.texture(size[0], size[1], vk_format(format),
                                        VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT, config.levels, config.base, config.count);
      std::vector<Texture> mapped_views;
      if (view_swizzle)
        for (auto mapping : mappings) {
          View mapped=view;mapped.components=mapping;
          mapped_views.push_back(experiment.additional_view(texture,vk_format(format),mapped));
        }
      std::vector<View> element_views;
      std::vector<std::vector<Texture>> element_mapped_views;
      if (experiment.nonuniform_index) {
        for (unsigned element = 0; element < experiment.array_length; ++element) {
          View selected = view;
          const unsigned offset = element % view.level_count;
          selected.base_mip += offset;
          selected.level_count -= offset;
          selected=Experiment::layer_view(selected,size[0],size[1],element);
          element_views.push_back(selected);
          element_mapped_views.emplace_back();
          for (auto mapping : mappings) {
            selected.components = Experiment::depth() ? ComponentMapping{} : mapping;
            element_mapped_views.back().push_back(experiment.additional_view(texture,vk_format(format),selected));
          }
        }
      }
      auto staging = experiment.buffer(image.data.size(), VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
      if (mips) std::memset(staging.map, 0xee, image.data.size());
      else std::memcpy(staging.map, image.data.data(), image.data.size());
      experiment.begin();
      experiment.transition(texture, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                            VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                            0, VK_ACCESS_TRANSFER_WRITE_BIT);
      std::vector<VkBufferImageCopy> copies;
      for (unsigned level = 0; level < image.levels.size(); ++level) {
        const auto &mip = image.levels[level];
        VkBufferImageCopy copy{};
        copy.bufferOffset = mip.offset;
        copy.bufferRowLength = mip.row_pitch / 4;
        copy.bufferImageHeight = image.volume ? mip.slice_pitch/mip.row_pitch : mip.layer_pitch ? mip.layer_pitch/mip.row_pitch : mip.height+2;
        copy.imageSubresource = {is_depth(image.format) ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, level, 0, image.layer_count};
        copy.imageExtent = {mip.width, mip.height, mip.depth};
        copies.push_back(copy);
      }
      vkCmdCopyBufferToImage(experiment.command, staging.handle, texture.handle,
                            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, copies.size(), copies.data());
      // Legal coherent host update after recording and before submission: copies
      // must consume the latest staging bytes, rather than memcpy at record time.
      if (mips) {
        std::memcpy(staging.map, image.data.data(), image.data.size());
      }
      experiment.transition(texture, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                            VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_PIPELINE_STAGE_TRANSFER_BIT,
                            VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
                            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
      experiment.submit();
      check_linear_layout(experiment,texture,image);
      const std::vector<AddressMode> modes = border_mode ? std::vector<AddressMode>{AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge,AddressMode::ClampToBorder}
          : std::vector<AddressMode>{AddressMode::Repeat,AddressMode::MirroredRepeat,AddressMode::ClampToEdge};
      for (auto u_mode : modes)
        for (auto v_mode : modes)
          for (unsigned border_index=0;border_index<(border_mode ? 3U : 1U);++border_index)
          for (unsigned filter_index = 0; filter_index < (min_mag ? 8U : 2U); ++filter_index) {
            if (border_mode && u_mode!=AddressMode::ClampToBorder && v_mode!=AddressMode::ClampToBorder) continue;
            if (cases == limit) goto finished;
            experiment.select_array_index(cases, false);
            const auto mag = static_cast<Filter>(min_mag ? (filter_index / 2) % 2 : filter_index);
            const auto min = static_cast<Filter>(min_mag ? filter_index % 2 : filter_index);
            const bool linear = mip_linear || (min_mag && filter_index >= 4);
            Sampling sampling{u_mode, v_mode, mag, min, linear ? MipFilter::Linear : MipFilter::Nearest};
            Experiment::volume_state(sampling);
            Experiment::depth_state(sampling);
            sampling.border_color=static_cast<BorderColor>(border_index);
            sampling.lod_bias=Experiment::requested_bias();
            auto sampler = experiment.samplerless ? VK_NULL_HANDLE : experiment.sampler(sampling, config.range, linear || (mips && !min_mag && view.level_count == 1) ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST);
            View sampled_view=view;
            const unsigned mapping_index=border_mode && border_index==1 && !fetch_query ? cases%2 : cases%mappings.size();
            if (view_swizzle) sampled_view.components=mappings[mapping_index];
            if (experiment.nonuniform_index) {
              const unsigned base_index = experiment.array_index;
              // Each lane uses legal coordinates/LOD relative to its selected
              // view; dimensions and level counts now differ within one warp.
              for (unsigned i = 0; i < uv.size(); ++i) {
                const View &selected = element_views[(i + base_index) % experiment.array_length];
                const unsigned lod = i % selected.level_count;
                const auto &level = image.levels[selected.base_mip + lod];
                uv[i] = {static_cast<float>(static_cast<int>((i * 37) % level.width)-Experiment::texel_offset()[0]),
                         static_cast<float>(static_cast<int>((i * 17) % level.height)-Experiment::texel_offset()[1])};
                mip_inputs[i] = {uv[i][0],uv[i][1],static_cast<float>(lod),Experiment::volume() ? static_cast<float>(i%level.depth) : Experiment::array_sampling() ? static_cast<float>(i%selected.layer_count) : 0};
              }
              std::memcpy(inputs.map,mip_inputs.data(),mip_inputs.size()*sizeof(mip_inputs[0]));
              for (unsigned element = 0; element < experiment.array_length; ++element) {
                experiment.array_index = element;
                experiment.descriptors(element_mapped_views[element][(mapping_index + element) % mappings.size()],
                    sampler, carrier, inputs, direct, output_address, uv.size(), input_stride, output_floats*4);
              }
              experiment.array_index = base_index;
              experiment.lane_invocations += uv.size();
              experiment.lane_active_a += uv.size();
            } else {
            experiment.descriptors(view_swizzle ? mapped_views[mapping_index] : texture,
                sampler, carrier, inputs, direct, output_address, uv.size(), input_stride, output_floats*4);
            }
            auto *actual = static_cast<float *>(direct.map);
            auto *indirect_values = static_cast<float *>(indirect.map);
            std::fill(actual, actual + uv.size() * output_floats, std::numeric_limits<float>::quiet_NaN());
            std::fill(indirect_values, indirect_values + uv.size() * output_floats, std::numeric_limits<float>::quiet_NaN());
            experiment.begin();
            if (mips && !sweep && u_mode == AddressMode::Repeat &&
                v_mode == (border_mode ? AddressMode::ClampToBorder : AddressMode::Repeat) &&
                filter_index == 0 && border_index == 0) {
              // Update a mip subrectangle immediately before tracing in the same
              // command buffer. This also catches stale device texture uploads.
              auto &mip = image.volume ? image.levels[0] : image.levels.back();
              const auto offset = mip.offset + (image.volume ? (mip.depth-1)*mip.slice_pitch : (image.layer_count-1)*mip.layer_pitch) + (mip.height - 1) * mip.row_pitch + (mip.width - 1) * 4;
              if(is_depth(image.format)) put_depth(image,offset,1-get_depth(image,offset));
              else for (unsigned c = 0; c < 4; ++c) image.data[offset + c] ^= 0xa5;
              std::memcpy(static_cast<std::uint8_t *>(staging.map) + offset, image.data.data() + offset, 4);
              VkBufferImageCopy patch{};
              patch.bufferOffset = offset;
              patch.imageSubresource = {is_depth(image.format) ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, image.volume ? 0 : config.levels - 1, image.layer_count-1, 1};
              patch.imageOffset = {static_cast<int>(mip.width - 1), static_cast<int>(mip.height - 1), image.volume ? static_cast<int>(mip.depth-1) : 0};
              patch.imageExtent = {1, 1, 1};
              experiment.transition(texture, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                  VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
              vkCmdCopyBufferToImage(experiment.command, staging.handle, texture.handle, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &patch);
              experiment.transition(texture, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                  VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
            }
            vkCmdBindPipeline(experiment.command, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR, experiment.pipeline);
            experiment.bind_descriptors();
            experiment.trace(experiment.command, &experiment.raygen, &experiment.miss,
                             &experiment.empty, &experiment.empty, uv.size(), 1, 1);
            VkMemoryBarrier visibility{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
            visibility.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
            visibility.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
            vkCmdPipelineBarrier(experiment.command, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
                                 VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &visibility, 0, nullptr, 0, nullptr);
            experiment.submit();
            if (std::memcmp(actual, indirect_values, uv.size() * output_floats * 4)) ++buffer_failed;
            if (inject_mismatch && cases == 0) actual[0] = 0.75f;
            if (inject_query_mismatch && cases == 0) actual[5] += 1;
            for (unsigned path = 0; path < 2; ++path) {
              auto *values = path ? indirect_values : actual;
              for (unsigned i = 0; i < uv.size(); ++i) {
              View lane_view = sampled_view;
              if (experiment.nonuniform_index) {
                const unsigned selected = (i + experiment.array_index) % experiment.array_length;
                lane_view = element_views[selected];
                lane_view.components = Experiment::depth() ? ComponentMapping{} : mappings[(mapping_index + selected) % mappings.size()];
              }
              const auto reference = Experiment::depth_compare() ? Experiment::depth_reference(image,sampled_view,sampling,uv[i][0],uv[i][1],mip_inputs[i][3],mip_inputs[i][2],config.range) : Experiment::volume() ? (fetch_query ? texel_fetch_volume(image,lane_view,static_cast<int32_t>(uv[i][0]),static_cast<int32_t>(uv[i][1]),static_cast<int32_t>(mip_inputs[i][3]),static_cast<int32_t>(mip_inputs[i][2])) : Experiment::volume_reference(image,sampled_view,sampling,uv[i][0],uv[i][1],Experiment::volume_w(i),mip_inputs[i][2],config.range)) : Experiment::cube() ? Experiment::cube_reference(image,sampled_view,sampling,Experiment::cube_model_direction(i),mip_inputs[i][2],config.range) : fetch_query ? texel_fetch(image,lane_view,static_cast<int32_t>(uv[i][0]),
                  static_cast<int32_t>(uv[i][1]),static_cast<int32_t>(mip_inputs[i][2]),Experiment::texel_offset(),static_cast<int32_t>(mip_inputs[i][3])) :
                  Experiment::gradients() ? sample_grad(image,sampled_view,sampling,uv[i][0],uv[i][1],
                      {mip_inputs[i][2],mip_inputs[i][3]},{Experiment::gradient_request(i)[2],Experiment::gradient_request(i)[3]},config.range,Experiment::texel_offset(),Experiment::array_sampling() ? Experiment::layer_request(i) : 0) :
                  sample_lod(image, sampled_view, sampling, uv[i][0], uv[i][1], mips ? mip_inputs[i][2] : 0, config.range,Experiment::texel_offset(),Experiment::array_sampling() ? Experiment::layer_request(i) : 0);
              if (query_mode) {
                if (values[i*output_floats+4]!=73) ++markers_failed;
                const auto size=texture_size(image,lane_view,(Experiment::cube() || (Experiment::volume() && !fetch_query)) ? static_cast<int32_t>(i%lane_view.level_count) : static_cast<int32_t>(mip_inputs[i][2]));
                const std::array<uint32_t,4> expected{size[0],size[1],texture_query_levels(image,lane_view),Experiment::volume() ? image.levels[lane_view.base_mip+(fetch_query ? static_cast<int32_t>(mip_inputs[i][2]) : i%lane_view.level_count)].depth : lane_view.layer_count};
                for (unsigned q=0;q<(Experiment::array_sampling() || Experiment::volume() ? 4U : 3U);++q) {
                  if (values[i*output_floats+(q==3 ? 8 : 5+q)]!=static_cast<float>(expected[q])) ++query_failed;
                  query_csv << cases << ',' << (path ? "bda" : "ssbo") << ',' << i << ','
                      << mip_inputs[i][2] << ',' << lane_view.base_mip << ',' << lane_view.level_count << ','
                      << q << ',' << expected[q] << ',' << values[i*output_floats+(q==3 ? 8 : 5+q)] << '\n';
                  ++query_components;
                }
                if ((Experiment::array_sampling() || Experiment::volume()) && (values[i*output_floats+9]!=123 || values[i*output_floats+10]!=456 || values[i*output_floats+11]!=789)) ++markers_failed;
              } else {
                const std::array<float,4> marker{73,123,456,789};
                if (!std::equal(marker.begin(),marker.end(),values+i*output_floats+4)) ++markers_failed;
              }
              for (unsigned c = 0; c < 4; ++c) {
                const float value = values[i * output_floats + c];
                const double error = std::abs(static_cast<double>(value) - reference.rgba[c]);
                const bool good = std::isfinite(value) && error <= tolerance;
                if (std::isfinite(value)) max_error = std::max(max_error, error);
                if (!good) {
                  ++mismatches;
                  if (mismatches <= 12) {
                    std::cerr << "MISMATCH case=" << cases << " uv=(" << uv[i][0] << ',' << uv[i][1]
                              << ") channel=" << c << " reference=" << reference.rgba[c]
                              << " gpu=" << value << " error=" << error << '\n';
                    for (const auto &tap : reference.taps)
                      std::cerr << "  reference tap=(" << tap.x << ',' << tap.y << ") offset="
                                << tap.byte_offset << " weight=" << tap.weight << '\n';
                  }
                }
                csv << cases << ',' << (path ? "bda" : "ssbo") << ',' << size[0] << ',' << size[1] << ',' << static_cast<int>(format)
                    << ',' << static_cast<int>(u_mode) << ',' << static_cast<int>(v_mode)
                    << ',' << static_cast<int>(mag) << ',' << static_cast<int>(min) << ',' << static_cast<int>(reference.filter)
                    << ',' << reference.sampler_lod << ',' << i << ',' << uv[i][0] << ',' << uv[i][1]
                    << ',' << (mips ? mip_inputs[i][2] : 0) << ',' << lane_view.base_mip << ',' << lane_view.level_count << ',' << reference.mip
                    << ',' << reference.mip_upper << ',' << reference.mip_weight << ',' << static_cast<int>(sampling.mip_filter)
                    << ',' << packed_mapping(lane_view.components)
                    << ',' << static_cast<unsigned>(sampling.border_color) << ',' << sampling.lod_bias << ',' << reference.footprint_squared << ',' << reference.input_lod
                    << ',' << (mips ? mip_inputs[i][3] : 0)
                    << ',' << (Experiment::gradients() ? Experiment::gradient_request(i)[2] : 0)
                    << ',' << (Experiment::gradients() ? Experiment::gradient_request(i)[3] : 0) << ',' << c << ',' << reference.rgba[c] << ',' << value << ',' << error
                    << ',' << reference.input_layer << ',' << lane_view.base_layer << ',' << lane_view.layer_count << ',' << reference.layer
                    << ',' << reference.input_direction[0] << ',' << reference.input_direction[1] << ',' << reference.input_direction[2] << ',' << reference.cube_face << ',' << reference.input_w << ',' << static_cast<unsigned>(sampling.address_w) << ',' << image.levels[reference.mip].depth << '\n';
                ++components;
              }
              ++samples;
              }
            }
            if (view_swizzle) ++mapping_counts[mapping_index];
            if (border_mode) ++border_counts[border_index];
            ++cases;
            std::cout << "CASE " << cases << "/" << std::min(limit, total_cases)
                      << " samples=" << samples << " mismatches=" << mismatches
                      << " markers_failed=" << markers_failed << " buffer_failed=" << buffer_failed << std::endl;
          }
    }
finished:
  csv.flush();
  if (!csv) throw std::runtime_error("cannot write numeric result CSV");
  if (query_mode) {
    query_csv.flush();
    if (!query_csv) throw std::runtime_error("cannot write query CSV");
  }
  experiment.array_lifecycle_checks();
  experiment.print_uniform_profile();
  loaded_libraries();
  std::cout << "PROFILE separate=" << experiment.separate
            << " descriptor_sets=" << (experiment.sampler_set ? 2 : 1)
            << " sampler_bindings=" << experiment.separate_sampler_bindings
            << " sampler_objects=" << experiment.samplers.size()
            << " array_binding=" << experiment.array_binding
            << " array_length=" << experiment.array_length << " array_index=" << experiment.array_index
            << " element_writes=" << experiment.element_writes << " element_copies=" << experiment.element_copies
            << " consecutive_copies=" << experiment.consecutive_copies << std::endl;
  std::cout << std::setprecision(10) << "SUMMARY cases=" << cases << " samples=" << samples
            << " components=" << components << " mismatches=" << mismatches
            << " markers_failed=" << markers_failed << " buffer_failed=" << buffer_failed << " max_abs_error=" << max_error
            << " tolerance=" << tolerance << " injected=" << inject_mismatch << " format_checks=" << format_checks
            << " query_components=" << query_components << " query_failed=" << query_failed
            << " injected_query=" << inject_query_mismatch;
  if (view_swizzle)
    for (unsigned m=0;m<mapping_counts.size();++m) std::cout << " mapping" << m << '=' << mapping_counts[m];
  if (border_mode)
    for (unsigned b=0;b<border_counts.size();++b) std::cout << " border" << b << '=' << border_counts[b];
  std::cout << std::endl;
  return cases && !mismatches && !markers_failed && !buffer_failed && !query_failed ? 0 : 1;
}

// Each resource has its own model/view; shared-image fixtures deliberately
// point both views at the same model and upload buffer.
struct MultiResource {
  Image *image;
  Texture texture;
  Buffer staging;
  View view;
};

void check_linear_layout(Experiment &experiment, Texture texture, const Image &image) {
  if (!texture.linear) return;
  void *mapped=nullptr;
  check(vkMapMemory(experiment.device,texture.memory,0,VK_WHOLE_SIZE,0,&mapped),"map linear image");
  for (unsigned mip=0;mip<image.levels.size();++mip) {
    const auto &source=image.levels[mip];
    for (unsigned layer=0;layer<image.layer_count;++layer) {
      const VkImageSubresource subresource{is_depth(image.format) ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT,mip,layer};
      VkSubresourceLayout layout{};
      vkGetImageSubresourceLayout(experiment.device,texture.handle,&subresource,&layout);
      if (layout.rowPitch<source.width*4 || layout.size<(source.height-1)*layout.rowPitch+source.width*4 ||
          (image.layer_count>1 && layout.arrayPitch<layout.size))
        throw std::runtime_error("inconsistent linear subresource layout");
      if (image.volume && (layout.depthPitch<(source.height-1)*layout.rowPitch+source.width*4 || layout.size<(source.depth-1)*layout.depthPitch+(source.height-1)*layout.rowPitch+source.width*4)) throw std::runtime_error("inconsistent volume layout");
      for (unsigned z=0;z<source.depth;++z)
      for (unsigned y=0;y<source.height;++y)
        if (std::memcmp(static_cast<const std::uint8_t *>(mapped)+layout.offset+z*layout.depthPitch+y*layout.rowPitch,
                        image.data.data()+source.offset+layer*source.layer_pitch+z*source.slice_pitch+y*source.row_pitch,source.width*4))
          throw std::runtime_error("queried linear layout does not locate uploaded texels");
    }
  }
  vkUnmapMemory(experiment.device,texture.memory);
}

void record_upload(Experiment &experiment, const MultiResource &resource, bool initial) {
  const auto &image = *resource.image;
  std::memcpy(resource.staging.map, image.data.data(), image.data.size());
  experiment.transition(resource.texture,
      initial ? VK_IMAGE_LAYOUT_UNDEFINED : VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
      VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
      initial ? VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT : VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
      VK_PIPELINE_STAGE_TRANSFER_BIT, initial ? 0 : VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
  std::vector<VkBufferImageCopy> copies;
  for (unsigned level = 0; level < image.levels.size(); ++level) {
    const auto &mip = image.levels[level];
    VkBufferImageCopy copy{};
    copy.bufferOffset = mip.offset;
    copy.bufferRowLength = mip.row_pitch / 4;
    copy.bufferImageHeight = image.volume ? mip.slice_pitch/mip.row_pitch : mip.layer_pitch ? mip.layer_pitch/mip.row_pitch : 0;
    copy.imageSubresource = {is_depth(image.format) ? VK_IMAGE_ASPECT_DEPTH_BIT : VK_IMAGE_ASPECT_COLOR_BIT, level, 0, image.layer_count};
    copy.imageExtent = {mip.width, mip.height, mip.depth};
    copies.push_back(copy);
  }
  vkCmdCopyBufferToImage(experiment.command, resource.staging.handle, resource.texture.handle,
      VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, copies.size(), copies.data());
  experiment.transition(resource.texture, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
      VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_PIPELINE_STAGE_TRANSFER_BIT,
      VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
}

int run_multi(const char *rgen, const char *rmiss, const char *csv_path,
              unsigned limit, bool inject_mismatch, bool mip_linear, bool min_mag, bool srgb, bool view_swizzle, bool border_mode, bool separate = false) {
  Experiment experiment;
  experiment.initialize();
  if(Experiment::depth()) {view_swizzle=false;srgb=false;}
  const unsigned format_checks = Experiment::depth() ? depth_format_checks(experiment) : srgb || view_swizzle || Experiment::cube() || Experiment::volume() ? srgb_format_checks(experiment) : 0;
  experiment.empty_tlas();
  experiment.setup_pipeline(rgen, rmiss, true, separate);
  auto uv = requests();
  if (std::getenv("VENTUS_TEXTURE_OFFSET")) uv[0] = {-std::numeric_limits<float>::denorm_min(), 0.25f};
  const std::array<float, 16> lods{-10,0,0.49f,0.5f,std::nextafter(0.5f,1.f),0.51f,
      1,1.49f,1.5f,std::nextafter(1.5f,2.f),1.51f,2,2.5f,3,10,1000};
  std::vector<std::array<float, 4>> request_data;
  for (unsigned i = 0; i < uv.size(); ++i)
    request_data.push_back({uv[i][0],uv[i][1],Experiment::gradients() ? Experiment::gradient_request(i)[0] : lods[i % lods.size()],
                           Experiment::gradients() ? Experiment::gradient_request(i)[1] : lods[(i * 7 + 3) % lods.size()]});
  const auto packed_requests=Experiment::gradient_inputs(request_data,true);
  auto inputs = experiment.buffer(packed_requests.size() * 16, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  std::memcpy(inputs.map, packed_requests.data(), packed_requests.size() * 16);
  auto direct = experiment.buffer(uv.size() * 64, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  auto indirect = experiment.buffer(uv.size() * 64, VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
  auto output_address = experiment.buffer(8, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  const auto bda = experiment.address(indirect);
  std::memcpy(output_address.map, &bda, 8);
  auto carrier = experiment.texture(uv.size(), 1, VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_USAGE_STORAGE_BIT);
  experiment.begin();
  experiment.transition(carrier, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL,
      VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR, 0, VK_ACCESS_SHADER_WRITE_BIT);
  experiment.submit();
  std::ofstream csv(csv_path);
  if (!csv) throw std::runtime_error("cannot open multi-binding CSV");
  csv << "case,pair,phase,path,output,resource_a,resource_b,view_a_base,view_b_base,mip_a,mip_b,mip_a_upper,mip_b_upper,mip_weight_a,mip_weight_b,request,u,v,lod_a,lod_b,address_a_u,address_a_v,mag_filter_a,min_filter_a,selected_filter_a,mip_filter_a,min_lod_a,max_lod_a,address_b_u,address_b_v,mag_filter_b,min_filter_b,selected_filter_b,mip_filter_b,min_lod_b,max_lod_b,channel,reference,gpu,abs_error,active_a,active_b,image_index_a,image_index_b,sampler_index_a,sampler_index_b,bias_a,bias_b,footprint_squared_a,footprint_squared_b,derived_lod_a,derived_lod_b,layer_a,layer_b,view_base_layer_a,view_base_layer_b,view_layer_count_a,view_layer_count_b\n";
  csv << std::setprecision(10);
  unsigned cases = 0;
  std::array<unsigned, 8> phase_counts{};
  std::array<unsigned, 3> pair_counts{};
  std::uint64_t samples = 0, mixed_results = 0, components = 0, mismatches = 0, markers_failed = 0, buffer_failed = 0;
  double max_error = 0;
  const std::array<const char *, 8> phases{"baseline","swap-images","swap-images-and-samplers",
      "swap-samplers","shared-sampler","update-a","redispatch","restore-a"};
  for (unsigned pair = 0; pair < 3; ++pair) {
    auto image_a = mip_fixture(pair == 1 ? 1 : 8, pair == 1 ? 8 : 4,
                              pair == 1 ? 3 : 4, Experiment::depth() ? Format::D32_SFLOAT : srgb ? Format::RGBA8_SRGB : Format::RGBA8_UNORM);
    auto image_b = mip_fixture(pair == 1 ? 5 : 3, pair == 1 ? 1 : 5, 3,
                              Experiment::depth() ? Format::D32_SFLOAT : srgb && pair == 1 ? Format::BGRA8_SRGB : Format::BGRA8_UNORM);
    if (Experiment::cube()) {
      image_a=mip_fixture(pair==1 ? 4 : 8,pair==1 ? 4 : 8,pair==1 ? 3 : 4,Experiment::depth() ? Format::D32_SFLOAT : srgb ? Format::RGBA8_SRGB : Format::RGBA8_UNORM);
      image_b=mip_fixture(pair==1 ? 5 : 4,pair==1 ? 5 : 4,3,srgb && pair==1 ? Format::BGRA8_SRGB : Format::BGRA8_UNORM);
    }
    const auto original_a = image_a.data;
    View view_a{pair == 2 ? 0U : 1U, pair == 1 ? 2U : pair == 2 ? 4U : 3U};
    View view_b{pair == 2 ? 2U : 0U, pair == 2 ? 2U : pair == 1 ? 3U : 2U};
    view_a=Experiment::layer_view(view_a,image_a.levels[0].width,image_a.levels[0].height);
    view_b=Experiment::layer_view(view_b,pair==2 ? image_a.levels[0].width : image_b.levels[0].width,pair==2 ? image_a.levels[0].height : image_b.levels[0].height);
    if (view_swizzle) {view_a.components=mappings[2+pair*2];view_b.components=mappings[3+pair*2];}
    if (border_mode && pair!=0) {view_a.components={};view_b.components={};}
    auto texture_a = experiment.texture(image_a.levels[0].width, image_a.levels[0].height,
        vk_format(image_a.format), VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        image_a.levels.size(), view_a.base_mip, view_a.level_count);
    if (view_swizzle) texture_a=experiment.additional_view(texture_a,vk_format(image_a.format),view_a);
    auto staging_a = experiment.buffer(image_a.data.size(), VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
    MultiResource resource_a{&image_a,texture_a,staging_a,view_a};
    MultiResource resource_b{};
    if (pair == 2) {
      resource_b = {&image_a,experiment.additional_view(texture_a,vk_format(image_a.format),view_b),staging_a,view_b};
    } else {
      auto texture_b = experiment.texture(image_b.levels[0].width,image_b.levels[0].height,
          vk_format(image_b.format),VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
          image_b.levels.size(),view_b.base_mip,view_b.level_count);
      if (view_swizzle) texture_b=experiment.additional_view(texture_b,vk_format(image_b.format),view_b);
      resource_b = {&image_b,texture_b,experiment.buffer(image_b.data.size(),VK_BUFFER_USAGE_TRANSFER_SRC_BIT),view_b};
    }
    experiment.begin();
    record_upload(experiment,resource_a,true);
    if (pair != 2) record_upload(experiment,resource_b,true);
    experiment.submit();
    check_linear_layout(experiment,resource_a.texture,*resource_a.image);
    check_linear_layout(experiment,resource_b.texture,*resource_b.image);
    for (unsigned u = 0; u < 3; ++u)
      for (unsigned v = 0; v < 3; ++v)
        for (unsigned filter = 0; filter < 2; ++filter) {
          Sampling sampling_a{static_cast<AddressMode>(u),static_cast<AddressMode>(v),static_cast<Filter>(filter),static_cast<Filter>(min_mag ? 1-filter : filter),mip_linear ? MipFilter::Linear : MipFilter::Nearest};
          Experiment::volume_state(sampling_a);
          Sampling sampling_b{static_cast<AddressMode>((v + 1) % 3),static_cast<AddressMode>((u + 2) % 3),static_cast<Filter>(1 - filter),static_cast<Filter>(min_mag ? filter : 1-filter),mip_linear && filter == 1 ? MipFilter::Linear : MipFilter::Nearest};
          if (border_mode) {
            sampling_a.address_v=AddressMode::ClampToBorder;
            sampling_b.address_u=AddressMode::ClampToBorder;
            if (u==2) sampling_a.address_u=AddressMode::ClampToBorder;
            if (v==2) sampling_b.address_v=AddressMode::ClampToBorder;
            sampling_a.border_color=static_cast<BorderColor>(pair);
            sampling_b.border_color=static_cast<BorderColor>((pair+2)%3);
          }
          const LodRange range_a{0,1000}, range_b{0.75,1.75};
          Experiment::volume_state(sampling_b);
          Experiment::depth_state(sampling_a);Experiment::depth_state(sampling_b,true);
          sampling_a.lod_bias=Experiment::requested_bias();
          sampling_b.lod_bias=-Experiment::requested_bias();
          const auto sampler_a = experiment.sampler(sampling_a,range_a,sampling_a.mip_filter == MipFilter::Linear ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST);
          const auto sampler_b = experiment.sampler(sampling_b,range_b,sampling_b.mip_filter == MipFilter::Linear ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST);
          for (unsigned phase = 0; phase < phases.size(); ++phase) {
            if (cases == limit) goto multi_finished;
            experiment.select_array_index(cases, true);
            const bool swap_images = phase == 1 || phase == 2;
            const bool swap_samplers = phase == 2 || phase == 3;
            const bool shared_sampler = phase == 4;
            const MultiResource &a = swap_images ? resource_b : resource_a;
            const MultiResource &b = swap_images ? resource_a : resource_b;
            const Sampling state_a = swap_samplers ? sampling_b : sampling_a;
            const Sampling state_b = shared_sampler ? sampling_a : swap_samplers ? sampling_a : sampling_b;
            const LodRange clamp_a = swap_samplers ? range_b : range_a;
            const LodRange clamp_b = shared_sampler || swap_samplers ? range_a : range_b;
            experiment.copy_elements = !experiment.nonuniform_index && (phase == 3 || phase == 4);
            if (phase != 6) {
              const auto selected_sampler_a = swap_samplers ? sampler_b : sampler_a;
              const auto selected_sampler_b = shared_sampler || swap_samplers ? sampler_a : sampler_b;
              if (experiment.nonuniform_index) {
                const unsigned base_index = experiment.array_index;
                for (unsigned element = 0; element < experiment.array_length; ++element) {
                  experiment.array_index = element;
                  const bool odd = element % 2;
                  // sampler_descriptor writes at the inverse array index.
                  experiment.descriptors(odd ? b.texture : a.texture,
                      odd ? selected_sampler_b : selected_sampler_a,
                      carrier,inputs,direct,output_address,uv.size(),(Experiment::gradients() || Experiment::array_sampling() || Experiment::cube() || Experiment::volume() || Experiment::depth_compare()) ? 32 : 16,64);
                  experiment.secondary_descriptor(odd ? a.texture : b.texture,
                      odd ? selected_sampler_a : selected_sampler_b);
                }
                experiment.array_index = base_index;
              } else {
                experiment.descriptors(a.texture,selected_sampler_a,
                    carrier,inputs,direct,output_address,uv.size(),(Experiment::gradients() || Experiment::array_sampling() || Experiment::cube() || Experiment::volume() || Experiment::depth_compare()) ? 32 : 16,64);
                experiment.secondary_descriptor(b.texture,selected_sampler_b);
              }
            }
            auto *actual = static_cast<float *>(direct.map);
            auto *indirect_values = static_cast<float *>(indirect.map);
            std::fill(actual,actual + uv.size() * 16,std::numeric_limits<float>::quiet_NaN());
            std::fill(indirect_values,indirect_values + uv.size() * 16,std::numeric_limits<float>::quiet_NaN());
            experiment.begin();
            if (phase == 5 || phase == 7) {
              image_a.data = original_a;
              if (phase == 5) {
                for (const auto &mip : image_a.levels)
                  for (unsigned layer=0;layer<(image_a.volume ? mip.depth : image_a.layer_count);++layer)
                  for (unsigned y = 0; y < mip.height; ++y)
                    for (unsigned x = 0; x < mip.width; ++x)
                      for (unsigned c = 0; c < 4; ++c)
                        if(is_depth(image_a.format)) {
                          if(c==0) {const auto offset=mip.offset+y*mip.row_pitch+x*4;put_depth(image_a,offset,1-get_depth(image_a,offset));}
                        } else image_a.data[mip.offset + layer*(image_a.volume ? mip.slice_pitch : mip.layer_pitch) + y * mip.row_pitch + x * 4 + c] ^= 0x5a;
              }
              record_upload(experiment,resource_a,false);
            }
            vkCmdBindPipeline(experiment.command,VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR,experiment.pipeline);
            experiment.bind_descriptors();
            experiment.trace(experiment.command,&experiment.raygen,&experiment.miss,&experiment.empty,&experiment.empty,uv.size(),1,1);
            VkMemoryBarrier visibility{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
            visibility.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
            visibility.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
            vkCmdPipelineBarrier(experiment.command,VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
                VK_PIPELINE_STAGE_HOST_BIT,0,1,&visibility,0,nullptr,0,nullptr);
            experiment.submit();
            if (std::memcmp(actual,indirect_values,uv.size() * 64)) ++buffer_failed;
            // The control corrupts only binding 13's BDA result, ensuring both
            // bindings and both readback paths are checked independently.
            if (inject_mismatch && cases == 0) indirect_values[4] = 0.75f;
            for (unsigned i = 0; i < uv.size(); ++i) {
              const auto &request = request_data[i];
              const unsigned index_a = (i + experiment.array_index) % experiment.array_length;
              const unsigned index_b = (i / 2 + experiment.array_index + 1) % experiment.array_length;
              // Independent sampler writes are at N-1-element, so undo that
              // transformation when selecting the host sampling state.
              const unsigned sampler_a_index = (i * 3 + experiment.array_length - experiment.array_index) % experiment.array_length;
              const unsigned sampler_b_index = (i / 2 * 3 + experiment.array_length + 1 - experiment.array_index) % experiment.array_length;
              const bool flip_a = experiment.nonuniform_index && index_a % 2;
              const bool flip_b = experiment.nonuniform_index && index_b % 2;
              const bool flip_sa = experiment.nonuniform_index && (experiment.separate ?
                  (experiment.array_length - 1 - sampler_a_index) % 2 : index_a % 2);
              const bool flip_sb = experiment.nonuniform_index && (experiment.separate ?
                  (experiment.array_length - 1 - sampler_b_index) % 2 : index_b % 2);
              const MultiResource &lane_a = flip_a ? b : a;
              const MultiResource &lane_b = flip_b ? a : b;
              auto ra = Experiment::depth_compare() ? Experiment::depth_reference(*lane_a.image,lane_a.view,flip_sa ? state_b : state_a,request[0],request[1],Experiment::dref(i),request[2],flip_sa ? clamp_b : clamp_a) : Experiment::volume() ? Experiment::volume_reference(*lane_a.image,lane_a.view,flip_sa ? state_b : state_a,request[0],request[1],Experiment::volume_w(i),request[2],flip_sa ? clamp_b : clamp_a) : Experiment::cube() ? Experiment::cube_reference(*lane_a.image,lane_a.view,flip_sa ? state_b : state_a,Experiment::cube_model_direction(i),request[2],flip_sa ? clamp_b : clamp_a) : Experiment::gradients() ? sample_grad(*lane_a.image,lane_a.view,flip_sa ? state_b : state_a,
                  request[0],request[1],{request[2],request[3]},
                  {Experiment::gradient_request(i)[2],Experiment::gradient_request(i)[3]},flip_sa ? clamp_b : clamp_a,Experiment::texel_offset(),Experiment::array_sampling() ? Experiment::layer_request(i) : 0) :
                  sample_lod(*lane_a.image,lane_a.view,flip_sa ? state_b : state_a,
                  request[0],request[1],request[2],flip_sa ? clamp_b : clamp_a,Experiment::texel_offset(),Experiment::array_sampling() ? Experiment::layer_request(i) : 0);
              auto rb = Experiment::depth_compare() ? Experiment::depth_reference(*lane_b.image,lane_b.view,flip_sb ? state_a : state_b,request[1],request[0],Experiment::dref(i,true),request[3],flip_sb ? clamp_a : clamp_b) : Experiment::volume() ? Experiment::volume_reference(*lane_b.image,lane_b.view,flip_sb ? state_a : state_b,request[1],request[0],Experiment::volume_w(i,true),request[3],flip_sb ? clamp_a : clamp_b) : Experiment::cube() ? Experiment::cube_reference(*lane_b.image,lane_b.view,flip_sb ? state_a : state_b,Experiment::cube_model_direction(i,true),request[3],flip_sb ? clamp_a : clamp_b) : Experiment::gradients() ? sample_grad(*lane_b.image,lane_b.view,flip_sb ? state_a : state_b,
                  request[1],request[0],{request[3],request[2]},
                  {Experiment::gradient_request(i)[3],Experiment::gradient_request(i)[2]},flip_sb ? clamp_a : clamp_b,Experiment::texel_offset(),Experiment::array_sampling() ? Experiment::layer_request(i,true) : 0) :
                  sample_lod(*lane_b.image,lane_b.view,flip_sb ? state_a : state_b,
                  request[1],request[0],request[3],flip_sb ? clamp_a : clamp_b,Experiment::texel_offset(),Experiment::array_sampling() ? Experiment::layer_request(i,true) : 0);
              if (experiment.nonuniform_index) {
                ++experiment.lane_invocations;
                ++experiment.lane_mixes;
                if (i % 4 == 3) ra.rgba = {}; else ++experiment.lane_active_a;
                if (i % 2) rb.rgba = {}; else ++experiment.lane_active_b;
              }
              Color mix{};
              for (unsigned c = 0; c < 4; ++c) mix[c] = ra.rgba[c] * 0.25f + rb.rgba[c] * 0.75f;
              const std::array<Color, 3> expected{ra.rgba,rb.rgba,mix};
              const std::array<float, 4> marker{73,123,456,789};
              for (unsigned path = 0; path < 2; ++path) {
                const float *values = (path ? indirect_values : actual) + i * 16;
                if (!std::equal(marker.begin(),marker.end(),values + 12)) ++markers_failed;
                for (unsigned output = 0; output < expected.size(); ++output) {
                  for (unsigned c = 0; c < 4; ++c) {
                    const float value = values[output * 4 + c];
                    const double error = std::abs(static_cast<double>(value) - expected[output][c]);
                    if (std::isfinite(value)) max_error = std::max(max_error,error);
                    if (!std::isfinite(value) || error > tolerance) {
                      ++mismatches;
                      if (mismatches <= 12) std::cerr << "MISMATCH case=" << cases << " pair=" << pair
                          << " phase=" << phases[phase] << " path=" << path << " output=" << output
                          << " request=" << i << " channel=" << c << " reference=" << expected[output][c]
                          << " gpu=" << value << " error=" << error << '\n';
                    }
                    csv << cases << ',' << pair << ',' << phases[phase] << ',' << (path ? "bda" : "ssbo")
                        << ',' << output << ',' << (swap_images != flip_a ? 1 : 0) << ',' << (swap_images != flip_b ? 0 : 1)
                        << ',' << lane_a.view.base_mip << ',' << lane_b.view.base_mip << ',' << ra.mip << ',' << rb.mip
                        << ',' << ra.mip_upper << ',' << rb.mip_upper << ',' << ra.mip_weight << ',' << rb.mip_weight
                        << ',' << i << ',' << request[0] << ',' << request[1] << ',' << request[2] << ',' << request[3]
                        << ',' << static_cast<int>((flip_sa ? state_b : state_a).address_u) << ',' << static_cast<int>((flip_sa ? state_b : state_a).address_v)
                        << ',' << static_cast<int>((flip_sa ? state_b : state_a).mag_filter) << ',' << static_cast<int>((flip_sa ? state_b : state_a).min_filter) << ',' << static_cast<int>(ra.filter)
                        << ',' << static_cast<int>((flip_sa ? state_b : state_a).mip_filter) << ',' << (flip_sa ? clamp_b : clamp_a).min << ',' << (flip_sa ? clamp_b : clamp_a).max
                        << ',' << static_cast<int>((flip_sb ? state_a : state_b).address_u) << ',' << static_cast<int>((flip_sb ? state_a : state_b).address_v)
                        << ',' << static_cast<int>((flip_sb ? state_a : state_b).mag_filter) << ',' << static_cast<int>((flip_sb ? state_a : state_b).min_filter) << ',' << static_cast<int>(rb.filter)
                        << ',' << static_cast<int>((flip_sb ? state_a : state_b).mip_filter) << ',' << (flip_sb ? clamp_a : clamp_b).min << ',' << (flip_sb ? clamp_a : clamp_b).max
                        << ',' << c << ',' << expected[output][c] << ',' << value << ',' << error
                        << ',' << (!experiment.nonuniform_index || i % 4 != 3)
                        << ',' << (!experiment.nonuniform_index || i % 2 == 0)
                        << ',' << (experiment.nonuniform_index ? index_a : experiment.array_index)
                        << ',' << (experiment.nonuniform_index ? index_b : experiment.array_index)
                        << ',' << (experiment.separate ? (experiment.nonuniform_index ? sampler_a_index : experiment.array_length - 1 - experiment.array_index) : (experiment.nonuniform_index ? index_a : experiment.array_index))
                        << ',' << (experiment.separate ? (experiment.nonuniform_index ? sampler_b_index : experiment.array_length - 1 - experiment.array_index) : (experiment.nonuniform_index ? index_b : experiment.array_index))
                        << ',' << (flip_sa ? state_b : state_a).lod_bias
                        << ',' << (flip_sb ? state_a : state_b).lod_bias
                        << ',' << ra.footprint_squared << ',' << rb.footprint_squared
                        << ',' << ra.input_lod << ',' << rb.input_lod << ',' << ra.layer << ',' << rb.layer
                        << ',' << lane_a.view.base_layer << ',' << lane_b.view.base_layer
                        << ',' << lane_a.view.layer_count << ',' << lane_b.view.layer_count << '\n';
                    ++components;
                  }
                  if (output < 2) ++samples;
                  else ++mixed_results;
                }
              }
            }
            ++phase_counts[phase]; ++pair_counts[pair]; ++cases;
            std::cout << "CASE " << cases << "/" << limit << " pair=" << pair << " phase=" << phases[phase]
                      << " mismatches=" << mismatches << " markers_failed=" << markers_failed << " buffer_failed=" << buffer_failed << std::endl;
          }
        }
  }
multi_finished:
  csv.flush();
  if (!csv) throw std::runtime_error("cannot write multi-binding CSV");
  experiment.array_lifecycle_checks();
  experiment.print_uniform_profile();
  loaded_libraries();
  std::cout << "PROFILE separate=" << experiment.separate
            << " descriptor_sets=" << (experiment.sampler_set ? 2 : 1)
            << " sampler_bindings=" << experiment.separate_sampler_bindings
            << " sampler_objects=" << experiment.samplers.size()
            << " array_binding=" << experiment.array_binding
            << " array_length=" << experiment.array_length << " array_index=" << experiment.array_index
            << " element_writes=" << experiment.element_writes << " element_copies=" << experiment.element_copies
            << " consecutive_copies=" << experiment.consecutive_copies << std::endl;
  std::cout << std::setprecision(10) << "SUMMARY cases=" << cases << " samples=" << samples
      << " mixed_results=" << mixed_results << " components=" << components << " mismatches=" << mismatches << " markers_failed=" << markers_failed
      << " buffer_failed=" << buffer_failed << " max_abs_error=" << max_error << " tolerance=" << tolerance
      << " injected=" << inject_mismatch << " format_checks=" << format_checks;
  for (unsigned phase = 0; phase < phase_counts.size(); ++phase) std::cout << " phase" << phase << '=' << phase_counts[phase];
  for (unsigned pair = 0; pair < pair_counts.size(); ++pair) std::cout << " pair" << pair << '=' << pair_counts[pair];
  std::cout << std::endl;
  return cases && !mismatches && !markers_failed && !buffer_failed ? 0 : 1;
}

} // namespace

int main(int argc, char **argv) {
  try {
    if (Experiment::gradients()) tolerance=4e-6;
    if (argc > 6 && std::string(argv[6]) == "checks")
      return profile_checks(argv[1], argv[2], argc - 7, argv + 7);
    const std::string mode=argc==7 ? argv[6] : "";
    if (mode=="multi" || mode=="multi-linear" || mode=="multi-min-mag" || mode=="multi-srgb" || mode=="multi-swizzle" || mode=="multi-border" || mode=="multi-separate")
      return run_multi(argv[1],argv[2],argv[3],std::stoul(argv[4]),std::string(argv[5])=="1",
          mode!="multi",mode=="multi-min-mag" || mode=="multi-srgb" || mode=="multi-swizzle" || mode=="multi-border" || mode=="multi-separate",
          mode=="multi-srgb" || mode=="multi-swizzle" || mode=="multi-border" || mode=="multi-separate",
          mode=="multi-swizzle" || mode=="multi-border" || mode=="multi-separate",mode=="multi-border" || mode=="multi-separate",mode=="multi-separate");
    const bool mip_linear=mode=="mip-linear";
    const bool srgb=mode=="srgb" || mode=="srgb-values";
    const bool fetch_query=mode=="fetch-query" || mode=="fetch-swizzle" || mode=="fetch-border" || mode=="fetch-separate";
    const bool separate=mode=="separate" || mode=="fetch-separate";
    const bool border_mode=mode=="border" || mode=="fetch-border" || separate;
    const bool view_swizzle=mode=="swizzle" || mode=="fetch-swizzle" || border_mode;
    const bool min_mag=mode=="min-mag" || srgb || fetch_query || view_swizzle;
    const bool mips=mode=="mips" || mip_linear || min_mag;
    if (argc!=6 && !mips) throw std::runtime_error("expected rgen.spv rmiss.spv numeric.csv case-limit inject-mismatch");
    return run(argv[1],argv[2],argv[3],std::stoul(argv[4]),std::string(argv[5])=="1",
        mips,mip_linear,min_mag,srgb,mode=="srgb-values",fetch_query,std::string(argv[5])=="2",view_swizzle,border_mode,separate);
  } catch (const std::exception &error) {
    std::cerr << "FAIL " << error.what() << std::endl;
    return 1;
  }
}
