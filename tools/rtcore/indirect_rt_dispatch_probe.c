#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include <vulkan/vulkan.h>

struct address_buffer {
  VkBuffer buffer;
  VkDeviceMemory memory;
  VkDeviceAddress address;
};

static void fail(const char *message) {
  fprintf(stderr, "error: %s\n", message);
  exit(EXIT_FAILURE);
}

static void require_result(VkResult result, const char *operation) {
  if (result == VK_SUCCESS)
    return;

  fprintf(stderr, "error: %s failed with VkResult %d\n", operation, result);
  exit(EXIT_FAILURE);
}

static uint32_t find_queue_family(VkPhysicalDevice physical_device) {
  uint32_t count = 0;
  vkGetPhysicalDeviceQueueFamilyProperties(physical_device, &count, NULL);
  if (count == 0)
    fail("physical device exposes no queue families");

  VkQueueFamilyProperties *properties = calloc(count, sizeof(*properties));
  if (!properties)
    fail("failed to allocate queue family properties");

  vkGetPhysicalDeviceQueueFamilyProperties(physical_device, &count, properties);
  for (uint32_t i = 0; i < count; i++) {
    if (properties[i].queueCount > 0) {
      free(properties);
      return i;
    }
  }

  free(properties);
  fail("physical device exposes no usable queue family");
  return 0;
}

static struct address_buffer create_address_buffer(VkDevice device,
                                                   VkDeviceSize size,
                                                   VkBufferUsageFlags usage) {
  struct address_buffer result = {0};
  const VkBufferCreateInfo buffer_info = {
      .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
      .size = size,
      .usage = usage | VK_BUFFER_USAGE_SHADER_DEVICE_ADDRESS_BIT,
      .sharingMode = VK_SHARING_MODE_EXCLUSIVE,
  };
  require_result(vkCreateBuffer(device, &buffer_info, NULL, &result.buffer),
                 "vkCreateBuffer");

  const VkBufferMemoryRequirementsInfo2 requirements_info = {
      .sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_REQUIREMENTS_INFO_2,
      .buffer = result.buffer,
  };
  VkMemoryRequirements2 requirements = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_REQUIREMENTS_2,
  };
  vkGetBufferMemoryRequirements2(device, &requirements_info, &requirements);
  if (!(requirements.memoryRequirements.memoryTypeBits & 1))
    fail("Ventus probe buffer does not accept memory type zero");

  const VkMemoryAllocateFlagsInfo flags = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_FLAGS_INFO,
      .flags = VK_MEMORY_ALLOCATE_DEVICE_ADDRESS_BIT,
  };
  const VkMemoryAllocateInfo allocation_info = {
      .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
      .pNext = &flags,
      .allocationSize = requirements.memoryRequirements.size,
      .memoryTypeIndex = 0,
  };
  require_result(
      vkAllocateMemory(device, &allocation_info, NULL, &result.memory),
      "vkAllocateMemory");

  const VkBindBufferMemoryInfo bind_info = {
      .sType = VK_STRUCTURE_TYPE_BIND_BUFFER_MEMORY_INFO,
      .buffer = result.buffer,
      .memory = result.memory,
  };
  require_result(vkBindBufferMemory2(device, 1, &bind_info),
                 "vkBindBufferMemory2");

  const VkBufferDeviceAddressInfo address_info = {
      .sType = VK_STRUCTURE_TYPE_BUFFER_DEVICE_ADDRESS_INFO,
      .buffer = result.buffer,
  };
  result.address = vkGetBufferDeviceAddress(device, &address_info);
  if (!result.address)
    fail("bound device-address buffer returned address zero");
  return result;
}

static void destroy_address_buffer(VkDevice device,
                                   struct address_buffer buffer) {
  vkDestroyBuffer(device, buffer.buffer, NULL);
  vkFreeMemory(device, buffer.memory, NULL);
}

