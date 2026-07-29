#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <vulkan/vulkan.h>

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

static bool
has_extension(VkPhysicalDevice physical_device, const char *name)
{
   uint32_t count = 0;
   require_result(vkEnumerateDeviceExtensionProperties(
                     physical_device, NULL, &count, NULL),
                  "vkEnumerateDeviceExtensionProperties(count)");

   VkExtensionProperties *extensions = calloc(count, sizeof(*extensions));
   if (count > 0 && !extensions)
      fail("failed to allocate extension property array");

   require_result(vkEnumerateDeviceExtensionProperties(
                     physical_device, NULL, &count, extensions),
                  "vkEnumerateDeviceExtensionProperties(data)");

   bool found = false;
   for (uint32_t i = 0; i < count; i++) {
      if (strcmp(extensions[i].extensionName, name) == 0) {
         found = true;
         break;
      }
   }
   free(extensions);
   return found;
}

static uint32_t
find_queue_family(VkPhysicalDevice physical_device)
{
   uint32_t count = 0;
   vkGetPhysicalDeviceQueueFamilyProperties(physical_device, &count, NULL);
   if (count == 0)
      fail("physical device exposes no queue families");

   VkQueueFamilyProperties *properties = calloc(count, sizeof(*properties));
   if (!properties)
      fail("failed to allocate queue family properties");

   vkGetPhysicalDeviceQueueFamilyProperties(physical_device, &count,
                                            properties);
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

static void
require_false(VkBool32 value, const char *name)
{
   if (value == VK_FALSE)
      return;

   fprintf(stderr, "error: unsupported AS feature reported true: %s\n", name);
   exit(EXIT_FAILURE);
}

static void
verify_as_vertex_format(VkPhysicalDevice physical_device)
{
   VkFormatProperties legacy = {0};
   vkGetPhysicalDeviceFormatProperties(
      physical_device, VK_FORMAT_R32G32B32_SFLOAT, &legacy);
   if (!(legacy.bufferFeatures &
         VK_FORMAT_FEATURE_ACCELERATION_STRUCTURE_VERTEX_BUFFER_BIT_KHR))
      fail("legacy format query omits the implemented AS vertex format");
   if ((legacy.linearTilingFeatures &
        VK_FORMAT_FEATURE_ACCELERATION_STRUCTURE_VERTEX_BUFFER_BIT_KHR) ||
       (legacy.optimalTilingFeatures &
        VK_FORMAT_FEATURE_ACCELERATION_STRUCTURE_VERTEX_BUFFER_BIT_KHR))
      fail("AS vertex-buffer support was reported as an image feature");

   VkFormatProperties3 properties3 = {
      .sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_3,
   };
   VkFormatProperties2 properties2 = {
      .sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2,
      .pNext = &properties3,
   };
   vkGetPhysicalDeviceFormatProperties2(
      physical_device, VK_FORMAT_R32G32B32_SFLOAT, &properties2);
   if (!(properties2.formatProperties.bufferFeatures &
         VK_FORMAT_FEATURE_ACCELERATION_STRUCTURE_VERTEX_BUFFER_BIT_KHR))
      fail("format-properties2 omits the implemented AS vertex format");
   if (!(properties3.bufferFeatures &
         VK_FORMAT_FEATURE_2_ACCELERATION_STRUCTURE_VERTEX_BUFFER_BIT_KHR))
      fail("format-properties3 omits the implemented AS vertex format");

   properties3 = (VkFormatProperties3){
      .sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_3,
   };
   properties2 = (VkFormatProperties2){
      .sType = VK_STRUCTURE_TYPE_FORMAT_PROPERTIES_2,
      .pNext = &properties3,
   };
   vkGetPhysicalDeviceFormatProperties2(
      physical_device, VK_FORMAT_R32G32_SFLOAT, &properties2);
   if ((properties2.formatProperties.bufferFeatures &
        VK_FORMAT_FEATURE_ACCELERATION_STRUCTURE_VERTEX_BUFFER_BIT_KHR) ||
       (properties3.bufferFeatures &
        VK_FORMAT_FEATURE_2_ACCELERATION_STRUCTURE_VERTEX_BUFFER_BIT_KHR))
      fail("format query advertises an unsupported AS vertex format");
}

int
main(void)
{
   const VkApplicationInfo application_info = {
      .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
      .pApplicationName = "ventus-as-host-capability-probe",
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

   if (!has_extension(physical_device,
                      VK_KHR_ACCELERATION_STRUCTURE_EXTENSION_NAME))
      fail("VK_KHR_acceleration_structure is not available");
   if (!has_extension(physical_device,
                      VK_KHR_DEFERRED_HOST_OPERATIONS_EXTENSION_NAME))
      fail("VK_KHR_deferred_host_operations is not available");

   verify_as_vertex_format(physical_device);

   VkPhysicalDeviceAccelerationStructureFeaturesKHR queried_as = {
      .sType =
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
   };
   VkPhysicalDeviceFeatures2 queried_features = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2,
      .pNext = &queried_as,
   };
   vkGetPhysicalDeviceFeatures2(physical_device, &queried_features);

   if (queried_as.accelerationStructure != VK_TRUE ||
       queried_as.accelerationStructureIndirectBuild != VK_TRUE)
      fail("direct or indirect device AS build support is not available");
   require_false(queried_as.accelerationStructureCaptureReplay,
                 "accelerationStructureCaptureReplay");
   require_false(queried_as.accelerationStructureHostCommands,
                 "accelerationStructureHostCommands");
   require_false(queried_as.descriptorBindingAccelerationStructureUpdateAfterBind,
                 "descriptorBindingAccelerationStructureUpdateAfterBind");

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
      VK_KHR_DEFERRED_HOST_OPERATIONS_EXTENSION_NAME,
   };
   VkPhysicalDeviceAccelerationStructureFeaturesKHR enabled_as = {
      .sType =
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
      .accelerationStructure = VK_TRUE,
   };
   VkPhysicalDeviceVulkan12Features enabled_vulkan12 = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES,
      .pNext = &enabled_as,
      .descriptorIndexing = VK_TRUE,
      .bufferDeviceAddress = VK_TRUE,
   };
   const VkDeviceCreateInfo device_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
      .pNext = &enabled_vulkan12,
      .queueCreateInfoCount = 1,
      .pQueueCreateInfos = &queue_info,
      .enabledExtensionCount = 2,
      .ppEnabledExtensionNames = extensions,
   };

   VkDevice device = VK_NULL_HANDLE;
   require_result(vkCreateDevice(physical_device, &device_info, NULL, &device),
                  "vkCreateDevice(baseline AS)");

   PFN_vkCreateDeferredOperationKHR create_deferred_operation =
      (PFN_vkCreateDeferredOperationKHR)
         vkGetDeviceProcAddr(device, "vkCreateDeferredOperationKHR");
   PFN_vkDestroyDeferredOperationKHR destroy_deferred_operation =
      (PFN_vkDestroyDeferredOperationKHR)
         vkGetDeviceProcAddr(device, "vkDestroyDeferredOperationKHR");
   PFN_vkGetDeferredOperationMaxConcurrencyKHR get_deferred_concurrency =
      (PFN_vkGetDeferredOperationMaxConcurrencyKHR)
         vkGetDeviceProcAddr(device,
                             "vkGetDeferredOperationMaxConcurrencyKHR");
   PFN_vkGetDeferredOperationResultKHR get_deferred_result =
      (PFN_vkGetDeferredOperationResultKHR)
         vkGetDeviceProcAddr(device, "vkGetDeferredOperationResultKHR");
   PFN_vkDeferredOperationJoinKHR join_deferred_operation =
      (PFN_vkDeferredOperationJoinKHR)
         vkGetDeviceProcAddr(device, "vkDeferredOperationJoinKHR");
   if (!create_deferred_operation || !destroy_deferred_operation ||
       !get_deferred_concurrency || !get_deferred_result ||
       !join_deferred_operation)
      fail("deferred-operation device entrypoints are unavailable");

   VkDeferredOperationKHR operation = VK_NULL_HANDLE;
   require_result(create_deferred_operation(device, NULL, &operation),
                  "vkCreateDeferredOperationKHR");
   if (operation == VK_NULL_HANDLE)
      fail("deferred operation creation returned a null handle");
   if (get_deferred_concurrency(device, operation) != 1)
      fail("synchronous deferred operation must report concurrency one");
   require_result(get_deferred_result(device, operation),
                  "vkGetDeferredOperationResultKHR");
   require_result(join_deferred_operation(device, operation),
                  "vkDeferredOperationJoinKHR");
   destroy_deferred_operation(device, operation, NULL);
   vkDestroyDevice(device, NULL);

   VkPhysicalDeviceAccelerationStructureFeaturesKHR unsupported_as = {
      .sType =
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
      .accelerationStructure = VK_TRUE,
      .accelerationStructureHostCommands = VK_TRUE,
   };
   VkPhysicalDeviceVulkan12Features unsupported_vulkan12 = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES,
      .pNext = &unsupported_as,
      .descriptorIndexing = VK_TRUE,
      .bufferDeviceAddress = VK_TRUE,
   };
   const VkDeviceCreateInfo unsupported_device_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
      .pNext = &unsupported_vulkan12,
      .queueCreateInfoCount = 1,
      .pQueueCreateInfos = &queue_info,
      .enabledExtensionCount = 2,
      .ppEnabledExtensionNames = extensions,
   };

   device = VK_NULL_HANDLE;
   const VkResult unsupported_result =
      vkCreateDevice(physical_device, &unsupported_device_info, NULL, &device);
   if (unsupported_result != VK_ERROR_FEATURE_NOT_PRESENT) {
      if (unsupported_result == VK_SUCCESS)
         vkDestroyDevice(device, NULL);
      fprintf(stderr,
              "error: host AS feature request returned %d, expected %d\n",
              unsupported_result, VK_ERROR_FEATURE_NOT_PRESENT);
      return EXIT_FAILURE;
   }

   vkDestroyInstance(instance, NULL);
   printf("PASS as-host-capability-profile device_build=1 host_commands=0 "
          "indirect_build=1 capture_replay=0 deferred_operation=sync "
          "as_vertex_format=R32G32B32_SFLOAT format_properties3=1 "
          "unsupported_request=%d\n",
          VK_ERROR_FEATURE_NOT_PRESENT);
   return EXIT_SUCCESS;
}
