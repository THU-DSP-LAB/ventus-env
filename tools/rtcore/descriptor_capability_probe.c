#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <vulkan/vulkan.h>

#define EXPECTED_MAX_BOUND_DESCRIPTOR_SETS 4

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

   VkExtensionProperties *extensions =
      calloc(count, sizeof(*extensions));
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

   VkQueueFamilyProperties *properties =
      calloc(count, sizeof(*properties));
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

   fprintf(stderr, "error: unsupported feature reported true: %s\n", name);
   exit(EXIT_FAILURE);
}

int
main(void)
{
   const VkApplicationInfo application_info = {
      .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
      .pApplicationName = "ventus-descriptor-capability-probe",
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

   VkPhysicalDeviceVulkan12Properties vulkan12_properties = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_PROPERTIES,
   };
   VkPhysicalDeviceProperties2 properties = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PROPERTIES_2,
      .pNext = &vulkan12_properties,
   };
   vkGetPhysicalDeviceProperties2(physical_device, &properties);

   if (properties.properties.limits.maxBoundDescriptorSets !=
       EXPECTED_MAX_BOUND_DESCRIPTOR_SETS) {
      fprintf(stderr,
              "error: maxBoundDescriptorSets=%u, expected %u\n",
              properties.properties.limits.maxBoundDescriptorSets,
              EXPECTED_MAX_BOUND_DESCRIPTOR_SETS);
      return EXIT_FAILURE;
   }
   require_false(
      vulkan12_properties.shaderUniformBufferArrayNonUniformIndexingNative,
      "shaderUniformBufferArrayNonUniformIndexingNative");
   require_false(
      vulkan12_properties.shaderSampledImageArrayNonUniformIndexingNative,
      "shaderSampledImageArrayNonUniformIndexingNative");
   require_false(
      vulkan12_properties.shaderStorageBufferArrayNonUniformIndexingNative,
      "shaderStorageBufferArrayNonUniformIndexingNative");
   require_false(
      vulkan12_properties.shaderStorageImageArrayNonUniformIndexingNative,
      "shaderStorageImageArrayNonUniformIndexingNative");
   require_false(
      vulkan12_properties.shaderInputAttachmentArrayNonUniformIndexingNative,
      "shaderInputAttachmentArrayNonUniformIndexingNative");
   require_false(vulkan12_properties.robustBufferAccessUpdateAfterBind,
                 "robustBufferAccessUpdateAfterBind");
   if (vulkan12_properties.maxUpdateAfterBindDescriptorsInAllPools != 0)
      fail("maxUpdateAfterBindDescriptorsInAllPools must be zero");

   VkPhysicalDeviceAccelerationStructureFeaturesKHR acceleration_features = {
      .sType =
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_ACCELERATION_STRUCTURE_FEATURES_KHR,
   };
   VkPhysicalDeviceVulkan12Features vulkan12_features = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_VULKAN_1_2_FEATURES,
      .pNext = &acceleration_features,
   };
   VkPhysicalDeviceFeatures2 features = {
      .sType = VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2,
      .pNext = &vulkan12_features,
   };
   vkGetPhysicalDeviceFeatures2(physical_device, &features);

   if (vulkan12_features.descriptorIndexing != VK_TRUE)
      fail("descriptorIndexing extension aggregate is not available");