int main(void) {
  const VkApplicationInfo application_info = {
      .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
      .pApplicationName = "ventus-indirect-rt-dispatch-probe",
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
  require_result(
      vkEnumeratePhysicalDevices(instance, &physical_device_count, NULL),
      "vkEnumeratePhysicalDevices(count)");
  if (physical_device_count != 1)
    fail("isolated Ventus ICD must expose exactly one physical device");

  VkPhysicalDevice physical_device = VK_NULL_HANDLE;
  require_result(vkEnumeratePhysicalDevices(instance, &physical_device_count,
                                            &physical_device),
                 "vkEnumeratePhysicalDevices(data)");

  VkPhysicalDeviceBufferDeviceAddressFeatures queried_bda = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_BUFFER_DEVICE_ADDRESS_FEATURES,
  };
  VkPhysicalDeviceAccelerationStructureFeaturesKHR queried_as = {
      .sType =
          VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
      .pNext = &queried_bda,
  };
  VkPhysicalDeviceRayTracingPipelineFeaturesKHR queried_rt = {
      .sType =
          VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RAY_TRACING_PIPELINE_FEATURES_KHR,
      .pNext = &queried_as,
  };
  VkPhysicalDeviceFeatures2 queried_features = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2,
      .pNext = &queried_rt,
  };
  vkGetPhysicalDeviceFeatures2(physical_device, &queried_features);
  if (queried_rt.rayTracingPipeline != VK_TRUE ||
      queried_rt.rayTracingPipelineTraceRaysIndirect != VK_TRUE)
    fail("indirect ray tracing dispatch feature is not advertised");
  if (queried_as.accelerationStructure != VK_TRUE ||
      queried_bda.bufferDeviceAddress != VK_TRUE)
    fail("required AS or buffer-device-address feature is not advertised");

  const uint32_t queue_family = find_queue_family(physical_device);
  const float queue_priority = 1.0f;
  const VkDeviceQueueCreateInfo queue_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
      .queueFamilyIndex = queue_family,
      .queueCount = 1,
      .pQueuePriorities = &queue_priority,
  };
  const char *extensions[] = {
      VK_KHR_ACCELERATION_STRUCTURE_EXTENSION_NAME,
      VK_KHR_RAY_TRACING_PIPELINE_EXTENSION_NAME,
      VK_KHR_BUFFER_DEVICE_ADDRESS_EXTENSION_NAME,
      VK_KHR_DEFERRED_HOST_OPERATIONS_EXTENSION_NAME,
      VK_EXT_DESCRIPTOR_INDEXING_EXTENSION_NAME,
      VK_KHR_SPIRV_1_4_EXTENSION_NAME,
      VK_KHR_SHADER_FLOAT_CONTROLS_EXTENSION_NAME,
  };
  VkPhysicalDeviceAccelerationStructureFeaturesKHR enabled_as = {
      .sType =
          VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
      .accelerationStructure = VK_TRUE,
  };
  VkPhysicalDeviceBufferDeviceAddressFeatures enabled_bda = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_BUFFER_DEVICE_ADDRESS_FEATURES,
      .pNext = &enabled_as,
      .bufferDeviceAddress = VK_TRUE,
  };
  VkPhysicalDeviceRayTracingPipelineFeaturesKHR enabled_rt = {
      .sType =
          VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_RAY_TRACING_PIPELINE_FEATURES_KHR,
      .pNext = &enabled_bda,
      .rayTracingPipeline = VK_TRUE,
      .rayTracingPipelineTraceRaysIndirect = VK_TRUE,
  };
  const VkDeviceCreateInfo device_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
      .pNext = &enabled_rt,
      .queueCreateInfoCount = 1,
      .pQueueCreateInfos = &queue_info,
      .enabledExtensionCount =
          (uint32_t)(sizeof(extensions) / sizeof(extensions[0])),
      .ppEnabledExtensionNames = extensions,
  };

  VkDevice device = VK_NULL_HANDLE;
  require_result(vkCreateDevice(physical_device, &device_info, NULL, &device),
                 "vkCreateDevice");

  PFN_vkCmdTraceRaysIndirectKHR trace_indirect =
      (PFN_vkCmdTraceRaysIndirectKHR)vkGetDeviceProcAddr(
          device, "vkCmdTraceRaysIndirectKHR");
  if (!trace_indirect)
    fail("vkCmdTraceRaysIndirectKHR device entrypoint is unavailable");

  const VkCommandPoolCreateInfo pool_info = {
      .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
      .queueFamilyIndex = queue_family,
  };
  VkCommandPool command_pool = VK_NULL_HANDLE;
  require_result(vkCreateCommandPool(device, &pool_info, NULL, &command_pool),
                 "vkCreateCommandPool");
  const VkCommandBufferAllocateInfo command_info = {
      .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
      .commandPool = command_pool,
      .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY,
      .commandBufferCount = 1,
  };
  VkCommandBuffer command_buffer = VK_NULL_HANDLE;
  require_result(
      vkAllocateCommandBuffers(device, &command_info, &command_buffer),
      "vkAllocateCommandBuffers");
  const VkCommandBufferBeginInfo begin_info = {
      .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
  };
  require_result(vkBeginCommandBuffer(command_buffer, &begin_info),
                 "vkBeginCommandBuffer");

  const struct address_buffer wrong_usage =
      create_address_buffer(device, sizeof(VkTraceRaysIndirectCommandKHR),
                            VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
  const struct address_buffer short_range =
      create_address_buffer(device, 8, VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT);
  const VkStridedDeviceAddressRegionKHR empty_region = {0};

  trace_indirect(command_buffer, &empty_region, &empty_region, &empty_region,
                 &empty_region, wrong_usage.address + 2);
  trace_indirect(command_buffer, &empty_region, &empty_region, &empty_region,
                 &empty_region, wrong_usage.address);
  trace_indirect(command_buffer, &empty_region, &empty_region, &empty_region,
                 &empty_region, short_range.address);

  require_result(vkEndCommandBuffer(command_buffer), "vkEndCommandBuffer");
  destroy_address_buffer(device, short_range);
  destroy_address_buffer(device, wrong_usage);
  vkDestroyCommandPool(device, command_pool, NULL);
  vkDestroyDevice(device, NULL);
  vkDestroyInstance(instance, NULL);

  puts("PASS indirect-rt-dispatch-negative feature=1 rejected=3");
  return EXIT_SUCCESS;
}
