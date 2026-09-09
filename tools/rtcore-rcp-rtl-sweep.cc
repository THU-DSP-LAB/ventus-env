#include "VRtFp32Reciprocal.h"
#include "verilated.h"

#define VENTUS_RT_STANDALONE
#include "ventus_rt.h"

#include <cstdint>
#include <cstdio>
#include <cstring>

namespace {

uint32_t fp32_bits(float value)
{
  uint32_t bits = 0;
  static_assert(sizeof(bits) == sizeof(value), "FP32 host required");
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

float fp32_value(uint32_t bits)
{
  float value = 0.0f;
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}

} // namespace

int main(int argc, char **argv)
{
  Verilated::commandArgs(argc, argv);
  VRtFp32Reciprocal dut;
  dut.clock = 0;
  dut.reset = 1;
  dut.io_in_valid = 0;
  dut.io_out_ready = 1;
  for (int cycle = 0; cycle < 2; ++cycle) {
    dut.eval();
    dut.clock = 1;
    dut.eval();
    dut.clock = 0;
  }
  dut.reset = 0;

  constexpr uint32_t count = uint32_t{1} << 23;
  uint32_t issued = 0;
  uint32_t received = 0;
  uint32_t max_ulp = 0;
  uint64_t exact_count = 0;
  uint64_t one_ulp_count = 0;

  while (received < count) {
    dut.clock = 0;
    dut.io_in_valid = issued < count;
    dut.io_in_bits = 0x3f800000u | issued;
    dut.io_out_ready = 1;
    dut.eval();

    const bool input_fire = dut.io_in_valid && dut.io_in_ready;
    const bool output_fire = dut.io_out_valid && dut.io_out_ready;
    if (output_fire) {
      const uint32_t input = 0x3f800000u | received;
      const uint32_t actual = dut.io_out_bits;
      const uint32_t model =
          ventus_rt::vtas_v2_rtcore_reciprocal_bits(input);
      if (actual != model) {
        std::fprintf(stderr,
                     "RTL/model mismatch input=%08x rtl=%08x model=%08x\n",
                     input, actual, model);
        return 1;
      }
      const uint32_t exact = fp32_bits(1.0f / fp32_value(input));
      const uint32_t ulp = actual > exact ? actual - exact : exact - actual;
      if (ulp > max_ulp)
        max_ulp = ulp;
      if (ulp == 0)
        ++exact_count;
      else if (ulp == 1)
        ++one_ulp_count;
      else {
        std::fprintf(stderr,
                     "ULP bound failed input=%08x rtl=%08x exact=%08x ulp=%u\n",
                     input, actual, exact, ulp);
        return 1;
      }
      ++received;
    }

    dut.clock = 1;
    dut.eval();
    if (input_fire)
      ++issued;
  }

  dut.final();
  std::printf("normalized mantissas=%u max_ulp=%u exact=%llu one_ulp=%llu\n",
              count, max_ulp,
              static_cast<unsigned long long>(exact_count),
              static_cast<unsigned long long>(one_ulp_count));
  return 0;
}