#define REQUIRE_CORE_FALSE(field) \
   require_false(features.features.field, #field)
   REQUIRE_CORE_FALSE(shaderUniformBufferArrayDynamicIndexing);
   REQUIRE_CORE_FALSE(shaderSampledImageArrayDynamicIndexing);
   REQUIRE_CORE_FALSE(shaderStorageBufferArrayDynamicIndexing);
   REQUIRE_CORE_FALSE(shaderStorageImageArrayDynamicIndexing);
#undef REQUIRE_CORE_FALSE

#define REQUIRE_V12_FALSE(field) \
   require_false(vulkan12_features.field, #field)
   REQUIRE_V12_FALSE(shaderInputAttachmentArrayDynamicIndexing);
   REQUIRE_V12_FALSE(shaderUniformTexelBufferArrayDynamicIndexing);
   REQUIRE_V12_FALSE(shaderStorageTexelBufferArrayDynamicIndexing);
   REQUIRE_V12_FALSE(shaderUniformBufferArrayNonUniformIndexing);
   REQUIRE_V12_FALSE(shaderSampledImageArrayNonUniformIndexing);
   REQUIRE_V12_FALSE(shaderStorageBufferArrayNonUniformIndexing);
   REQUIRE_V12_FALSE(shaderStorageImageArrayNonUniformIndexing);
   REQUIRE_V12_FALSE(shaderInputAttachmentArrayNonUniformIndexing);
   REQUIRE_V12_FALSE(shaderUniformTexelBufferArrayNonUniformIndexing);
   REQUIRE_V12_FALSE(shaderStorageTexelBufferArrayNonUniformIndexing);
   REQUIRE_V12_FALSE(descriptorBindingUniformBufferUpdateAfterBind);
   REQUIRE_V12_FALSE(descriptorBindingSampledImageUpdateAfterBind);
   REQUIRE_V12_FALSE(descriptorBindingStorageImageUpdateAfterBind);
   REQUIRE_V12_FALSE(descriptorBindingStorageBufferUpdateAfterBind);
   REQUIRE_V12_FALSE(descriptorBindingUniformTexelBufferUpdateAfterBind);
   REQUIRE_V12_FALSE(descriptorBindingStorageTexelBufferUpdateAfterBind);
   REQUIRE_V12_FALSE(descriptorBindingUpdateUnusedWhilePending);
   REQUIRE_V12_FALSE(descriptorBindingPartiallyBound);
   REQUIRE_V12_FALSE(descriptorBindingVariableDescriptorCount);
   REQUIRE_V12_FALSE(runtimeDescriptorArray);
#undef REQUIRE_V12_FALSE

   require_false(
      acceleration_features
         .descriptorBindingAccelerationStructureUpdateAfterBind,
      "descriptorBindingAccelerationStructureUpdateAfterBind");

   if (!has_extension(physical_device,
                      VK_EXT_DESCRIPTOR_INDEXING_EXTENSION_NAME))
      fail("VK_EXT_descriptor_indexing extension alias is not available");

   const uint32_t queue_family = find_queue_family(physical_device);
   const float queue_priority = 1.0f;
   const VkDeviceQueueCreateInfo queue_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
      .queueFamilyIndex = queue_family,
      .queueCount = 1,
      .pQueuePriorities = &queue_priority,
   };
   const char *extensions[] = {
      VK_EXT_DESCRIPTOR_INDEXING_EXTENSION_NAME,
   };
   const VkDeviceCreateInfo baseline_device_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
      .queueCreateInfoCount = 1,
      .pQueueCreateInfos = &queue_info,
      .enabledExtensionCount = 1,
      .ppEnabledExtensionNames = extensions,
   };

   VkDevice device = VK_NULL_HANDLE;
   require_result(vkCreateDevice(physical_device, &baseline_device_info, NULL,
                                 &device),
                  "vkCreateDevice(baseline)");
   vkDestroyDevice(device, NULL);

   const VkPhysicalDeviceDescriptorIndexingFeatures unsupported_features = {
      .sType =
         VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_DESCRIPTOR_INDEXING_FEATURES,
      .runtimeDescriptorArray = VK_TRUE,
   };
   const VkDeviceCreateInfo unsupported_device_info = {
      .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
      .pNext = &unsupported_features,
      .queueCreateInfoCount = 1,
      .pQueueCreateInfos = &queue_info,
      .enabledExtensionCount = 1,
      .ppEnabledExtensionNames = extensions,
   };
   device = VK_NULL_HANDLE;
   const VkResult unsupported_result =
      vkCreateDevice(physical_device, &unsupported_device_info, NULL, &device);
   if (unsupported_result != VK_ERROR_FEATURE_NOT_PRESENT) {
      if (unsupported_result == VK_SUCCESS)
         vkDestroyDevice(device, NULL);
      fprintf(stderr,
              "error: runtimeDescriptorArray request returned %d, expected %d\n",
              unsupported_result, VK_ERROR_FEATURE_NOT_PRESENT);
      return EXIT_FAILURE;
   }

   vkDestroyInstance(instance, NULL);
   printf("PASS descriptor-capability-profile max_bound_sets=%u "
          "descriptor_indexing=1 optional_indexing_features=0 "
          "unsupported_request=%d\n",
          EXPECTED_MAX_BOUND_DESCRIPTOR_SETS,
          VK_ERROR_FEATURE_NOT_PRESENT);
   return EXIT_SUCCESS;
}
