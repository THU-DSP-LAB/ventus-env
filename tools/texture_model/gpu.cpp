// Numeric Vulkan -> SPIR-V -> Ventus ELF -> Spike regression. The CPU model
// supplies expectations only; actual floats come from SSBO and buffer-reference stores.
#include "model.hpp"

#include <vulkan/vulkan.h>

#include <algorithm>
#include <cmath>
#include <cstring>
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

constexpr double tolerance = 1e-6; // Declared before running: float shader arithmetic.
static_assert(sizeof(float) == 4 && std::numeric_limits<float>::is_iec559,
              "numeric readback requires IEEE 754 binary32");

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
    device_info.pEnabledFeatures = &core_features;
    device_info.pNext = &rt;
    device_info.queueCreateInfoCount = 1;
    device_info.pQueueCreateInfos = &queue_info;
    device_info.enabledExtensionCount = 3;
    device_info.ppEnabledExtensionNames = extensions;
    check(vkCreateDevice(physical, &device_info, nullptr, &device), "vkCreateDevice");
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

  Texture texture(unsigned width, unsigned height, VkFormat format, VkImageUsageFlags usage,
                  unsigned levels = 1, unsigned base = 0, unsigned count = 1) {
    VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    info.imageType = VK_IMAGE_TYPE_2D;
    info.format = format;
    info.extent = {width, height, 1};
    info.mipLevels = levels;
    info.arrayLayers = 1;
    info.samples = VK_SAMPLE_COUNT_1_BIT;
    info.tiling = VK_IMAGE_TILING_OPTIMAL;
    info.usage = usage;
    info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    Texture texture{};
    check(vkCreateImage(device, &info, nullptr, &texture.handle), "create image");
    VkMemoryRequirements requirements{};
    vkGetImageMemoryRequirements(device, texture.handle, &requirements);
    texture.memory = allocate(requirements, false);
    check(vkBindImageMemory(device, texture.handle, texture.memory, 0), "bind image");
    VkImageViewCreateInfo view{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    view.image = texture.handle;
    view.viewType = VK_IMAGE_VIEW_TYPE_2D;
    view.format = format;
    view.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, base, count, 0, 1};
    check(vkCreateImageView(device, &view, nullptr, &texture.view), "create image view");
    textures.push_back(texture);
    return texture;
  }

  Texture additional_view(Texture image, VkFormat format, View range) {
    VkImageViewCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    info.image = image.handle;
    info.viewType = VK_IMAGE_VIEW_TYPE_2D;
    info.format = format;
    info.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, range.base_mip, range.level_count, 0, 1};
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
    barrier.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, VK_REMAINING_MIP_LEVELS, 0, 1};
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

  void setup_pipeline(const char *rgen, const char *rmiss, bool multiple_bindings = false) {
    std::vector<VkDescriptorType> types{
        VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
        VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER};
    if (multiple_bindings) types.push_back(VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER);
    std::vector<VkDescriptorSetLayoutBinding> bindings(types.size());
    for (unsigned i = 0; i < bindings.size(); ++i)
      bindings[i] = {i == 6 ? 13 : i, types[i], 1, VK_SHADER_STAGE_RAYGEN_BIT_KHR, nullptr};
    VkDescriptorSetLayoutCreateInfo layout{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    layout.bindingCount = bindings.size();
    layout.pBindings = bindings.data();
    check(vkCreateDescriptorSetLayout(device, &layout, nullptr, &set_layout), "create descriptor layout");
    const VkDescriptorPoolSize sizes[]{{types[0], 1}, {types[1], 1},
                                     {types[2], multiple_bindings ? 2U : 1U}, {types[3], 3}};
    VkDescriptorPoolCreateInfo pool{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    pool.maxSets = 1;
    pool.poolSizeCount = 4;
    pool.pPoolSizes = sizes;
    check(vkCreateDescriptorPool(device, &pool, nullptr, &descriptor_pool), "create descriptor pool");
    VkDescriptorSetAllocateInfo allocate{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    allocate.descriptorPool = descriptor_pool;
    allocate.descriptorSetCount = 1;
    allocate.pSetLayouts = &set_layout;
    check(vkAllocateDescriptorSets(device, &allocate, &descriptor_set), "allocate descriptor set");
    VkPipelineLayoutCreateInfo pipeline_info{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
    pipeline_info.setLayoutCount = 1;
    pipeline_info.pSetLayouts = &set_layout;
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

  VkSampler sampler(Sampling sampling, LodRange range = {0, 0},
                    VkSamplerMipmapMode mode = VK_SAMPLER_MIPMAP_MODE_NEAREST) {
    const VkSamplerAddressMode modes[]{VK_SAMPLER_ADDRESS_MODE_REPEAT,
                                      VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT,
                                      VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE};
    VkSamplerCreateInfo info{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
    info.magFilter = sampling.mag_filter == Filter::Nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
    info.minFilter = sampling.min_filter == Filter::Nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
    info.mipmapMode = mode;
    info.addressModeU = modes[static_cast<unsigned>(sampling.address_u)];
    info.addressModeV = modes[static_cast<unsigned>(sampling.address_v)];
    info.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.minLod = range.min;
    info.maxLod = range.max;
    VkSampler result;
    check(vkCreateSampler(device, &info, nullptr, &result), "create sampler");
    samplers.push_back(result);
    return result;
  }

  void descriptors(Texture sampled, VkSampler sampler, Texture carrier, Buffer uv, Buffer direct, Buffer output_address,
                   std::uint64_t request_count, unsigned input_stride = 8, unsigned output_stride = 32) {
    VkWriteDescriptorSetAccelerationStructureKHR acceleration{
        VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET_ACCELERATION_STRUCTURE_KHR};
    acceleration.accelerationStructureCount = 1;
    acceleration.pAccelerationStructures = &tlas;
    const VkDescriptorImageInfo images[]{{VK_NULL_HANDLE, carrier.view, VK_IMAGE_LAYOUT_GENERAL},
                                         {sampler, sampled.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL}};
    const VkDescriptorBufferInfo data[]{{uv.handle, 0, request_count * input_stride},
                                      {direct.handle, 0, request_count * output_stride},
                                      {output_address.handle, 0, 8}};
    const VkDescriptorType types[]{VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR,
                                    VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                                    VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER,
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
    vkUpdateDescriptorSets(device, 6, writes, 0, nullptr);
  }

  void secondary_descriptor(Texture sampled, VkSampler sampler) {
    VkDescriptorImageInfo image{sampler, sampled.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL};
    VkWriteDescriptorSet write{VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
    write.dstSet = descriptor_set;
    write.dstBinding = 13;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    write.pImageInfo = &image;
    vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);
  }
};

Image fixture(unsigned width, unsigned height, Format format) {
  // Source offset and padded rows exercise Vulkan's buffer-to-image copy.
  const Level level{width, height, 16, (width + 3) * 4ULL};
  Image image{format, std::vector<std::uint8_t>(16 + height * level.row_pitch, 0xcd), {level}};
  for (unsigned y = 0; y < height; ++y)
    for (unsigned x = 0; x < width; ++x) {
      std::array<std::uint8_t, 4> color{
          static_cast<std::uint8_t>((x * 37 + y * 71 + 5) % 256),
          static_cast<std::uint8_t>((x * 89 + y * 11 + 23) % 256),
          static_cast<std::uint8_t>((x * 13 + y * 53 + 251) % 256),
          static_cast<std::uint8_t>((x * 31 + y * 29 + 19) % 256)};
      if (format == Format::BGRA8_UNORM) std::swap(color[0], color[2]);
      std::copy(color.begin(), color.end(), image.data.begin() + level.offset + y * level.row_pitch + x * 4);
    }
  return image;
}

Image mip_fixture(unsigned width, unsigned height, unsigned levels, Format format) {
  Image image{format, {}, {}};
  for (unsigned level = 0; level < levels; ++level) {
    auto part = fixture(width, height, format);
    const auto start = image.data.size();
    // Distinct mip content and poisoned padding make incorrect level selection visible.
    for (unsigned y = 0; y < height; ++y)
      for (unsigned x = 0; x < width; ++x)
        for (unsigned c = 0; c < 4; ++c) {
          auto &byte = part.data[part.levels[0].offset + y * part.levels[0].row_pitch + x * 4 + c];
          byte = static_cast<std::uint8_t>(byte + level * (47 + c * 19));
        }
    image.data.insert(image.data.end(), part.data.begin(), part.data.end());
    auto mip = part.levels[0];
    mip.offset += start;
    image.levels.push_back(mip);
    width = std::max(1U, width / 2); height = std::max(1U, height / 2);
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
  changed = base; changed.addressModeU = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_BORDER; verify("border-u", changed, false);
  changed = base; changed.addressModeV = VK_SAMPLER_ADDRESS_MODE_MIRROR_CLAMP_TO_EDGE; verify("mirror-clamp-v", changed, false);
  changed = base; changed.mipLodBias = 0.5f; verify("bias", changed, false);
  changed = base; changed.mipLodBias = std::numeric_limits<float>::quiet_NaN(); verify("nan-bias", changed, false);
  changed = base; changed.compareEnable = VK_TRUE; verify("comparison", changed, false);
  changed = base; changed.anisotropyEnable = VK_TRUE; verify("anisotropy", changed, false);
  changed = base; changed.unnormalizedCoordinates = VK_TRUE; verify("unnormalized", changed, false);
  changed = base; changed.minLod = -1; verify("negative-min-lod", changed, false);
  changed = base; changed.minLod = 2; changed.maxLod = 1; verify("reversed-lod", changed, false);
  changed = base; changed.maxLod = std::numeric_limits<float>::infinity(); verify("infinite-lod", changed, false);
}

void image_view_checks(Experiment &experiment, unsigned &images, unsigned &views) {
  VkImageFormatProperties properties{};
  check(vkGetPhysicalDeviceImageFormatProperties(experiment.physical, VK_FORMAT_R8G8B8A8_UNORM,
      VK_IMAGE_TYPE_2D, VK_IMAGE_TILING_OPTIMAL, VK_IMAGE_USAGE_SAMPLED_BIT, 0, &properties), "query mip limits");
  if (properties.maxMipLevels != 15 || properties.maxArrayLayers != 1)
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
  changed = info; changed.arrayLayers = 2; image_check("array-layers", changed, false);
  changed = info; changed.imageType = VK_IMAGE_TYPE_1D; image_check("1d", changed, false);
  changed = info; changed.imageType = VK_IMAGE_TYPE_3D; changed.extent.depth = 2; image_check("3d", changed, false);
  changed = info; changed.samples = VK_SAMPLE_COUNT_4_BIT; image_check("multisample", changed, false);
  changed = info; changed.flags = VK_IMAGE_CREATE_CUBE_COMPATIBLE_BIT; image_check("cube", changed, false);
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
  other = view; other.viewType = VK_IMAGE_VIEW_TYPE_2D_ARRAY; view_check("array-view", other, false);
  other = view; other.format = VK_FORMAT_B8G8R8A8_UNORM; view_check("format-reinterpretation", other, false);
  other = view; other.components.r = VK_COMPONENT_SWIZZLE_B; view_check("swizzle", other, false);
  other = view; other.subresourceRange.aspectMask = VK_IMAGE_ASPECT_DEPTH_BIT; view_check("depth-aspect", other, false);
}

int profile_checks(const char *rgen, const char *rmiss, int bad_count, char **bad_shaders) {
  unsigned samplers = 0, shaders = 0, images = 0, views = 0;
  Experiment baseline;
  baseline.initialize();
  sampler_checks(baseline, samplers);
  image_view_checks(baseline, images, views);
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
  std::cout << "CHECKS samplers=" << samplers << " shaders=" << shaders << " images=" << images << " views=" << views << " baseline=pass errors=0" << std::endl;
  return 0;
}

int run(const char *rgen, const char *rmiss, const char *csv_path, unsigned limit,
        bool inject_mismatch, bool mips = false, bool mip_linear = false, bool min_mag = false) {
  Experiment experiment;
  experiment.initialize();
  experiment.empty_tlas();
  experiment.setup_pipeline(rgen, rmiss);
  auto uv = requests();
  if (mips) {
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
    mip_inputs.push_back({uv[i][0], uv[i][1], lods[i % lods.size()], 0});
  auto inputs = experiment.buffer(uv.size() * (mips ? 16 : 8), VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  std::memcpy(inputs.map, mips ? static_cast<const void *>(mip_inputs.data()) : static_cast<const void *>(uv.data()), uv.size() * (mips ? 16 : 8));
  auto direct = experiment.buffer(uv.size() * 32, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  auto indirect = experiment.buffer(uv.size() * 32, VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT);
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
  csv << "case,path,width,height,format,address_u,address_v,mag_filter,min_filter,selected_filter,sampler_lod,request,u,v,input_lod,view_base,view_count,selected_mip,mip_upper,mip_weight,mip_filter,channel,reference,gpu,abs_error\n";
  csv << std::setprecision(10);
  unsigned cases = 0;
  std::uint64_t samples = 0, components = 0, mismatches = 0, markers_failed = 0, buffer_failed = 0;
  double max_error = 0;
  struct Config { unsigned width, height, levels, base, count; LodRange range; };
  const std::vector<Config> configurations = mips ? std::vector<Config>{
      {5,3,3,0,3,{0,1000}}, {5,3,3,1,VK_REMAINING_MIP_LEVELS,{0,1000}},
      {8,4,4,0,4,{0.75,1.75}}, {8,4,4,1,2,{2,2}},
      {1,8,3,0,3,{0,0.5}}, {1,8,3,2,1,{0,1000}},
      {1,1,1,0,1,{2,2}}, {3,5,2,1,1,{0,1000}},
      {16384,1,15,0,15,{0,1000}}, {16384,1,15,13,VK_REMAINING_MIP_LEVELS,{0,1000}}} : std::vector<Config>{
      {1,1,1,0,1,{0,0}}, {1,5,1,0,1,{0,0}}, {5,1,1,0,1,{0,0}},
      {3,5,1,0,1,{0,0}}, {4,4,1,0,1,{0,0}}};
  const unsigned total_cases = configurations.size() * (min_mag ? 144 : 36);
  for (auto config : configurations)
    for (auto format : {Format::RGBA8_UNORM, Format::BGRA8_UNORM}) {
      const std::array<unsigned, 2> size{config.width, config.height};
      const View view{config.base, config.count == VK_REMAINING_MIP_LEVELS ? config.levels - config.base : config.count};
      auto image = mip_fixture(size[0], size[1], config.levels, format);
      const auto vk_format = format == Format::RGBA8_UNORM ? VK_FORMAT_R8G8B8A8_UNORM : VK_FORMAT_B8G8R8A8_UNORM;
      auto texture = experiment.texture(size[0], size[1], vk_format,
                                        VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT, config.levels, config.base, config.count);
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
        copy.bufferImageHeight = mip.height + 2;
        copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, level, 0, 1};
        copy.imageExtent = {mip.width, mip.height, 1};
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
      for (auto u_mode : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
        for (auto v_mode : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
          for (unsigned filter_index = 0; filter_index < (min_mag ? 8U : 2U); ++filter_index) {
            if (cases == limit) goto finished;
            const auto mag = static_cast<Filter>(min_mag ? (filter_index / 2) % 2 : filter_index);
            const auto min = static_cast<Filter>(min_mag ? filter_index % 2 : filter_index);
            const bool linear = mip_linear || (min_mag && filter_index >= 4);
            const Sampling sampling{u_mode, v_mode, mag, min, linear ? MipFilter::Linear : MipFilter::Nearest};
            auto sampler = experiment.sampler(sampling, config.range, linear || (mips && !min_mag && view.level_count == 1) ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST);
            experiment.descriptors(texture, sampler, carrier, inputs, direct, output_address, uv.size(), mips ? 16 : 8);
            auto *actual = static_cast<float *>(direct.map);
            auto *indirect_values = static_cast<float *>(indirect.map);
            std::fill(actual, actual + uv.size() * 8, std::numeric_limits<float>::quiet_NaN());
            std::fill(indirect_values, indirect_values + uv.size() * 8, std::numeric_limits<float>::quiet_NaN());
            experiment.begin();
            if (mips && u_mode == AddressMode::Repeat && v_mode == AddressMode::Repeat && filter_index == 0) {
              // Update a mip subrectangle immediately before tracing in the same
              // command buffer. This also catches stale device texture uploads.
              auto &mip = image.levels.back();
              const auto offset = mip.offset + (mip.height - 1) * mip.row_pitch + (mip.width - 1) * 4;
              for (unsigned c = 0; c < 4; ++c) image.data[offset + c] ^= 0xa5;
              std::memcpy(static_cast<std::uint8_t *>(staging.map) + offset, image.data.data() + offset, 4);
              VkBufferImageCopy patch{};
              patch.bufferOffset = offset;
              patch.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, config.levels - 1, 0, 1};
              patch.imageOffset = {static_cast<int>(mip.width - 1), static_cast<int>(mip.height - 1), 0};
              patch.imageExtent = {1, 1, 1};
              experiment.transition(texture, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                  VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_ACCESS_SHADER_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT);
              vkCmdCopyBufferToImage(experiment.command, staging.handle, texture.handle, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &patch);
              experiment.transition(texture, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                  VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
            }
            vkCmdBindPipeline(experiment.command, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR, experiment.pipeline);
            vkCmdBindDescriptorSets(experiment.command, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR,
                                    experiment.pipeline_layout, 0, 1, &experiment.descriptor_set, 0, nullptr);
            experiment.trace(experiment.command, &experiment.raygen, &experiment.miss,
                             &experiment.empty, &experiment.empty, uv.size(), 1, 1);
            VkMemoryBarrier visibility{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
            visibility.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
            visibility.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
            vkCmdPipelineBarrier(experiment.command, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
                                 VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &visibility, 0, nullptr, 0, nullptr);
            experiment.submit();
            if (std::memcmp(actual, indirect_values, uv.size() * 32)) ++buffer_failed;
            if (inject_mismatch && cases == 0) actual[0] = 0.75f;
            for (unsigned path = 0; path < 2; ++path) {
              auto *values = path ? indirect_values : actual;
              for (unsigned i = 0; i < uv.size(); ++i) {
              const auto reference = sample_lod(image, view, sampling, uv[i][0], uv[i][1], mips ? mip_inputs[i][2] : 0, config.range);
              const std::array<float, 4> marker{73, 123, 456, 789};
              if (!std::equal(marker.begin(), marker.end(), values + i * 8 + 4)) ++markers_failed;
              for (unsigned c = 0; c < 4; ++c) {
                const float value = values[i * 8 + c];
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
                    << ',' << (mips ? mip_inputs[i][2] : 0) << ',' << view.base_mip << ',' << view.level_count << ',' << reference.mip
                    << ',' << reference.mip_upper << ',' << reference.mip_weight << ',' << static_cast<int>(sampling.mip_filter)
                    << ',' << c << ',' << reference.rgba[c] << ',' << value << ',' << error << '\n';
                ++components;
              }
              ++samples;
              }
            }
            ++cases;
            std::cout << "CASE " << cases << "/" << std::min(limit, total_cases)
                      << " samples=" << samples << " mismatches=" << mismatches
                      << " markers_failed=" << markers_failed << " buffer_failed=" << buffer_failed << std::endl;
          }
    }
finished:
  csv.flush();
  if (!csv) throw std::runtime_error("cannot write numeric result CSV");
  loaded_libraries();
  std::cout << std::setprecision(10) << "SUMMARY cases=" << cases << " samples=" << samples
            << " components=" << components << " mismatches=" << mismatches
            << " markers_failed=" << markers_failed << " buffer_failed=" << buffer_failed << " max_abs_error=" << max_error
            << " tolerance=" << tolerance << " injected=" << inject_mismatch << std::endl;
  return cases && !mismatches && !markers_failed && !buffer_failed ? 0 : 1;
}

// Each resource has its own model/view; shared-image fixtures deliberately
// point both views at the same model and upload buffer.
struct MultiResource {
  Image *image;
  Texture texture;
  Buffer staging;
  View view;
};

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
    copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, level, 0, 1};
    copy.imageExtent = {mip.width, mip.height, 1};
    copies.push_back(copy);
  }
  vkCmdCopyBufferToImage(experiment.command, resource.staging.handle, resource.texture.handle,
      VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, copies.size(), copies.data());
  experiment.transition(resource.texture, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
      VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_PIPELINE_STAGE_TRANSFER_BIT,
      VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
}

int run_multi(const char *rgen, const char *rmiss, const char *csv_path,
              unsigned limit, bool inject_mismatch, bool mip_linear, bool min_mag) {
  Experiment experiment;
  experiment.initialize();
  experiment.empty_tlas();
  experiment.setup_pipeline(rgen, rmiss, true);
  const auto uv = requests();
  const std::array<float, 16> lods{-10,0,0.49f,0.5f,std::nextafter(0.5f,1.f),0.51f,
      1,1.49f,1.5f,std::nextafter(1.5f,2.f),1.51f,2,2.5f,3,10,1000};
  std::vector<std::array<float, 4>> request_data;
  for (unsigned i = 0; i < uv.size(); ++i)
    request_data.push_back({uv[i][0],uv[i][1],lods[i % lods.size()],lods[(i * 7 + 3) % lods.size()]});
  auto inputs = experiment.buffer(request_data.size() * 16, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  std::memcpy(inputs.map, request_data.data(), request_data.size() * 16);
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
  csv << "case,pair,phase,path,output,resource_a,resource_b,view_a_base,view_b_base,mip_a,mip_b,mip_a_upper,mip_b_upper,mip_weight_a,mip_weight_b,request,u,v,lod_a,lod_b,address_a_u,address_a_v,mag_filter_a,min_filter_a,selected_filter_a,mip_filter_a,min_lod_a,max_lod_a,address_b_u,address_b_v,mag_filter_b,min_filter_b,selected_filter_b,mip_filter_b,min_lod_b,max_lod_b,channel,reference,gpu,abs_error\n";
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
                              pair == 1 ? 3 : 4, Format::RGBA8_UNORM);
    auto image_b = mip_fixture(pair == 1 ? 5 : 3, pair == 1 ? 1 : 5, 3, Format::BGRA8_UNORM);
    const auto original_a = image_a.data;
    const View view_a{pair == 2 ? 0U : 1U, pair == 1 ? 2U : pair == 2 ? 4U : 3U};
    const View view_b{pair == 2 ? 2U : 0U, pair == 2 ? 2U : pair == 1 ? 3U : 2U};
    auto texture_a = experiment.texture(image_a.levels[0].width, image_a.levels[0].height,
        VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
        image_a.levels.size(), view_a.base_mip, view_a.level_count);
    auto staging_a = experiment.buffer(image_a.data.size(), VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
    MultiResource resource_a{&image_a,texture_a,staging_a,view_a};
    MultiResource resource_b{};
    if (pair == 2) {
      resource_b = {&image_a,experiment.additional_view(texture_a,VK_FORMAT_R8G8B8A8_UNORM,view_b),staging_a,view_b};
    } else {
      auto texture_b = experiment.texture(image_b.levels[0].width,image_b.levels[0].height,
          VK_FORMAT_B8G8R8A8_UNORM,VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT,
          image_b.levels.size(),view_b.base_mip,view_b.level_count);
      resource_b = {&image_b,texture_b,experiment.buffer(image_b.data.size(),VK_BUFFER_USAGE_TRANSFER_SRC_BIT),view_b};
    }
    experiment.begin();
    record_upload(experiment,resource_a,true);
    if (pair != 2) record_upload(experiment,resource_b,true);
    experiment.submit();
    for (unsigned u = 0; u < 3; ++u)
      for (unsigned v = 0; v < 3; ++v)
        for (unsigned filter = 0; filter < 2; ++filter) {
          const Sampling sampling_a{static_cast<AddressMode>(u),static_cast<AddressMode>(v),static_cast<Filter>(filter),static_cast<Filter>(min_mag ? 1-filter : filter),mip_linear ? MipFilter::Linear : MipFilter::Nearest};
          const Sampling sampling_b{static_cast<AddressMode>((v + 1) % 3),static_cast<AddressMode>((u + 2) % 3),static_cast<Filter>(1 - filter),static_cast<Filter>(min_mag ? filter : 1-filter),mip_linear && filter == 1 ? MipFilter::Linear : MipFilter::Nearest};
          const LodRange range_a{0,1000}, range_b{0.75,1.75};
          const auto sampler_a = experiment.sampler(sampling_a,range_a,sampling_a.mip_filter == MipFilter::Linear ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST);
          const auto sampler_b = experiment.sampler(sampling_b,range_b,sampling_b.mip_filter == MipFilter::Linear ? VK_SAMPLER_MIPMAP_MODE_LINEAR : VK_SAMPLER_MIPMAP_MODE_NEAREST);
          for (unsigned phase = 0; phase < phases.size(); ++phase) {
            if (cases == limit) goto multi_finished;
            const bool swap_images = phase == 1 || phase == 2;
            const bool swap_samplers = phase == 2 || phase == 3;
            const bool shared_sampler = phase == 4;
            const MultiResource &a = swap_images ? resource_b : resource_a;
            const MultiResource &b = swap_images ? resource_a : resource_b;
            const Sampling state_a = swap_samplers ? sampling_b : sampling_a;
            const Sampling state_b = shared_sampler ? sampling_a : swap_samplers ? sampling_a : sampling_b;
            const LodRange clamp_a = swap_samplers ? range_b : range_a;
            const LodRange clamp_b = shared_sampler || swap_samplers ? range_a : range_b;
            if (phase != 6) {
              experiment.descriptors(a.texture,swap_samplers ? sampler_b : sampler_a,
                  carrier,inputs,direct,output_address,uv.size(),16,64);
              experiment.secondary_descriptor(b.texture,shared_sampler || swap_samplers ? sampler_a : sampler_b);
            }
            auto *actual = static_cast<float *>(direct.map);
            auto *indirect_values = static_cast<float *>(indirect.map);
            std::fill(actual,actual + uv.size() * 16,std::numeric_limits<float>::quiet_NaN());
            std::fill(indirect_values,indirect_values + uv.size() * 16,std::numeric_limits<float>::quiet_NaN());
            experiment.begin();
            if (phase == 5 || phase == 7) {
              image_a.data = original_a;
              if (phase == 5)
                for (const auto &mip : image_a.levels)
                  for (unsigned y = 0; y < mip.height; ++y)
                    for (unsigned x = 0; x < mip.width; ++x)
                      for (unsigned c = 0; c < 4; ++c)
                        image_a.data[mip.offset + y * mip.row_pitch + x * 4 + c] ^= 0x5a;
              record_upload(experiment,resource_a,false);
            }
            vkCmdBindPipeline(experiment.command,VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR,experiment.pipeline);
            vkCmdBindDescriptorSets(experiment.command,VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR,
                experiment.pipeline_layout,0,1,&experiment.descriptor_set,0,nullptr);
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
              const auto ra = sample_lod(*a.image,a.view,state_a,request[0],request[1],request[2],clamp_a);
              const auto rb = sample_lod(*b.image,b.view,state_b,request[1],request[0],request[3],clamp_b);
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
                        << ',' << output << ',' << (swap_images ? 1 : 0) << ',' << (swap_images ? 0 : 1)
                        << ',' << a.view.base_mip << ',' << b.view.base_mip << ',' << ra.mip << ',' << rb.mip
                        << ',' << ra.mip_upper << ',' << rb.mip_upper << ',' << ra.mip_weight << ',' << rb.mip_weight
                        << ',' << i << ',' << request[0] << ',' << request[1] << ',' << request[2] << ',' << request[3]
                        << ',' << static_cast<int>(state_a.address_u) << ',' << static_cast<int>(state_a.address_v)
                        << ',' << static_cast<int>(state_a.mag_filter) << ',' << static_cast<int>(state_a.min_filter) << ',' << static_cast<int>(ra.filter)
                        << ',' << static_cast<int>(state_a.mip_filter) << ',' << clamp_a.min << ',' << clamp_a.max
                        << ',' << static_cast<int>(state_b.address_u) << ',' << static_cast<int>(state_b.address_v)
                        << ',' << static_cast<int>(state_b.mag_filter) << ',' << static_cast<int>(state_b.min_filter) << ',' << static_cast<int>(rb.filter)
                        << ',' << static_cast<int>(state_b.mip_filter) << ',' << clamp_b.min << ',' << clamp_b.max
                        << ',' << c << ',' << expected[output][c] << ',' << value << ',' << error << '\n';
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
  loaded_libraries();
  std::cout << std::setprecision(10) << "SUMMARY cases=" << cases << " samples=" << samples
      << " mixed_results=" << mixed_results << " components=" << components << " mismatches=" << mismatches << " markers_failed=" << markers_failed
      << " buffer_failed=" << buffer_failed << " max_abs_error=" << max_error << " tolerance=" << tolerance
      << " injected=" << inject_mismatch;
  for (unsigned phase = 0; phase < phase_counts.size(); ++phase) std::cout << " phase" << phase << '=' << phase_counts[phase];
  for (unsigned pair = 0; pair < pair_counts.size(); ++pair) std::cout << " pair" << pair << '=' << pair_counts[pair];
  std::cout << std::endl;
  return cases && !mismatches && !markers_failed && !buffer_failed ? 0 : 1;
}

} // namespace

int main(int argc, char **argv) {
  try {
    if (argc > 6 && std::string(argv[6]) == "checks")
      return profile_checks(argv[1], argv[2], argc - 7, argv + 7);
    if (argc == 7 && (std::string(argv[6]) == "multi" || std::string(argv[6]) == "multi-linear" || std::string(argv[6]) == "multi-min-mag"))
      return run_multi(argv[1],argv[2],argv[3],std::stoul(argv[4]),std::string(argv[5]) == "1",std::string(argv[6]) != "multi",std::string(argv[6]) == "multi-min-mag");
    const bool mip_linear = argc == 7 && std::string(argv[6]) == "mip-linear";
    const bool min_mag = argc == 7 && std::string(argv[6]) == "min-mag";
    const bool mips = argc == 7 && (std::string(argv[6]) == "mips" || mip_linear || min_mag);
    if (argc != 6 && !mips) throw std::runtime_error("expected rgen.spv rmiss.spv numeric.csv case-limit inject-mismatch");
    return run(argv[1], argv[2], argv[3], std::stoul(argv[4]), std::string(argv[5]) == "1", mips, mip_linear, min_mag);
  } catch (const std::exception &error) {
    std::cerr << "FAIL " << error.what() << std::endl;
    return 1;
  }
}
