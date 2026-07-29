#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <vulkan/vulkan.h>

#include "ventus/common/ventus_rt_bvh.h"

struct probe_buffer {
   VkBuffer buffer;
   VkDeviceMemory memory;
   VkDeviceAddress address;
   VkDeviceSize size;
};

struct as_functions {
   PFN_vkCreateAccelerationStructureKHR create;
   PFN_vkDestroyAccelerationStructureKHR destroy;
   PFN_vkGetAccelerationStructureBuildSizesKHR get_sizes;
   PFN_vkCmdBuildAccelerationStructuresKHR cmd_build;
   PFN_vkGetAccelerationStructureDeviceAddressKHR get_address;
};

static void
fail(const char *message)
{
   fprintf(stderr, "error: %s\n", message);
   exit(EXIT_FAILURE);
}

static void
require_result(VkResult result, const char *operation)
{
   if (result == VK_SUCCESS)
      return;

   fprintf(stderr, "error: %s failed with VkResult %d\n", operation, result);
   exit(EXIT_FAILURE);
}

static uint32_t
load_u32(const uint8_t *base, uint32_t offset)
{
   uint32_t value;
   memcpy(&value, base + offset, sizeof(value));
   return value;
}

static uint64_t
load_u64(const uint8_t *base, uint32_t offset)
{
   uint64_t value;
   memcpy(&value, base + offset, sizeof(value));
   return value;
}

static float
load_f32(const uint8_t *base, uint32_t offset)
{
   float value;
   memcpy(&value, base + offset, sizeof(value));
   return value;
}

static void
require_f32(float actual, float expected, const char *name)
{
   if (fabsf(actual - expected) <= 1.0e-6f)
      return;

   fprintf(stderr, "error: %s=%g, expected %g\n", name, actual, expected);
   exit(EXIT_FAILURE);
}

static uint32_t
find_queue_family(VkPhysicalDevice physical_device)
{
   uint32_t count = 0;
   vkGetPhysicalDeviceQueueFamilyProperties(physical_device, &count, NULL);
   if (!count)
      fail("physical device exposes no queue families");

   VkQueueFamilyProperties *properties = calloc(count, sizeof(*properties));
   if (!properties)
      fail("failed to allocate queue family properties");
   vkGetPhysicalDeviceQueueFamilyProperties(physical_device, &count,
                                            properties);

   for (uint32_t i = 0; i < count; i++) {
      if (properties[i].queueCount) {
         free(properties);
         return i;
      }
   }

   free(properties);
   fail("physical device exposes no usable queue family");
   return 0;
}

static uint32_t
find_memory_type(VkPhysicalDevice physical_device, uint32_t type_bits,
                 VkMemoryPropertyFlags required)
{
   VkPhysicalDeviceMemoryProperties properties;
   vkGetPhysicalDeviceMemoryProperties(physical_device, &properties);
   for (uint32_t i = 0; i < properties.memoryTypeCount; i++) {
      if ((type_bits & (1u << i)) &&
          (properties.memoryTypes[i].propertyFlags & required) == required)
         return i;
   }

   fail("no compatible host-visible memory type");
   return 0;
}

static void
create_buffer(VkPhysicalDevice physical_device, VkDevice device,
              VkDeviceSize size, VkBufferUsageFlags usage,
              struct probe_buffer *out)
{
   const VkBufferCreateInfo buffer_info = {
      .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
      .size = size,
      .usage = usage | VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT,
      .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
   };
   require_result(vkCreateBuffer(device, &buffer_info, NULL, &out->buffer),
                  "vkCreateBuffer");

