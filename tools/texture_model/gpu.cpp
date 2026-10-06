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

  Texture texture(unsigned width, unsigned height, VkFormat format, VkImageUsageFlags usage) {
    VkImageCreateInfo info{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    info.imageType = VK_IMAGE_TYPE_2D;
    info.format = format;
    info.extent = {width, height, 1};
    info.mipLevels = 1;
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
    view.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    check(vkCreateImageView(device, &view, nullptr, &texture.view), "create image view");
    textures.push_back(texture);
    return texture;
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
    barrier.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
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

  void setup_pipeline(const char *rgen, const char *rmiss) {
    const std::array<VkDescriptorType, 6> types{
        VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
        VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
        VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER};
    std::array<VkDescriptorSetLayoutBinding, 6> bindings{};
    for (unsigned i = 0; i < bindings.size(); ++i)
      bindings[i] = {i, types[i], 1, VK_SHADER_STAGE_RAYGEN_BIT_KHR, nullptr};
    VkDescriptorSetLayoutCreateInfo layout{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    layout.bindingCount = bindings.size();
    layout.pBindings = bindings.data();
    check(vkCreateDescriptorSetLayout(device, &layout, nullptr, &set_layout), "create descriptor layout");
    const VkDescriptorPoolSize sizes[]{{types[0], 1}, {types[1], 1}, {types[2], 1}, {types[3], 3}};
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

  VkSampler sampler(Sampling sampling) {
    const VkSamplerAddressMode modes[]{VK_SAMPLER_ADDRESS_MODE_REPEAT,
                                      VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT,
                                      VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE};
    VkSamplerCreateInfo info{VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO};
    info.magFilter = info.minFilter = sampling.filter == Filter::Nearest ? VK_FILTER_NEAREST : VK_FILTER_LINEAR;
    info.mipmapMode = VK_SAMPLER_MIPMAP_MODE_NEAREST;
    info.addressModeU = modes[static_cast<unsigned>(sampling.address_u)];
    info.addressModeV = modes[static_cast<unsigned>(sampling.address_v)];
    info.addressModeW = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    info.maxLod = 0;
    VkSampler result;
    check(vkCreateSampler(device, &info, nullptr, &result), "create sampler");
    samplers.push_back(result);
    return result;
  }

  void descriptors(Texture sampled, VkSampler sampler, Texture carrier, Buffer uv, Buffer direct, Buffer output_address,
                   std::uint64_t request_count) {
    VkWriteDescriptorSetAccelerationStructureKHR acceleration{
        VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET_ACCELERATION_STRUCTURE_KHR};
    acceleration.accelerationStructureCount = 1;
    acceleration.pAccelerationStructures = &tlas;
    const VkDescriptorImageInfo images[]{{VK_NULL_HANDLE, carrier.view, VK_IMAGE_LAYOUT_GENERAL},
                                         {sampler, sampled.view, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL}};
    const VkDescriptorBufferInfo data[]{{uv.handle, 0, request_count * 8},
                                      {direct.handle, 0, request_count * 32},
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

int run(const char *rgen, const char *rmiss, const char *csv_path, unsigned limit,
        bool inject_mismatch) {
  Experiment experiment;
  experiment.initialize();
  experiment.empty_tlas();
  experiment.setup_pipeline(rgen, rmiss);
  const auto uv = requests();
  auto inputs = experiment.buffer(uv.size() * 8, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  std::memcpy(inputs.map, uv.data(), uv.size() * 8);
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
  csv << "case,path,width,height,format,address_u,address_v,filter,request,u,v,channel,reference,gpu,abs_error\n";
  csv << std::setprecision(10);
  unsigned cases = 0;
  std::uint64_t samples = 0, components = 0, mismatches = 0, markers_failed = 0, buffer_failed = 0;
  double max_error = 0;
  const std::array<std::array<unsigned, 2>, 5> sizes{{{1, 1}, {1, 5}, {5, 1}, {3, 5}, {4, 4}}};
  for (auto size : sizes)
    for (auto format : {Format::RGBA8_UNORM, Format::BGRA8_UNORM}) {
      const auto image = fixture(size[0], size[1], format);
      const auto vk_format = format == Format::RGBA8_UNORM ? VK_FORMAT_R8G8B8A8_UNORM : VK_FORMAT_B8G8R8A8_UNORM;
      auto texture = experiment.texture(size[0], size[1], vk_format,
                                        VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT);
      auto staging = experiment.buffer(image.data.size(), VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
      std::memcpy(staging.map, image.data.data(), image.data.size());
      experiment.begin();
      experiment.transition(texture, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                            VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                            0, VK_ACCESS_TRANSFER_WRITE_BIT);
      VkBufferImageCopy copy{};
      copy.bufferOffset = image.levels[0].offset;
      copy.bufferRowLength = image.levels[0].row_pitch / 4;
      copy.bufferImageHeight = size[1];
      copy.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
      copy.imageExtent = {size[0], size[1], 1};
      vkCmdCopyBufferToImage(experiment.command, staging.handle, texture.handle,
                            VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &copy);
      experiment.transition(texture, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                            VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_PIPELINE_STAGE_TRANSFER_BIT,
                            VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
                            VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_READ_BIT);
      experiment.submit();
      for (auto u_mode : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
        for (auto v_mode : {AddressMode::Repeat, AddressMode::MirroredRepeat, AddressMode::ClampToEdge})
          for (auto filter : {Filter::Nearest, Filter::Bilinear}) {
            if (cases == limit) goto finished;
            const Sampling sampling{u_mode, v_mode, filter};
            auto sampler = experiment.sampler(sampling);
            experiment.descriptors(texture, sampler, carrier, inputs, direct, output_address, uv.size());
            auto *actual = static_cast<float *>(direct.map);
            auto *indirect_values = static_cast<float *>(indirect.map);
            std::fill(actual, actual + uv.size() * 8, std::numeric_limits<float>::quiet_NaN());
            std::fill(indirect_values, indirect_values + uv.size() * 8, std::numeric_limits<float>::quiet_NaN());
            experiment.begin();
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
              const auto reference = sample_level(image, 0, sampling, uv[i][0], uv[i][1]);
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
                    << ',' << static_cast<int>(filter) << ',' << i << ',' << uv[i][0] << ',' << uv[i][1]
                    << ',' << c << ',' << reference.rgba[c] << ',' << value << ',' << error << '\n';
                ++components;
              }
              ++samples;
              }
            }
            ++cases;
            std::cout << "CASE " << cases << "/" << std::min(limit, 180U)
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

} // namespace

int main(int argc, char **argv) {
  try {
    if (argc != 6) throw std::runtime_error("expected rgen.spv rmiss.spv numeric.csv case-limit inject-mismatch");
    return run(argv[1], argv[2], argv[3], std::stoul(argv[4]), std::string(argv[5]) == "1");
  } catch (const std::exception &error) {
    std::cerr << "FAIL " << error.what() << std::endl;
    return 1;
  }
}
