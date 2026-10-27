// Standard Vulkan integration test: four sparse descriptor sets, descriptor
// arrays/copies, nonzero dynamic offsets, two geometries and reused BLAS.
// Build/run instructions and exact expectations are in README.md beside this
// file.
#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <vector>
#include <vulkan/vulkan.h>
#define CHECK(call)                                                            \
  do {                                                                         \
    VkResult r = (call);                                                       \
    if (r != VK_SUCCESS) {                                                     \
      fprintf(stderr, "%s: %d\n", #call, r);                                   \
      exit(1);                                                                 \
    }                                                                          \
  } while (0)
#define LOAD(name) auto name = (PFN_##name)vkGetDeviceProcAddr(dev, #name)
struct Buffer {
  VkBuffer handle;
  VkDeviceMemory memory;
  void *map;
  VkDeviceAddress address;
};
int main(int argc, char **argv) {
  if (argc != 2 && argc != 3) {
    fprintf(stderr, "usage: resource_layout SHADER_DIRECTORY\n");
    return 2;
  }
  const std::string variant = argc == 3 ? argv[2] : "single";
  const bool rebind = variant.find("rebind") != std::string::npos;
  const bool oneGeometry = variant.find("onegeometry") != std::string::npos;
  const bool aabb = variant.find("aabb") != std::string::npos;
  VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
  app.apiVersion = VK_API_VERSION_1_2;
  VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
  ici.pApplicationInfo = &app;
  VkInstance instance;
  CHECK(vkCreateInstance(&ici, nullptr, &instance));
  uint32_t count = 1;
  VkPhysicalDevice gpu;
  CHECK(vkEnumeratePhysicalDevices(instance, &count, &gpu));
  uint32_t qcount = 0;
  vkGetPhysicalDeviceQueueFamilyProperties(gpu, &qcount, nullptr);
  std::vector<VkQueueFamilyProperties> queues(qcount);
  vkGetPhysicalDeviceQueueFamilyProperties(gpu, &qcount, queues.data());
  uint32_t family = 0;
  while (family < qcount && !(queues[family].queueFlags & VK_QUEUE_COMPUTE_BIT))
    family++;
  float priority = 1;
  VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
  qci.queueFamilyIndex = family;
  qci.queueCount = 1;
  qci.pQueuePriorities = &priority;
  VkPhysicalDeviceBufferDeviceAddressFeatures bda{
      VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_BUFFER_DEVICE_ADDRESS_FEATURES};
  bda.bufferDeviceAddress = VK_TRUE;
  VkPhysicalDeviceAccelerationStructureFeaturesKHR asf{
      VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR};
  asf.accelerationStructure = VK_TRUE;
  asf.pNext = &bda;
  VkPhysicalDeviceRayTracingPipelineFeaturesKHR rtf{
      VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RAY_TRACING_PIPELINE_FEATURES_KHR};
  rtf.rayTracingPipeline = VK_TRUE;
  rtf.pNext = &asf;
  VkPhysicalDeviceFeatures features{};
  features.shaderStorageImageArrayDynamicIndexing = VK_TRUE;
  features.shaderStorageBufferArrayDynamicIndexing = VK_TRUE;
  const char *exts[] = {VK_KHR_ACCELERATION_STRUCTURE_EXTENSION_NAME,
                        VK_KHR_RAY_TRACING_PIPELINE_EXTENSION_NAME,
                        VK_KHR_DEFERRED_HOST_OPERATIONS_EXTENSION_NAME};
  VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
  dci.pNext = &rtf;
  dci.queueCreateInfoCount = 1;
  dci.pQueueCreateInfos = &qci;
  dci.enabledExtensionCount = 3;
  dci.ppEnabledExtensionNames = exts;
  dci.pEnabledFeatures = &features;
  VkDevice dev;
  CHECK(vkCreateDevice(gpu, &dci, nullptr, &dev));
  VkQueue queue;
  vkGetDeviceQueue(dev, family, 0, &queue);
  LOAD(vkCreateAccelerationStructureKHR);
  LOAD(vkDestroyAccelerationStructureKHR);
  LOAD(vkGetAccelerationStructureBuildSizesKHR);
  LOAD(vkCmdBuildAccelerationStructuresKHR);
  LOAD(vkGetAccelerationStructureDeviceAddressKHR);
  LOAD(vkCreateRayTracingPipelinesKHR);
  LOAD(vkGetRayTracingShaderGroupHandlesKHR);
  LOAD(vkCmdTraceRaysKHR);
  VkPhysicalDeviceMemoryProperties mem;
  vkGetPhysicalDeviceMemoryProperties(gpu, &mem);
  auto memoryType = [&](uint32_t bits, VkMemoryPropertyFlags flags) {
    for (uint32_t i = 0; i < mem.memoryTypeCount; i++)
      if ((bits & (1u << i)) &&
          (mem.memoryTypes[i].propertyFlags & flags) == flags)
        return i;
    std::abort();
  };
  std::vector<Buffer> buffers;
  auto buffer = [&](VkDeviceSize size, VkBufferUsageFlags usage) {
    Buffer b{};
    VkBufferCreateInfo ci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    ci.size = size;
    ci.usage = usage | VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT;
    CHECK(vkCreateBuffer(dev, &ci, nullptr, &b.handle));
    VkMemoryRequirements req;
    vkGetBufferMemoryRequirements(dev, b.handle, &req);
    VkMemoryAllocateFlagsInfo flags{
        VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO};
    flags.flags = VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT;
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.pNext = &flags;
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = memoryType(req.memoryTypeBits,
                                    VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
                                        VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    CHECK(vkAllocateMemory(dev, &ai, nullptr, &b.memory));
    CHECK(vkBindBufferMemory(dev, b.handle, b.memory, 0));
    CHECK(vkMapMemory(dev, b.memory, 0, req.size, 0, &b.map));
    memset(b.map, 0, req.size);
    VkBufferDeviceAddressInfo address{
        VK_STRUCTURE_TYPE_BUFFER_DEVICE_ADDRESS_INFO};
    address.buffer = b.handle;
    b.address = vkGetBufferDeviceAddress(dev, &address);
    buffers.push_back(b);
    return b;
  };
  VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
  pci.queueFamilyIndex = family;
  pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
  VkCommandPool pool;
  CHECK(vkCreateCommandPool(dev, &pci, nullptr, &pool));
  auto begin = [&]() {
    VkCommandBufferAllocateInfo ai{
        VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    ai.commandPool = pool;
    ai.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    ai.commandBufferCount = 1;
    VkCommandBuffer cmd;
    CHECK(vkAllocateCommandBuffers(dev, &ai, &cmd));
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    CHECK(vkBeginCommandBuffer(cmd, &bi));
    return cmd;
  };
  auto submit = [&](VkCommandBuffer cmd) {
    CHECK(vkEndCommandBuffer(cmd));
    VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.commandBufferCount = 1;
    si.pCommandBuffers = &cmd;
    CHECK(vkQueueSubmit(queue, 1, &si, VK_NULL_HANDLE));
    CHECK(vkQueueWaitIdle(queue));
    vkFreeCommandBuffers(dev, pool, 1, &cmd);
  };
  auto barrier = [&](VkCommandBuffer cmd, VkPipelineStageFlags src,
                     VkPipelineStageFlags dst, VkAccessFlags sa,
                     VkAccessFlags da) {
    VkMemoryBarrier b{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
    b.srcAccessMask = sa;
    b.dstAccessMask = da;
    vkCmdPipelineBarrier(cmd, src, dst, 0, 1, &b, 0, nullptr, 0, nullptr);
  };
  std::vector<VkAccelerationStructureKHR> structures;
  auto build = [&](VkAccelerationStructureTypeKHR type,
                   std::vector<VkAccelerationStructureGeometryKHR> geoms,
                   std::vector<VkAccelerationStructureBuildRangeInfoKHR> ranges,
                   bool pointers) {
    VkAccelerationStructureBuildGeometryInfoKHR info{
        VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_GEOMETRY_INFO_KHR};
    info.type = type;
    info.mode = VK_BUILD_ACCELERATION_STRUCTURE_MODE_BUILD_KHR;
    info.geometryCount = geoms.size();
    std::vector<const VkAccelerationStructureGeometryKHR *> gp;
    std::vector<uint32_t> counts;
    for (size_t i = 0; i < geoms.size(); i++) {
      gp.push_back(&geoms[i]);
      counts.push_back(ranges[i].primitiveCount);
    }
    if (pointers)
      info.ppGeometries = gp.data();
    else
      info.pGeometries = geoms.data();
    VkAccelerationStructureBuildSizesInfoKHR sizes{
        VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_SIZES_INFO_KHR};
    vkGetAccelerationStructureBuildSizesKHR(
        dev, VK_ACCELERATION_STRUCTURE_BUILD_TYPE_DEVICE_KHR, &info,
        counts.data(), &sizes);
    auto storage =
        buffer(sizes.accelerationStructureSize,
               VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_STORAGE_BIT_KHR);
    VkAccelerationStructureCreateInfoKHR ci{
        VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_CREATE_INFO_KHR};
    ci.buffer = storage.handle;
    ci.size = sizes.accelerationStructureSize;
    ci.type = type;
    VkAccelerationStructureKHR result;
    CHECK(vkCreateAccelerationStructureKHR(dev, &ci, nullptr, &result));
    structures.push_back(result);
    auto scratch = buffer(sizes.buildScratchSize + 4096,
                          VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
    info.dstAccelerationStructure = result;
    info.scratchData.deviceAddress =
        (scratch.address + 4095) & ~VkDeviceAddress(4095);
    auto cmd = begin();
    const auto *rp = ranges.data();
    vkCmdBuildAccelerationStructuresKHR(cmd, 1, &info, &rp);
    // Vulkan copies these host parameters during recording. Detect shallow
    // copies.
    for (auto &g : geoms)
      g.geometryType = VK_GEOMETRY_TYPE_MAX_ENUM_KHR;
    for (auto &r : ranges)
      r.primitiveCount = 0;
    barrier(cmd, VK_PIPELINE_STAGE_ACCELERATION_STRUCTURE_BUILD_BIT_KHR,
            VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
            VK_ACCESS_ACCELERATION_STRUCTURE_WRITE_BIT_KHR,
            VK_ACCESS_ACCELERATION_STRUCTURE_READ_BIT_KHR);
    submit(cmd);
    VkAccelerationStructureDeviceAddressInfoKHR addr{
        VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_DEVICE_ADDRESS_INFO_KHR};
    addr.accelerationStructure = result;
    return std::pair<VkAccelerationStructureKHR, VkDeviceAddress>(
        result, vkGetAccelerationStructureDeviceAddressKHR(dev, &addr));
  };
  auto vertices = buffer(
      12 * 3 * sizeof(float),
      VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR);
  auto indices = buffer(
      12 * sizeof(uint32_t),
      VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR);
  for (unsigned t = 0; t < 4; t++) {
    float x = float(t) * 2 - 3;
    float v[] = {x - .7f, -.7f, 0, x + .7f, -.7f, 0, x, .7f, 0};
    memcpy((float *)vertices.map + t * 9, v, sizeof(v));
    for (unsigned k = 0; k < 3; k++)
      ((uint32_t *)indices.map)[t * 3 + k] = t * 3 + k;
  }
  VkAccelerationStructureGeometryKHR geom{
      VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR};
  geom.geometryType = VK_GEOMETRY_TYPE_TRIANGLES_KHR;
  geom.flags = VK_GEOMETRY_OPAQUE_BIT_KHR;
  auto &tri = geom.geometry.triangles;
  tri.sType =
      VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_TRIANGLES_DATA_KHR;
  tri.vertexFormat = VK_FORMAT_R32G32B32_SFLOAT;
  tri.vertexData.deviceAddress = vertices.address;
  tri.vertexStride = 12;
  tri.maxVertex = 11;
  tri.indexType = VK_INDEX_TYPE_UINT32;
  tri.indexData.deviceAddress = indices.address;
  uint32_t secondOffset = 24;
  if (aabb) {
    auto boxes = buffer(
        4 * sizeof(VkAabbPositionsKHR),
        VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR);
    for (unsigned i = 0; i < 4; i++) {
      const float x = float(i) * 2 - 3;
      ((VkAabbPositionsKHR *)boxes.map)[i] = {x - .7f, -.7f, -.1f,
                                              x + .7f, .7f,  .1f};
    }
    geom.geometry = {};
    geom.geometryType = VK_GEOMETRY_TYPE_AABBS_KHR;
    geom.geometry.aabbs.sType =
        VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_AABBS_DATA_KHR;
    geom.geometry.aabbs.data.deviceAddress = boxes.address;
    geom.geometry.aabbs.stride = sizeof(VkAabbPositionsKHR);
    secondOffset = 2 * sizeof(VkAabbPositionsKHR);
  }
  auto blas =
      oneGeometry
          ? build(VK_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL_KHR, {geom},
                  {{4, 0, 0, 0}}, true)
          : build(VK_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL_KHR, {geom, geom},
                  {{2, 0, 0, 0}, {2, secondOffset, 0, 0}}, true);
  auto instances = buffer(
      2 * sizeof(VkAccelerationStructureInstanceKHR),
      VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR);
  for (unsigned i = 0; i < 2; i++) {
    auto &v = ((VkAccelerationStructureInstanceKHR *)instances.map)[i];
    v.transform.matrix[0][0] = v.transform.matrix[1][1] =
        v.transform.matrix[2][2] = 1;
    v.transform.matrix[1][3] = float(i) * 4;
    v.instanceCustomIndex = 7 + i * 4;
    v.mask = 255;
    v.flags = VK_GEOMETRY_INSTANCE_TRIANGLE_FACING_CULL_DISABLE_BIT_KHR;
    v.accelerationStructureReference = blas.second;
  }
  VkAccelerationStructureGeometryKHR ig{
      VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR};
  ig.geometryType = VK_GEOMETRY_TYPE_INSTANCES_KHR;
  ig.geometry.instances.sType =
      VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_INSTANCES_DATA_KHR;
  ig.geometry.instances.data.deviceAddress = instances.address;
  auto tlas = build(VK_ACCELERATION_STRUCTURE_TYPE_TOP_LEVEL_KHR, {ig},
                    {{2, 0, 0, 0}}, false);
  std::array<VkImage, 2> images;
  std::array<VkImageView, 2> views;
  std::array<VkDeviceMemory, 2> imageMemory;
  for (unsigned i = 0; i < 2; i++) {
    VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ci.imageType = VK_IMAGE_TYPE_2D;
    ci.format = VK_FORMAT_R8G8B8A8_UNORM;
    ci.extent = {6, 3, 1};
    ci.mipLevels = ci.arrayLayers = 1;
    ci.samples = VK_SAMPLE_COUNT_1_BIT;
    ci.usage = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT |
               VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    CHECK(vkCreateImage(dev, &ci, nullptr, &images[i]));
    VkMemoryRequirements req;
    vkGetImageMemoryRequirements(dev, images[i], &req);
    VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    ai.allocationSize = req.size;
    ai.memoryTypeIndex = memoryType(req.memoryTypeBits, 0);
    CHECK(vkAllocateMemory(dev, &ai, nullptr, &imageMemory[i]));
    CHECK(vkBindImageMemory(dev, images[i], imageMemory[i], 0));
    VkImageViewCreateInfo vi{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vi.image = images[i];
    vi.viewType = VK_IMAGE_VIEW_TYPE_2D;
    vi.format = ci.format;
    vi.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    CHECK(vkCreateImageView(dev, &vi, nullptr, &views[i]));
  }
  auto params = buffer(2048, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT);
  ((uint32_t *)params.map)[256 / 4] = 20;
  ((uint32_t *)params.map)[768 / 4] = 30;
  ((uint32_t *)params.map)[512 / 4] = 40;
  ((uint32_t *)params.map)[1024 / 4] = 30;
  auto results = buffer(512, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  auto readback = buffer(144, VK_BUFFER_USAGE_TRANSFER_DST_BIT);
  auto zero = buffer(72, VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
  std::array<VkDescriptorSetLayout, 4> layouts;
  const VkShaderStageFlags stages = VK_SHADER_STAGE_RAYGEN_BIT_KHR;
  std::vector<std::vector<VkDescriptorSetLayoutBinding>> bindings = {
      {{91, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2, stages, nullptr},
       {7, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 2, stages, nullptr}},
      {{73, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 2, stages, nullptr}},
      {{41, VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR, 1, stages, nullptr}},
      {{125, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 2, stages, nullptr}}};
  for (unsigned i = 0; i < 4; i++) {
    VkDescriptorSetLayoutCreateInfo ci{
        VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    ci.bindingCount = bindings[i].size();
    ci.pBindings = bindings[i].data();
    VkDescriptorSetLayoutSupport support{
        VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_SUPPORT};
    vkGetDescriptorSetLayoutSupport(dev, &ci, &support);
    if (!support.supported) {
      fprintf(stderr, "valid resource layout rejected\n");
      return 1;
    }
    CHECK(vkCreateDescriptorSetLayout(dev, &ci, nullptr, &layouts[i]));
  }
  VkPhysicalDeviceProperties limits;
  vkGetPhysicalDeviceProperties(gpu, &limits);
  if (limits.vendorID == 0x5654) {
    VkDescriptorSetLayoutBinding tooLarge{
        500, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 33, stages, nullptr};
    VkDescriptorSetLayoutCreateInfo ci{
        VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
    ci.bindingCount = 1;
    ci.pBindings = &tooLarge;
    VkDescriptorSetLayoutSupport support{
        VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_SUPPORT};
    vkGetDescriptorSetLayoutSupport(dev, &ci, &support);
    if (support.supported) {
      fprintf(stderr, "oversized resource layout accepted\n");
      return 1;
    }
    if (limits.limits.maxBoundDescriptorSets != 4 ||
        limits.limits.maxDescriptorSetUniformBuffersDynamic < 2)
      return 1;
  }
  VkPushConstantRange push{stages, 0, 4};
  VkPipelineLayoutCreateInfo lci{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
  lci.setLayoutCount = 4;
  lci.pSetLayouts = layouts.data();
  lci.pushConstantRangeCount = 1;
  lci.pPushConstantRanges = &push;
  VkPipelineLayout layout;
  CHECK(vkCreatePipelineLayout(dev, &lci, nullptr, &layout));
  VkDescriptorPoolSize poolSizes[] = {
      {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 4},
      {VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 2},
      {VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR, 1},
      {VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC, 2}};
  VkDescriptorPoolCreateInfo dpci{
      VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
  dpci.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
  dpci.maxSets = 4;
  dpci.poolSizeCount = 4;
  dpci.pPoolSizes = poolSizes;
  VkDescriptorPool descriptorPool;
  CHECK(vkCreateDescriptorPool(dev, &dpci, nullptr, &descriptorPool));
  std::array<VkDescriptorSet, 4> sets;
  VkDescriptorSetAllocateInfo dai{
      VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
  dai.descriptorPool = descriptorPool;
  dai.descriptorSetCount = 4;
  dai.pSetLayouts = layouts.data();
  CHECK(vkAllocateDescriptorSets(dev, &dai, sets.data()));
  VkDescriptorBufferInfo outInfo[] = {{results.handle, 0, 32},
                                      {results.handle, 128, 32}},
                         paramInfo[] = {{params.handle, 0, 16},
                                        {params.handle, 0, 16}};
  VkDescriptorImageInfo imageInfo[] = {
      {VK_NULL_HANDLE, views[0], VK_IMAGE_LAYOUT_GENERAL},
      {VK_NULL_HANDLE, views[1], VK_IMAGE_LAYOUT_GENERAL}};
  VkWriteDescriptorSetAccelerationStructureKHR asi{
      VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET_ACCELERATION_STRUCTURE_KHR};
  asi.accelerationStructureCount = 1;
  asi.pAccelerationStructures = &tlas.first;
  std::array<VkWriteDescriptorSet, 4> writes{};
  for (unsigned i = 0; i < 4; i++) {
    writes[i].sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    writes[i].dstSet = sets[i];
    writes[i].descriptorCount = i == 2 ? 1 : 2;
  }
  writes[0].dstBinding = 7;
  writes[0].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
  writes[0].pBufferInfo = outInfo;
  writes[1].dstBinding = 73;
  writes[1].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_IMAGE;
  writes[1].pImageInfo = imageInfo;
  writes[2].dstBinding = 41;
  writes[2].descriptorType = VK_DESCRIPTOR_TYPE_ACCELERATION_STRUCTURE_KHR;
  writes[2].pNext = &asi;
  writes[3].dstBinding = 125;
  writes[3].descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC;
  writes[3].pBufferInfo = paramInfo;
  vkUpdateDescriptorSets(dev, 4, writes.data(), 0, nullptr);
  VkCopyDescriptorSet copy{VK_STRUCTURE_TYPE_COPY_DESCRIPTOR_SET};
  copy.srcSet = sets[0];
  copy.dstSet = sets[0];
  copy.srcBinding = 7;
  copy.dstBinding = 91;
  copy.descriptorCount = 2;
  vkUpdateDescriptorSets(dev, 0, nullptr, 1, &copy);
  const unsigned shaderCount = aabb ? 4 : 3;
  std::vector<VkShaderModule> modules(shaderCount);
  std::vector<VkPipelineShaderStageCreateInfo> shaderStages(shaderCount);
  std::array<VkRayTracingShaderGroupCreateInfoKHR, 3> groups{};
  const char *names[] = {"raygen.rgen.spv", "miss.rmiss.spv",
                         "closesthit.rchit.spv", "intersection.rint.spv"};
  VkShaderStageFlagBits stageBits[] = {VK_SHADER_STAGE_RAYGEN_BIT_KHR,
                                       VK_SHADER_STAGE_MISS_BIT_KHR,
                                       VK_SHADER_STAGE_CLOSEST_HIT_BIT_KHR,
                                       VK_SHADER_STAGE_INTERSECTION_BIT_KHR};
  for (unsigned i = 0; i < shaderCount; i++) {
    std::ifstream f(std::string(argv[1]) + "/" + names[i],
                    std::ios::binary | std::ios::ate);
    if (!f) {
      fprintf(stderr, "missing shader %s\n", names[i]);
      return 1;
    }
    size_t size = f.tellg();
    std::vector<uint32_t> code(size / 4);
    f.seekg(0);
    f.read((char *)code.data(), size);
    VkShaderModuleCreateInfo ci{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
    ci.codeSize = size;
    ci.pCode = code.data();
    CHECK(vkCreateShaderModule(dev, &ci, nullptr, &modules[i]));
    shaderStages[i].sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    shaderStages[i].stage = stageBits[i];
    shaderStages[i].module = modules[i];
    shaderStages[i].pName = "main";
    if (i >= 3)
      continue;
    groups[i].sType =
        VK_STRUCTURE_TYPE_RAY_TRACING_SHADER_GROUP_CREATE_INFO_KHR;
    groups[i].type =
        i == 2 ? VK_RAY_TRACING_SHADER_GROUP_TYPE_TRIANGLES_HIT_GROUP_KHR
               : VK_RAY_TRACING_SHADER_GROUP_TYPE_GENERAL_KHR;
    groups[i].generalShader = groups[i].closestHitShader =
        groups[i].anyHitShader = groups[i].intersectionShader =
            VK_SHADER_UNUSED_KHR;
    if (i == 2)
      groups[i].closestHitShader = i;
    else
      groups[i].generalShader = i;
  }
  VkRayTracingPipelineCreateInfoKHR rtci{
      VK_STRUCTURE_TYPE_RAY_TRACING_PIPELINE_CREATE_INFO_KHR};
  if (aabb) {
    groups[2].type = VK_RAY_TRACING_SHADER_GROUP_TYPE_PROCEDURAL_HIT_GROUP_KHR;
    groups[2].intersectionShader = 3;
  }
  rtci.stageCount = shaderCount;
  rtci.pStages = shaderStages.data();
  rtci.groupCount = 3;
  rtci.pGroups = groups.data();
  rtci.maxPipelineRayRecursionDepth = 1;
  rtci.layout = layout;
  VkPipeline pipeline;
  CHECK(vkCreateRayTracingPipelinesKHR(dev, VK_NULL_HANDLE, VK_NULL_HANDLE, 1,
                                       &rtci, nullptr, &pipeline));
  VkPhysicalDeviceRayTracingPipelinePropertiesKHR props{
      VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RAY_TRACING_PIPELINE_PROPERTIES_KHR};
  VkPhysicalDeviceProperties2 props2{
      VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2};
  props2.pNext = &props;
  vkGetPhysicalDeviceProperties2(gpu, &props2);
  uint32_t stride =
      (props.shaderGroupHandleSize + props.shaderGroupBaseAlignment - 1) &
      ~(props.shaderGroupBaseAlignment - 1);
  auto sbt = buffer(stride * 3 + props.shaderGroupBaseAlignment,
                    VK_BUFFER_USAGE_SHADER_BINDING_TABLE_BIT_KHR);
  VkDeviceAddress sbtBase =
      (sbt.address + props.shaderGroupBaseAlignment - 1) &
      ~VkDeviceAddress(props.shaderGroupBaseAlignment - 1);
  std::vector<uint8_t> handles(props.shaderGroupHandleSize * 3);
  CHECK(vkGetRayTracingShaderGroupHandlesKHR(dev, pipeline, 0, 3,
                                             handles.size(), handles.data()));
  for (unsigned i = 0; i < 3; i++)
    memcpy((uint8_t *)sbt.map + (sbtBase - sbt.address) + i * stride,
           handles.data() + i * props.shaderGroupHandleSize,
           props.shaderGroupHandleSize);
  VkStridedDeviceAddressRegionKHR rg{sbtBase, stride, stride},
      ms{sbtBase + stride, stride, stride},
      ch{sbtBase + stride * 2, stride, stride}, empty{};
  auto cmd = begin();
  for (unsigned i = 0; i < 2; i++) {
    VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.image = images[i];
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    b.newLayout = VK_IMAGE_LAYOUT_GENERAL;
    b.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0,
                         nullptr, 1, &b);
    VkBufferImageCopy c{};
    c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    c.imageExtent = {6, 3, 1};
    vkCmdCopyBufferToImage(cmd, zero.handle, images[i], VK_IMAGE_LAYOUT_GENERAL,
                           1, &c);
  }
  barrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT,
          VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
          VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_SHADER_WRITE_BIT);
  vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR, pipeline);
  // Bind a nonzero firstSet, then lower sets separately; the higher set
  // survives.
  uint32_t offsets[] = {256, 768};
  vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR, layout,
                          3, 1, &sets[3], 2, offsets);
  vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR, layout,
                          0, 3, sets.data(), 0, nullptr);
  uint32_t target = 1;
  vkCmdPushConstants(cmd, layout, stages, 0, 4, &target);
  vkCmdTraceRaysKHR(cmd, &rg, &ms, &ch, &empty, 4, 2, 1);
  if (rebind) {
    barrier(cmd, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
            VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
            VK_ACCESS_SHADER_WRITE_BIT,
            VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT);
    offsets[0] = 512;
    offsets[1] = 1024;
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_RAY_TRACING_KHR, layout,
                            3, 1, &sets[3], 2, offsets);
    target = 0;
    vkCmdPushConstants(cmd, layout, stages, 0, 4, &target);
    vkCmdTraceRaysKHR(cmd, &rg, &ms, &ch, &empty, 4, 2, 1);
  }
  barrier(cmd, VK_PIPELINE_STAGE_RAY_TRACING_SHADER_BIT_KHR,
          VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_HOST_BIT,
          VK_ACCESS_SHADER_WRITE_BIT,
          VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_HOST_READ_BIT);
  for (unsigned i = 0; i < 2; i++) {
    VkBufferImageCopy c{};
    c.bufferOffset = i * 72;
    c.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    c.imageExtent = {6, 3, 1};
    vkCmdCopyImageToBuffer(cmd, images[i], VK_IMAGE_LAYOUT_GENERAL,
                           readback.handle, 1, &c);
  }
  barrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
          VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_HOST_READ_BIT);
  submit(cmd);
  unsigned failures = 0;
  auto *pixels = (uint8_t *)readback.map;
  for (unsigned i = 0; i < 2; i++)
    for (unsigned y = 0; y < 3; y++)
      for (unsigned x = 0; x < 6; x++) {
        uint8_t expected[4] = {0, 0, 0, 0};
        if ((i == 1 || rebind) && x < 4 && y < 2) {
          expected[0] = (oneGeometry ? 1 : x / 2 + 1) * 40;
          expected[1] = (oneGeometry ? x + 1 : x % 2 + 1) * 50;
          expected[2] = 57 + y * 4 + (i == 0 ? 20 : 0);
          expected[3] = 255;
        }
        auto *actual = pixels + i * 72 + (y * 6 + x) * 4;
        if (memcmp(actual, expected, 4)) {
          fprintf(stderr,
                  "image %u (%u,%u): [%u,%u,%u,%u], expected [%u,%u,%u,%u]\n",
                  i, x, y, actual[0], actual[1], actual[2], actual[3],
                  expected[0], expected[1], expected[2], expected[3]);
          failures++;
        }
      }
  for (unsigned i = 0; i < 2; i++)
    for (unsigned n = 0; n < 8; n++) {
      uint32_t expected =
          (i || rebind)
              ? (oneGeometry ? 1 : (n % 4) / 2 + 1) * 1000 +
                    (oneGeometry ? (n % 4) + 1 : (n % 4) % 2 + 1) * 100 + 57 +
                    (n / 4) * 4 + (i == 0 ? 20 : 0)
              : 0;
      uint32_t actual = ((uint32_t *)results.map)[i * 32 + n];
      if (actual != expected) {
        fprintf(stderr, "buffer %u[%u]: %u expected %u\n", i, n, actual,
                expected);
        failures++;
      }
    }
  CHECK(vkDeviceWaitIdle(dev));
  vkDestroyPipeline(dev, pipeline, nullptr);
  for (auto m : modules)
    vkDestroyShaderModule(dev, m, nullptr);
  CHECK(vkFreeDescriptorSets(dev, descriptorPool, 4, sets.data()));
  vkDestroyDescriptorPool(dev, descriptorPool, nullptr);
  vkDestroyPipelineLayout(dev, layout, nullptr);
  for (auto l : layouts)
    vkDestroyDescriptorSetLayout(dev, l, nullptr);
  for (auto a : structures)
    vkDestroyAccelerationStructureKHR(dev, a, nullptr);
  for (unsigned i = 0; i < 2; i++) {
    vkDestroyImageView(dev, views[i], nullptr);
    vkDestroyImage(dev, images[i], nullptr);
    vkFreeMemory(dev, imageMemory[i], nullptr);
  }
  for (auto b : buffers) {
    vkUnmapMemory(dev, b.memory);
    vkDestroyBuffer(dev, b.handle, nullptr);
    vkFreeMemory(dev, b.memory, nullptr);
  }
  vkDestroyCommandPool(dev, pool, nullptr);
  vkDestroyDevice(dev, nullptr);
  vkDestroyInstance(instance, nullptr);
  printf("%s: sparse sets/bindings, arrays/copies, dynamic offsets, image "
         "extent/readback, SSBO aliases, geometry/primitive IDs, BLAS reuse\n",
         failures ? "FAIL" : "PASS");
  return failures ? 1 : 0;
}