   VkMemoryRequirements requirements;
   vkGetBufferMemoryRequirements(device, out->buffer, &requirements);
   const VkMemoryAllocateFlagsInfo flags = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO,
      .flags = VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT,
   };
   const VkMemoryAllocateInfo allocation_info = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
      .pNext = &flags,
      .allocationSize = requirements.size,
      .memoryTypeIndex = find_memory_type(
         physical_device, requirements.memoryTypeBits,
         VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT |
         VK_MEMORY_PROPERTY_HOST_COHERENT_BIT),
   };
   require_result(vkAllocateMemory(device, &allocation_info, NULL,
                                   &out->memory),
                  "vkAllocateMemory");
   require_result(vkBindBufferMemory(device, out->buffer, out->memory, 0),
                  "vkBindBufferMemory");

   const VkBufferDeviceAddressInfo address_info = {
      .sType = VK_STRUCTURE_TYPE_BUFFER_DEVICE_ADDRESS_INFO,
      .buffer = out->buffer,
   };
   out->address = vkGetBufferDeviceAddress(device, &address_info);
   out->size = size;
   if (!out->address)
      fail("vkGetBufferDeviceAddress returned zero");
}

static void
upload_buffer(VkDevice device, const struct probe_buffer *buffer,
              const void *source, size_t size)
{
   if (size > buffer->size)
      fail("upload exceeds destination buffer");

   void *mapping = NULL;
   require_result(vkMapMemory(device, buffer->memory, 0, buffer->size, 0,
                              &mapping),
                  "vkMapMemory");
   memcpy(mapping, source, size);
   vkUnmapMemory(device, buffer->memory);
}

static void
destroy_buffer(VkDevice device, struct probe_buffer *buffer)
{
   if (buffer->buffer)
      vkDestroyBuffer(device, buffer->buffer, NULL);
   if (buffer->memory)
      vkFreeMemory(device, buffer->memory, NULL);
   memset(buffer, 0, sizeof(*buffer));
}

static void
load_as_functions(VkDevice device, struct as_functions *functions)
{
#define LOAD_FUNCTION(member, name)                                           \
   do {                                                                        \
      functions->member = (PFN_##name)vkGetDeviceProcAddr(device, #name);      \
      if (!functions->member)                                                  \
         fail("missing device function " #name);                               \
   } while (0)
   LOAD_FUNCTION(create, vkCreateAccelerationStructureKHR);
   LOAD_FUNCTION(destroy, vkDestroyAccelerationStructureKHR);
   LOAD_FUNCTION(get_sizes, vkGetAccelerationStructureBuildSizesKHR);
   LOAD_FUNCTION(cmd_build, vkCmdBuildAccelerationStructuresKHR);
   LOAD_FUNCTION(get_address, vkGetAccelerationStructureDeviceAddressKHR);
#undef LOAD_FUNCTION
}

static void
collect_triangle_offsets(const uint8_t *as_data, uint32_t ref,
                         uint32_t offsets[4], uint32_t *count)
{
   if (ref == VENTUS_AS_INVALID_NODE)
      return;

   const uint32_t type = ref & VENTUS_NODE_REF_TYPE_MASK;
   const uint32_t offset = ref & VENTUS_NODE_REF_OFFSET_MASK;
   if (type == VENTUS_BVH_NODE_TRIANGLE) {
      if (*count >= 4)
         fail("unexpectedly many triangle leaves");
      offsets[(*count)++] = offset;
      return;
   }
   if (type != VENTUS_BVH_NODE_BOX4)
      fail("unexpected node type in triangle BLAS");

   for (uint32_t child = 0; child < 4; child++) {
      collect_triangle_offsets(
         as_data,
         load_u32(as_data + offset, VENTUS_BOX4_CHILD_REF + child * 4),
         offsets, count);
   }
}

static void
verify_triangle_leaf(const uint8_t *as_data, uint32_t offset,
                     uint32_t geometry_id, uint32_t expected_flags,
                     uint64_t expected_primitive_address,
                     const float expected_v0[3])
{
   const uint8_t *leaf = as_data + offset;
   if (load_u32(leaf, VENTUS_TRIANGLE_PRIMITIVE_ID) != 0)
      fail("primitive id is not local to its geometry");
   if (load_u32(leaf, VENTUS_TRIANGLE_GEOMETRY_ID) != geometry_id)
      fail("triangle geometry id does not match pGeometries index");
   if (load_u32(leaf, VENTUS_TRIANGLE_SBT_RECORD_OFFSET) != 0)
      fail("triangle leaf contains a nonzero static SBT contribution");
   if (load_u32(leaf, VENTUS_TRIANGLE_FLAGS) != expected_flags)
      fail("triangle geometry flags were not preserved");
   if (load_u64(leaf, VENTUS_TRIANGLE_PRIMITIVE_ADDR_LO) !=
       expected_primitive_address)
      fail("triangle primitive source address does not match its range");

   require_f32(load_f32(leaf, VENTUS_TRIANGLE_V0 + 0), expected_v0[0],
               "triangle.v0.x");
   require_f32(load_f32(leaf, VENTUS_TRIANGLE_V0 + 4), expected_v0[1],
               "triangle.v0.y");
   require_f32(load_f32(leaf, VENTUS_TRIANGLE_V0 + 8), expected_v0[2],
               "triangle.v0.z");
}

int
main(void)
{
   const VkApplicationInfo application_info = {
      .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
      .pApplicationName = "ventus-multi-geometry-blas-probe",
      .applicationVersion = 1,
      .pEngineName = "none",
      .engineVersion = 1,
      .apiVersion = VK_API_VERSION_1_2,
   };
   const VkInstanceCreateInfo instance_info = {
      .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
      .pApplicationInfo = &application_info,
   };

   VkInstance instance = VK_NULL_HANDLE;
   require_result(vkCreateInstance(&instance_info, NULL, &instance),
                  "vkCreateInstance");

   uint32_t physical_device_count = 0;
   require_result(vkEnumeratePhysicalDevices(instance, &physical_device_count,
                                             NULL),
                  "vkEnumeratePhysicalDevices(count)");
   if (physical_device_count != 1)
      fail("isolated Ventus ICD must expose exactly one physical device");
   VkPhysicalDevice physical_device = VK_NULL_HANDLE;
   require_result(vkEnumeratePhysicalDevices(instance, &physical_device_count,
                                             &physical_device),
                  "vkEnumeratePhysicalDevices(data)");

   const uint32_t queue_family = find_queue_family(physical_device);
   const float queue_priority = 1.0f;
   const VkDeviceQueueCreateInfo queue_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
      .queueFamilyIndex = queue_family,
      .queueCount = 1,
      .pQueuePriorities = &queue_priority,
   };
   VkPhysicalDeviceBufferDeviceAddressFeatures address_features = {
      .sType =
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_BUFFER_DEVICE_ADDRESS_FEATURES,
      .bufferDeviceAddress = VK_TRUE,
   };
   VkPhysicalDeviceAccelerationStructureFeaturesKHR as_features = {
      .sType =
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
      .pNext = &address_features,
      .accelerationStructure = VK_TRUE,
   };
   const char *extensions[] = {
      VK_KHR_ACCELERATION_STRUCTURE_EXTENSION_NAME,
      VK_KHR_BUFFER_DEVICE_ADDRESS_EXTENSION_NAME,
      VK_KHR_DEFERRED_HOST_OPERATIONS_EXTENSION_NAME,
   };
   const VkDeviceCreateInfo device_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
      .pNext = &as_features,
      .queueCreateInfoCount = 1,
      .pQueueCreateInfos = &queue_info,
      .enabledExtensionCount =
         (uint32_t)(sizeof(extensions) / sizeof(extensions[0])),
      .ppEnabledExtensionNames = extensions,
   };

   VkDevice device = VK_NULL_HANDLE;
   require_result(vkCreateDevice(physical_device, &device_info, NULL, &device),
                  "vkCreateDevice");
   struct as_functions as_functions;
   load_as_functions(device, &as_functions);

   const float vertices[6][3] = {
      { -1.0f, -1.0f, 5.0f },
      { 1.0f, -1.0f, 5.0f },
      { 0.0f, 1.0f, 5.0f },
      { -0.5f, -0.5f, 8.0f },
      { 0.5f, -0.5f, 8.0f },
      { 0.0f, 0.5f, 8.0f },
   };
   const uint32_t indices[6] = { 0, 1, 2, 3, 4, 5 };
   const VkTransformMatrixKHR transforms[2] = {
      { .matrix = {
           { 1.0f, 0.0f, 0.0f, 0.0f },
           { 0.0f, 1.0f, 0.0f, 0.0f },
           { 0.0f, 0.0f, 1.0f, 0.0f },
        } },
      { .matrix = {
           { 1.0f, 0.0f, 0.0f, 2.0f },
           { 0.0f, 1.0f, 0.0f, 0.0f },
           { 0.0f, 0.0f, 1.0f, 0.0f },
        } },
   };

   struct probe_buffer vertex_buffer = { 0 };
   struct probe_buffer index_buffer = { 0 };
   struct probe_buffer transform_buffer = { 0 };
   create_buffer(physical_device, device, sizeof(vertices),
                 VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR,
                 &vertex_buffer);
   create_buffer(physical_device, device, sizeof(indices),
                 VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR,
                 &index_buffer);
   create_buffer(physical_device, device, sizeof(transforms),
                 VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_BUILD_INPUT_READ_ONLY_BIT_KHR,
                 &transform_buffer);
   upload_buffer(device, &vertex_buffer, vertices, sizeof(vertices));
   upload_buffer(device, &index_buffer, indices, sizeof(indices));
   upload_buffer(device, &transform_buffer, transforms, sizeof(transforms));

   VkAccelerationStructureGeometryKHR geometries[2];
   memset(geometries, 0, sizeof(geometries));
   for (uint32_t i = 0; i < 2; i++) {
      geometries[i].sType =
         VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_KHR;
      geometries[i].geometryType = VK_GEOMETRY_TYPE_TRIANGLES_KHR;
      geometries[i].flags = i == 0 ? VK_GEOMETRY_OPAQUE_BIT_KHR : 0;
      geometries[i].geometry.triangles.sType =
         VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_GEOMETRY_TRIANGLES_DATA_KHR;
      geometries[i].geometry.triangles.vertexFormat =
         VK_FORMAT_R32G32B32_SFLOAT;
      geometries[i].geometry.triangles.vertexData.deviceAddress =
         vertex_buffer.address;
      geometries[i].geometry.triangles.vertexStride = sizeof(vertices[0]);
      geometries[i].geometry.triangles.maxVertex = 5;
      geometries[i].geometry.triangles.indexType = VK_INDEX_TYPE_UINT32;
      geometries[i].geometry.triangles.indexData.deviceAddress =
         index_buffer.address;
      geometries[i].geometry.triangles.transformData.deviceAddress =
         transform_buffer.address;
   }
   const uint32_t primitive_counts[2] = { 1, 1 };
   const VkAccelerationStructureBuildGeometryInfoKHR size_info = {
      .sType =
         VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_GEOMETRY_INFO_KHR,
      .type = VK_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL_KHR,
      .flags = VK_BUILD_ACCELERATION_STRUCTURE_PREFER_FAST_TRACE_BIT_KHR,
      .geometryCount = 2,
      .pGeometries = geometries,
   };
   VkAccelerationStructureBuildSizesInfoKHR multi_sizes = {
      .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_SIZES_INFO_KHR,
   };
   as_functions.get_sizes(
      device, VK_ACCELERATION_STRUCTURE_BUILD_TYPE_DEVICE_KHR, &size_info,
      primitive_counts, &multi_sizes);

   VkAccelerationStructureBuildGeometryInfoKHR single_size_info = size_info;
   single_size_info.geometryCount = 1;
   VkAccelerationStructureBuildSizesInfoKHR single_sizes = {
      .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_BUILD_SIZES_INFO_KHR,
   };
   as_functions.get_sizes(
      device, VK_ACCELERATION_STRUCTURE_BUILD_TYPE_DEVICE_KHR,
      &single_size_info, primitive_counts, &single_sizes);
   if (multi_sizes.accelerationStructureSize <=
       single_sizes.accelerationStructureSize)
      fail("multi-geometry size query ignored geometry one");

   struct probe_buffer as_buffer = { 0 };
   struct probe_buffer scratch_buffer = { 0 };
   create_buffer(physical_device, device, multi_sizes.accelerationStructureSize,
                 VK_BUFFER_USAGE_ACCELERATION_STRUCTURE_STORAGE_BIT_KHR,
                 &as_buffer);
   create_buffer(physical_device, device, multi_sizes.buildScratchSize,
                 VK_BUFFER_USAGE_STORAGE_BUFFER_BIT, &scratch_buffer);

   const VkAccelerationStructureCreateInfoKHR create_info = {
      .sType = VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_CREATE_INFO_KHR,
      .buffer = as_buffer.buffer,
      .size = multi_sizes.accelerationStructureSize,
      .type = VK_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL_KHR,
   };
   VkAccelerationStructureKHR acceleration_structure = VK_NULL_HANDLE;
   require_result(as_functions.create(device, &create_info, NULL,
                                      &acceleration_structure),
                  "vkCreateAccelerationStructureKHR");

   const VkCommandPoolCreateInfo pool_info = {
      .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
      .queueFamilyIndex = queue_family,
   };
   VkCommandPool command_pool = VK_NULL_HANDLE;
   require_result(vkCreateCommandPool(device, &pool_info, NULL, &command_pool),
                  "vkCreateCommandPool");
   const VkCommandBufferAllocateInfo command_buffer_info = {
      .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
      .commandPool = command_pool,
      .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
      .commandBufferCount = 1,
   };
   VkCommandBuffer command_buffer = VK_NULL_HANDLE;
   require_result(vkAllocateCommandBuffers(device, &command_buffer_info,
                                           &command_buffer),
                  "vkAllocateCommandBuffers");
   const VkCommandBufferBeginInfo begin_info = {
      .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
      .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT,
   };
   require_result(vkBeginCommandBuffer(command_buffer, &begin_info),
                  "vkBeginCommandBuffer");

   VkAccelerationStructureBuildGeometryInfoKHR build_info = size_info;
   build_info.mode = VK_BUILD_ACCELERATION_STRUCTURE_MODE_BUILD_KHR;
   build_info.dstAccelerationStructure = acceleration_structure;
   build_info.scratchData.deviceAddress = scratch_buffer.address;
   const VkAccelerationStructureBuildRangeInfoKHR ranges[2] = {
      {
         .primitiveCount = 1,
         .primitiveOffset = 0,
         .firstVertex = 0,
         .transformOffset = 0,
      },
      {
         .primitiveCount = 1,
         .primitiveOffset = 3 * sizeof(uint32_t),
         .firstVertex = 0,
         .transformOffset = sizeof(VkTransformMatrixKHR),
      },
   };
   const VkAccelerationStructureBuildRangeInfoKHR *range_array = ranges;
   as_functions.cmd_build(command_buffer, 1, &build_info, &range_array);
   require_result(vkEndCommandBuffer(command_buffer), "vkEndCommandBuffer");

   VkQueue queue = VK_NULL_HANDLE;
   vkGetDeviceQueue(device, queue_family, 0, &queue);
   const VkSubmitInfo submit_info = {
      .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
      .commandBufferCount = 1,
      .pCommandBuffers = &command_buffer,
   };
   require_result(vkQueueSubmit(queue, 1, &submit_info, VK_NULL_HANDLE),
                  "vkQueueSubmit");
   require_result(vkQueueWaitIdle(queue), "vkQueueWaitIdle");

   const VkAccelerationStructureDeviceAddressInfoKHR address_info = {
      .sType =
         VK_STRUCTURE_TYPE_ACCELERATION_STRUCTURE_DEVICE_ADDRESS_INFO_KHR,
      .accelerationStructure = acceleration_structure,
   };
   const VkDeviceAddress as_address =
      as_functions.get_address(device, &address_info);
   if (!as_address)
      fail("built acceleration structure has no address");
   const uint8_t *as_data = (const uint8_t *)(uintptr_t)as_address;
   if (load_u32(as_data, VENTUS_AS_HEADER_MAGIC) != VENTUS_AS_MAGIC)
      fail("built object does not contain a VTAS header");
   if (load_u32(as_data, VENTUS_AS_HEADER_TYPE) != VENTUS_AS_TYPE_BLAS)
      fail("built VTAS object is not a BLAS");
   if (load_u32(as_data, VENTUS_AS_HEADER_PRIMITIVE_COUNT) != 2)
      fail("VTAS header did not aggregate both geometry primitive counts");
   if (load_u32(as_data, VENTUS_AS_HEADER_NODE_COUNT) != 3)
      fail("two-leaf BLAS does not contain one internal box node");

   uint32_t leaf_offsets[4] = { 0 };
   uint32_t leaf_count = 0;
   collect_triangle_offsets(
      as_data, load_u32(as_data, VENTUS_AS_HEADER_ROOT_NODE_REF),
      leaf_offsets, &leaf_count);
   if (leaf_count != 2)
      fail("VTAS tree does not expose two triangle leaves");

   uint32_t geometry_offsets[2] = { 0 };
   for (uint32_t i = 0; i < leaf_count; i++) {
      const uint32_t geometry_id = load_u32(
         as_data + leaf_offsets[i], VENTUS_TRIANGLE_GEOMETRY_ID);
      if (geometry_id >= 2)
         fail("VTAS leaf has an out-of-range geometry id");
      geometry_offsets[geometry_id] = leaf_offsets[i];
   }
   if (!geometry_offsets[0] || !geometry_offsets[1])
      fail("VTAS leaves do not cover geometry ids zero and one");

   const float geometry0_v0[3] = { -1.0f, -1.0f, 5.0f };
   const float geometry1_v0[3] = { 1.5f, -0.5f, 8.0f };
   verify_triangle_leaf(
      as_data, geometry_offsets[0], 0, 1, index_buffer.address,
      geometry0_v0);
   verify_triangle_leaf(
      as_data, geometry_offsets[1], 1, 0,
      index_buffer.address + 3 * sizeof(uint32_t), geometry1_v0);
   require_f32(load_f32(as_data, VENTUS_AS_HEADER_ROOT_AABB_MIN_X), -1.0f,
               "root_aabb.min.x");
   require_f32(load_f32(as_data, VENTUS_AS_HEADER_ROOT_AABB_MAX_X), 2.5f,
               "root_aabb.max.x");

   printf("PASS multi-geometry-blas geometries=2 primitives=2 nodes=3 "
          "transform_offset=%zu\n",
          sizeof(VkTransformMatrixKHR));

   vkDeviceWaitIdle(device);
   vkDestroyCommandPool(device, command_pool, NULL);
   as_functions.destroy(device, acceleration_structure, NULL);
   destroy_buffer(device, &scratch_buffer);
   destroy_buffer(device, &as_buffer);
   destroy_buffer(device, &transform_buffer);
   destroy_buffer(device, &index_buffer);
   destroy_buffer(device, &vertex_buffer);
   vkDestroyDevice(device, NULL);
   vkDestroyInstance(instance, NULL);
   return EXIT_SUCCESS;
}
