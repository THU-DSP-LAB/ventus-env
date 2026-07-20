#!/usr/bin/env bash

set -euo pipefail

DIR=$(cd "$(dirname "${0}")" &> /dev/null && (pwd -W 2> /dev/null || pwd))
VENTUS_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX:-${DIR}/install}
PROGRAMS_TOBUILD_DEFAULT=(systemc llvm ocl-icd libclc spike gvm sbtsim driver pocl rodinia cts test-pocl)
PROGRAMS_TOBUILD_DEFAULT_FULL=(systemc llvm ocl-icd libclc spike rtlsim cyclesim gvm sbtsim driver pocl rodinia cts test-pocl)
PROGRAMS_TOBUILD=(${PROGRAMS_TOBUILD_DEFAULT_FULL[@]})

BUILD_PARALLEL=$(( $(nproc) * 2 / 3 ))

# Helper function
help() {
  cat <<END

  Build [systemc, llvm, rt-toolchain, pocl, ocl-icd, libclc, driver, driver-spike, spike, rtlsim|gpgpu, cyclesim|simulator, gvm, sbt|sbtsim|ptx|ptxsim, mesa, rt-workload] programs.
Run the rodinia and test-pocl test suites.
Read ${DIR}/llvm/README.md to get started.

Usage: ${DIR}/$(basename ${0})
                          [--build <build programs>]
                          [--help | -h]

Options:
  --build <build programs>
    Chosen programs to build : [${PROGRAMS_TOBUILD[*]}]
    Option format : "llvm;pocl", string are separated by semicolon.
    ( Note that quotation marks are necessary, or bash will parse the semicolon as command ending )
    Default : "llvm;ocl-icd;libclc;spike;rtlsim;cyclesim;sbtsim;driver;pocl;rodinia;test-pocl"
    'BUILD_TYPE' is default 'Release' which can be changed by enviroment variable

  --help | -h
    Print this help message and exit.
END
}

# Check the to be built program exits in file system or not
check_if_program_exits() {
  if [ ! -d "$1" ]; then
    echo "WARNING:*************************************************************"
    echo
    echo "$2 folder not found, please set or check!"
    echo "Default folder is set to be $(realpath $1)"
    echo
    echo "WARNING:*************************************************************"
    exit 1
  fi
}

# Parse command line options
while [ $# -gt 0 ]; do
  case $1 in
  -h | --help)
    help
    exit 0
    ;;

  --build)
    shift
    if [ ! -z "${1}" ];then
      PROGRAMS_TOBUILD=(${1//;/ })
    fi
    ;;
  
  # --build-full)
  #   PROGRAMS_TOBUILD=(${PROGRAMS_TOBUILD_DEFAULT_FULL[@]})
  #   ;;

  ?*)
    echo "Invalid options: \"$1\" , try ${DIR}/$(basename ${0}) --help for help"
    exit -1
    ;;
  esac
  # Process next command-line option
  shift
done

# Get build type from env, otherwise use default value 'Release'
BUILD_TYPE=${BUILD_TYPE:-Release}

# Detect nvidia driver availability; sbtsim requires CUDA/nvidia driver to build
NVIDIA_DRIVER_AVAILABLE=false
check_nvidia_driver() {
  # Check for nvidia-smi, /dev/nvidia0, or libcuda.so as indicators of a working nvidia driver
  if nvidia-smi &> /dev/null 2>&1; then
    NVIDIA_DRIVER_AVAILABLE=true
  elif [ -e /dev/nvidia0 ]; then
    NVIDIA_DRIVER_AVAILABLE=true
  elif ldconfig -p 2>/dev/null | grep -q "libcuda\.so"; then
    NVIDIA_DRIVER_AVAILABLE=true
  fi
  if [ "${NVIDIA_DRIVER_AVAILABLE}" = "false" ]; then
    echo "WARNING:*************************************************************"
    echo
    echo "NVIDIA driver not found. Skipping sbtsim (SBT PTX translator) build."
    echo "If you need sbtsim, please install the NVIDIA driver and try again."
    echo
    echo "WARNING:*************************************************************"
  fi
}
check_nvidia_driver

# Need to get the systemc folder from enviroment variables
SYSTEMC_DIR=${SYSTEMC_DIR:-${DIR}/systemc}
SYSTEMC_INSTALL_DIR=${SYSTEMC_INSTALL_DIR:-${VENTUS_INSTALL_PREFIX}/systemc}
check_if_program_exits $SYSTEMC_DIR "lib systemc"

# Need to get the ventus-llvm folder from enviroment variables
LLVM_DIR=${LLVM_DIR:-${DIR}/llvm}
check_if_program_exits $LLVM_DIR "ventus-llvm"
LIBCLC_DIR=${LLVM_DIR}/libclc
LLVM_BUILD_DIR=${LLVM_DIR}/build
LIBCLC_BUILD_DIR=${LLVM_DIR}/build-libclc
RT_LIBCLC_BUILD_DIR=${RT_LIBCLC_BUILD_DIR:-${LLVM_DIR}/build-rt-libclc}

# Need to get the cpp-cycle-level-simulator folder from enviroment variables
CYCLESIM_DIR=${CYCLESIM_DIR:-${DIR}/cyclesim}
check_if_program_exits $CYCLESIM_DIR "ventus-gpgpu cpp cycle-level simulator"
CYCLESIM_BUILD_DIR=${CYCLESIM_DIR}/build

# Need to get the ventus-gpgpu (Chisel RTL) folder from enviroment variables
GPGPU_DIR=${GPGPU_DIR:-${DIR}/gpgpu}
check_if_program_exits $GPGPU_DIR "ventus-gpgpu chisel RTL"

# Need to get the pocl folder from enviroment variables
POCL_DIR=${POCL_DIR:-${DIR}/pocl}
check_if_program_exits $POCL_DIR "pocl"
POCL_BUILD_DIR=${POCL_DIR}/build

# Need to get the ventus-driver folder from enviroment variables
DRIVER_DIR=${DRIVER_DIR:-${DIR}/driver}
check_if_program_exits ${DRIVER_DIR} "ventus-driver"
DRIVER_BUILD_DIR=${DRIVER_DIR}/build

# Need to get the sbtsim (SBT PTX translator) folder from enviroment variables
SBTSIM_DIR=${SBTSIM_DIR:-${DIR}/sbtsim}
check_if_program_exits ${SBTSIM_DIR} "sbtsim (SBT PTX translator)"
SBTSIM_BUILD_DIR=${SBTSIM_DIR}/build

# Need to get the ventus-spike folder from enviroment variables
SPIKE_DIR=${SPIKE_DIR:-${DIR}/spike}
check_if_program_exits ${SPIKE_DIR} "spike"
SPIKE_BUILD_DIR=${SPIKE_DIR}/build

# Need to get the icd_loader folder from enviroment variables
OCL_ICD_DIR=${OCL_ICD_DIR:-${DIR}/ocl-icd}
check_if_program_exits ${OCL_ICD_DIR} "ocl-icd"
OCL_ICD_BUILD_DIR=${OCL_ICD_DIR}/build

# Need to get the OpenCL-CTS folder from enviroment variables
OPENCL_CTS_DIR=${OPENCL_CTS_DIR:-${DIR}/OpenCL-CTS}
check_if_program_exits ${OPENCL_CTS_DIR} "OpenCL Conformance Test Suite (CTS)"
OPENCL_CTS_BUILD_DIR=${OPENCL_CTS_DIR}/build

# Need to get the gpu-rodinia folder from enviroment variables
RODINIA_DIR=${RODINIA_DIR:-${DIR}/rodinia}
check_if_program_exits ${RODINIA_DIR} "gpu-rodinia"

# Optional Vulkan RT vertical-slice sources. These targets are not part of the
# default OpenCL build and are built only when explicitly requested.
MESA_DIR=${MESA_DIR:-${DIR}/mesa}
check_if_program_exits ${MESA_DIR} "Mesa Ventus Vulkan driver"
MESA_BUILD_DIR=${MESA_BUILD_DIR:-${MESA_DIR}/build-ventus}

RT_WORKLOAD_DIR=${RT_WORKLOAD_DIR:-${DIR}/workloads/rt}
check_if_program_exits ${RT_WORKLOAD_DIR} "Ventus RT workload"
RT_WORKLOAD_BUILD_ROOT=${RT_WORKLOAD_BUILD_ROOT:-${DIR}/build/rt-workload}
RT_WORKLOAD_SOURCE_DIR=${RT_WORKLOAD_SOURCE_DIR:-${RT_WORKLOAD_BUILD_ROOT}/source}
RT_WORKLOAD_BUILD_DIR=${RT_WORKLOAD_BUILD_DIR:-${RT_WORKLOAD_BUILD_ROOT}/build}

# Build library systemc: depended by cyclesim
build_systemc() {
  cd ${SYSTEMC_DIR}
  ./config/bootstrap
  mkdir -p ${SYSTEMC_DIR}/build
  cd ${SYSTEMC_DIR}/build
  ../configure 'CXXFLAGS=-std=c++20' --prefix=${SYSTEMC_INSTALL_DIR} --enable-debug
  make -j${BUILD_PARALLEL}
  make -j${BUILD_PARALLEL} check
  make install
}

# Build llvm
build_llvm() {
  if [ -e "${LLVM_DIR}/prebuilt" ]; then
    echo "Using prebuilt llvm-ventus, skip building"
    cp --reflink=auto -a ${LLVM_DIR}/install ${VENTUS_INSTALL_PREFIX}
    return 0
  fi
  mkdir -p ${LLVM_BUILD_DIR}
  cd ${LLVM_BUILD_DIR}
  # libclc is configured separately below. Enabling it here makes LLVM's
  # external project take ownership of build-libclc and build every GPU
  # target, which is both unnecessary and incompatible with this tree.
  cmake -G Ninja -B ${LLVM_BUILD_DIR} -S ${LLVM_DIR}/llvm \
    -DLLVM_CCACHE_BUILD=ON \
    -DLLVM_OPTIMIZED_TABLEGEN=ON \
    -DLLVM_PARALLEL_LINK_JOBS=12 \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DLLVM_ENABLE_PROJECTS="clang;lld" \
    -DLLVM_TARGETS_TO_BUILD="AMDGPU;X86;RISCV" \
    -DLLVM_TARGET_ARCH=riscv32 \
    -DBUILD_SHARED_LIBS=ON \
    -DLLVM_BUILD_LLVM_DYLIB=ON \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX}
  ninja
  ninja install
}

# Build ventus driver
build_driver() {
  local driver_enable_ptx="ON"
  if [ "${NVIDIA_DRIVER_AVAILABLE}" = "false" ]; then
    driver_enable_ptx="OFF"
    echo "WARNING: Building driver without PTX support (sbtsim skipped -- NVIDIA driver not available)."
  fi
  mkdir -p ${DRIVER_BUILD_DIR}
  cd ${DRIVER_DIR}
  cmake -G Ninja -B ${DRIVER_BUILD_DIR} -S ${DRIVER_DIR} \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DVENTUS_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DSPIKE_SRC_DIR=${SPIKE_DIR} \
    -DDRIVER_ENABLE_AUTOSELECT=ON \
    -DDRIVER_ENABLE_RTLSIM=ON \
    -DDRIVER_ENABLE_CYCLESIM=ON \
    -DDRIVER_ENABLE_GVM=ON \
    -DDRIVER_ENABLE_PTX=${driver_enable_ptx}
    # -DCMAKE_C_COMPILER=clang \
    # -DCMAKE_CXX_COMPILER=clang++ \
  ninja -C ${DRIVER_BUILD_DIR}
  ninja -C ${DRIVER_BUILD_DIR} install
}

# Minimal driver build used by the Spike-only Vulkan RT functional path. It
# intentionally avoids requiring RTL, cycle-simulator, GVM, PTX or auto-select
# backend artifacts.
build_spike_driver() {
  mkdir -p ${DRIVER_BUILD_DIR}
  cd ${DRIVER_DIR}
  cmake -G Ninja -B ${DRIVER_BUILD_DIR} -S ${DRIVER_DIR} \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DVENTUS_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DSPIKE_SRC_DIR=${SPIKE_DIR} \
    -DDRIVER_ENABLE_AUTOSELECT=OFF \
    -DDRIVER_ENABLE_RTLSIM=OFF \
    -DDRIVER_ENABLE_CYCLESIM=OFF \
    -DDRIVER_ENABLE_GVM=OFF \
    -DDRIVER_ENABLE_PTX=OFF
  ninja -C ${DRIVER_BUILD_DIR}
  ninja -C ${DRIVER_BUILD_DIR} install
}

build_mesa_ventus() {
  local setup_args=(setup)
  local meson_bin="${MESON:-meson}"
  if [ -z "${MESON:-}" ] && [ -x "${DIR}/build/meson-venv/bin/meson" ]; then
    meson_bin="${DIR}/build/meson-venv/bin/meson"
  fi
  if [ -f "${MESA_BUILD_DIR}/meson-private/coredata.dat" ]; then
    setup_args+=(--reconfigure)
  fi

  PATH="$(dirname "${meson_bin}"):${PATH}" \
    "${meson_bin}" "${setup_args[@]}" ${MESA_BUILD_DIR} ${MESA_DIR} \
    -Dbuildtype=debugoptimized \
    -Dvulkan-drivers=ventus \
    -Dgallium-drivers= \
    -Dllvm=enabled \
    -Dplatforms=x11 \
    -Dglx=disabled \
    -Degl=disabled \
    -Dgles1=disabled \
    -Dgles2=disabled \
    -Dopengl=false \
    -Dbuild-tests=false \
    -Dvulkan-layers= \
    -Dtools= \
    -Dventus_rt_resident_warps=8
  ninja -C ${MESA_BUILD_DIR}
}

build_rt_workload() {
  # The frozen compatibility image was produced by the sample's headless
  # platform path.  XCB has a different benchmark/warm-up lifecycle and can
  # submit an extra frame, so it is not an equivalent functional gate.
  local rt_workload_build_type="${RT_WORKLOAD_BUILD_TYPE:-}"

  RT_WORKLOAD_BUILD_ROOT=${RT_WORKLOAD_BUILD_ROOT} \
  RT_WORKLOAD_SOURCE_DIR=${RT_WORKLOAD_SOURCE_DIR} \
    ${RT_WORKLOAD_DIR}/prepare.sh

  cmake -G Ninja -B ${RT_WORKLOAD_BUILD_DIR} -S ${RT_WORKLOAD_SOURCE_DIR} \
    -DCMAKE_BUILD_TYPE="${rt_workload_build_type}" \
    -DUSE_HEADLESS=ON
  ninja -C ${RT_WORKLOAD_BUILD_DIR} \
    raytracingshadows \
    raytracingintersection \
    raytracingsbtdata
}

# Build sbtsim (SBT translator) and install via CMake rules to ${VENTUS_INSTALL_PREFIX}
build_sbtsim() {
  mkdir -p ${SBTSIM_BUILD_DIR}
  cd ${SBTSIM_DIR}
  cmake -G Ninja -B ${SBTSIM_BUILD_DIR} -S ${SBTSIM_DIR} \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DSBT_SPIKE_ENCODING_H=${SPIKE_DIR}/riscv/encoding.h
  ninja -C ${SBTSIM_BUILD_DIR}
  cmake --install ${SBTSIM_BUILD_DIR}
}

# Build spike simulator
build_spike() {
  # rm -rf ${SPIKE_BUILD_DIR} || true
  mkdir -p ${SPIKE_BUILD_DIR}
  cd ${SPIKE_BUILD_DIR}
  ../configure --prefix=${VENTUS_INSTALL_PREFIX} --enable-commitlog
  make -j${BUILD_PARALLEL}
  make install
}

# Build ventus cpp cycle-level simulator
build_gpgpu_cyclesim() {
  cd ${CYCLESIM_DIR}
  cmake -G Ninja -B ${CYCLESIM_BUILD_DIR} -S ${CYCLESIM_DIR} \
    -DSYSTEMC_HOME=${SYSTEMC_INSTALL_DIR} \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX}
  ninja -C ${CYCLESIM_BUILD_DIR}
  ninja -C ${CYCLESIM_BUILD_DIR} install
}

# Build ventus cpp cycle-level simulator
build_gpgpu_rtlsim() {
  cd ${GPGPU_DIR}/sim-verilator
  make -j${BUILD_PARALLEL} install RELEASE=1 PREFIX=${VENTUS_INSTALL_PREFIX}
  cd ${GPGPU_DIR}/sim-verilator-nocache
  make -j${BUILD_PARALLEL} install RELEASE=1 PREFIX=${VENTUS_INSTALL_PREFIX}
}

build_gvm() {
  cd ${GPGPU_DIR}/sim-verilator
  make -f gvm.mk -j${BUILD_PARALLEL} RELEASE=1 GVM_TRACE=1 GVM_REF_DIR=${VENTUS_INSTALL_PREFIX}/lib
  make -f gvm.mk install RELEASE=1 PREFIX=${VENTUS_INSTALL_PREFIX} GVM_REF_DIR=${VENTUS_INSTALL_PREFIX}/lib

  cd ${GPGPU_DIR}/sim-verilator-nocache
  make -f gvm.mk -j${BUILD_PARALLEL} RELEASE=1 GVM_TRACE=1 GVM_REF_DIR=${VENTUS_INSTALL_PREFIX}/lib
  make -f gvm.mk install RELEASE=1 PREFIX=${VENTUS_INSTALL_PREFIX} GVM_REF_DIR=${VENTUS_INSTALL_PREFIX}/lib
}

build_gpgpu_rtlsim_gvm() {
  make -C ${GPGPU_DIR} --output-sync=target -j${BUILD_PARALLEL} rtlsim-gvm-install \
    RELEASE=1 \
    PREFIX=${VENTUS_INSTALL_PREFIX} \
    GVM_REF_DIR=${VENTUS_INSTALL_PREFIX}/lib \
    GVM_TRACE=1
}

# Build pocl from THU
build_pocl() {
  mkdir -p ${POCL_BUILD_DIR}
  cd ${POCL_DIR}
  cmake -G Ninja -B ${POCL_BUILD_DIR} -S ${POCL_DIR} \
    -DENABLE_HOST_CPU_DEVICES=OFF \
    -DENABLE_VENTUS=ON \
    -DENABLE_ICD=ON \
    -DDEFAULT_ENABLE_ICD=ON \
    -DENABLE_TESTS=OFF \
    -DSTATIC_LLVM=OFF \
    -DVENTUS_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DINSTALL_OPENCL_HEADERS=ON \
    -DPOCL_INSTALL_OPENCL_HEADER_DIR=${VENTUS_INSTALL_PREFIX}/include/CL \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    # -DCMAKE_C_COMPILER=clang \
    # -DCMAKE_CXX_COMPILER=clang++ \
  ninja -C ${POCL_BUILD_DIR}
  ninja -C ${POCL_BUILD_DIR} install
}

# Configure the standalone libclc tree shared by the OpenCL and Vulkan RT
# paths. The ordinary CMake install includes crt0.o, which is all the Vulkan
# RT linker needs from this tree.
configure_libclc() {
  local libclc_build_dir="${1:-${LIBCLC_BUILD_DIR}}"
  mkdir -p ${libclc_build_dir}
  cmake -G Ninja -B ${libclc_build_dir} -S ${LLVM_DIR}/libclc \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON \
    -DCMAKE_CLC_COMPILER=clang \
    -DCMAKE_LLAsm_COMPILER_WORKS=ON \
    -DCMAKE_CLC_COMPILER_WORKS=ON \
    -DCMAKE_CLC_COMPILER_FORCED=ON \
    -DCMAKE_LLAsm_FLAGS="-target riscv32 -mcpu=ventus-gpgpu -cl-std=CL2.0 -Dcl_khr_fp64 -ffunction-sections -fdata-sections" \
    -DCMAKE_CLC_FLAGS="-target riscv32 -mcpu=ventus-gpgpu -cl-std=CL2.0 -I${LLVM_DIR}/libclc/generic/include -Dcl_khr_fp64  -ffunction-sections -fdata-sections"\
    -DLIBCLC_TARGETS_TO_BUILD="riscv32--" \
    -DCMAKE_CXX_FLAGS="-I ${LLVM_DIR}/llvm/include/ -std=c++17 -Dcl_khr_fp64 -ffunction-sections -fdata-sections" \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE}
    # -DCMAKE_C_COMPILER=clang \
    # -DCMAKE_CXX_COMPILER=clang++ \
}

# Build libclc for pocl.
build_libclc() {
  if [ -e "${LLVM_DIR}/prebuilt" ]; then
    echo "Using prebuilt llvm libclc, skip building"
    cp --reflink=auto -a ${LLVM_DIR}/install ${VENTUS_INSTALL_PREFIX}
    return 0
  fi
  configure_libclc ${LIBCLC_BUILD_DIR}
  ninja -C ${LIBCLC_BUILD_DIR}
  ninja -C ${LIBCLC_BUILD_DIR} install
  # TODO: There are bugs in linking all libclc object files now
  echo "************* Building riscv32 libclc object file ************"
  bash ${LLVM_DIR}/libclc/build_riscv32clc.sh ${LLVM_DIR}/libclc ${LIBCLC_BUILD_DIR} ${VENTUS_INSTALL_PREFIX}

  DstDir=${VENTUS_INSTALL_PREFIX}/share/pocl
  if [ ! -d "${DstDir}" ]; then
    mkdir -p ${DstDir}
  fi
}

# Install only the libclc products required by Vulkan RT. In particular, do
# not run build_riscv32clc.sh: that aggregate OpenCL object is unrelated to the
# RT path and currently exercises a known backend verifier failure.
build_rt_libclc() {
  if [ -e "${LLVM_DIR}/prebuilt" ]; then
    echo "Using prebuilt llvm libclc, skip building"
    cp --reflink=auto -a ${LLVM_DIR}/install ${VENTUS_INSTALL_PREFIX}
  else
    configure_libclc ${RT_LIBCLC_BUILD_DIR}
    ninja -C ${RT_LIBCLC_BUILD_DIR}
    ninja -C ${RT_LIBCLC_BUILD_DIR} install
  fi

  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/crt0.o" ]; then
    echo "Vulkan RT toolchain build did not install ${VENTUS_INSTALL_PREFIX}/lib/crt0.o"
    exit 1
  fi
}

# Build icd_loader
build_icd_loader() {
  cd ${OCL_ICD_DIR}
  ./bootstrap
  mkdir -p ${OCL_ICD_DIR}/build
  cd ${OCL_ICD_DIR}/build
  ../configure --prefix=${VENTUS_INSTALL_PREFIX}
  make -j${BUILD_PARALLEL} && make install
}

build_opencl_cts() {
    cd ${OPENCL_CTS_DIR}
    cmake -S ${OPENCL_CTS_DIR} -B ${OPENCL_CTS_BUILD_DIR} -G Ninja \
        -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
        -DCL_INCLUDE_DIR=${VENTUS_INSTALL_PREFIX}/include \
        -DCL_LIB_DIR=${VENTUS_INSTALL_PREFIX}/lib \
        -DOPENCL_LIBRARIES=OpenCL
    cmake --build ${OPENCL_CTS_BUILD_DIR} -j ${BUILD_PARALLEL}
}

# Test the rodinia test suit
test_rodinia() {
   cd ${RODINIA_DIR}
   make OCL_clean
   make OPENCL
}

# TODO : More test cases of the pocl will be added
test_pocl() {
   cd ${POCL_BUILD_DIR}/examples
   ./vecadd/vecadd
   ./matadd/matadd
   VENTUS_BACKEND=cyclesim ./matadd/matadd
   VENTUS_BACKEND=rtlsim ./matadd/matadd
}

# Export needed path and enviroment variables
export_elements() {
  export PATH=${VENTUS_INSTALL_PREFIX}/bin:$PATH
  export LD_LIBRARY_PATH=${VENTUS_INSTALL_PREFIX}/lib:${LD_LIBRARY_PATH:-}
  export SPIKE_SRC_DIR=${SPIKE_DIR}
  export SPIKE_TARGET_DIR=${VENTUS_INSTALL_PREFIX}
  export VENTUS_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX}
  export POCL_DEVICES="ventus"
  export OCL_ICD_VENDORS=${VENTUS_INSTALL_PREFIX}/lib/libpocl.so
}

# When no need to build llvm, export needed elements
if [[ ! "${PROGRAMS_TOBUILD[*]}" =~ "llvm" ]];then
  export_elements
fi

# Check dep-library systemc is built or not
check_if_systemc_built() {
  if [ ! -f "${SYSTEMC_INSTALL_DIR}/lib-linux64/libsystemc.so" ];then
    echo "Please build library systemc first!"
    exit 1
  fi
}

# Check llvm is built or not
check_if_ventus_llvm_built() {
  if [ ! -d "${VENTUS_INSTALL_PREFIX}" ];then
    echo "Please build llvm first!"
    exit 1
  fi
}

# Check isa simulator is built or not
check_if_spike_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libspike_main.so" ];then
    if [ ! -f "${SPIKE_BUILD_DIR}/lib/libspike_main.so" ];then
      echo "Please build spike isa-simulator first!"
      exit 1
    else
      cp ${SPIKE_BUILD_DIR}/lib/libspike_main.so ${VENTUS_INSTALL_PREFIX}/lib
    fi
  fi
}

check_if_gvmref_built() {
  if [ -f "${SPIKE_BUILD_DIR}/libgvmref.so" ]; then
    cp ${SPIKE_BUILD_DIR}/libgvmref.so ${VENTUS_INSTALL_PREFIX}/lib
    return 0
  fi
  if [ -f "${SPIKE_BUILD_DIR}/lib/libgvmref.so" ]; then
    cp ${SPIKE_BUILD_DIR}/lib/libgvmref.so ${VENTUS_INSTALL_PREFIX}/lib
    return 0
  fi
  echo "Please build spike gvm reference library (libgvmref.so) for GVM!"
  exit 1
}

# Check gpgpu rtlsim sim-verilator is built or not
check_if_rtlsim_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libVentusRTL.so" ];then
    echo "Please build Ventus Chisel RTL sim-verilator (rtlsim) first!"
    exit 1
  fi
}

check_if_gvm_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libVentusGVM-withcache.so" ]; then
    echo "Please build Ventus GVM backend (withcache) first (use --build gvm)!"
    exit 1
  fi
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libVentusGVM-nocache.so" ]; then
    echo "Please build Ventus GVM backend (nocache) first (use --build gvm)!"
    exit 1
  fi
}

# Check gpgpu cpp cycle-level simulator is built or not
check_if_cyclesim_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libVentusCycleSim.so" ];then
    echo "Please build Ventus C++ cycle-level simulator (cyclesim) first!"
    exit 1
  fi
}

# Check ventus sbtsim simulator is built or not
check_if_sbtsim_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/bin/sbt_ptx" ];then
    echo "Please build Ventus CUDA-PTX simulator (sbtsim) first!"
    exit 1
  fi
}

# Check ocl-icd loader is built or not
# since pocl need ocl-icd and llvm built first
check_if_ocl_icd_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libOpenCL.so" ];then
    echo "Please build ocl-icd first!"
    exit 1
  fi
}

check_if_pocl_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/pocl/libpocl-devices-ventus.so" ];then
    echo "Please build POCL first!"
    exit 1
  fi
}

is_rtlsim_program() {
  [ "$1" = "rtlsim" ] || [ "$1" = "rtl" ] || [ "$1" = "gpgpu" ]
}

rtlsim_requested=false
gvm_requested=false
rtlsim_gvm_built=false

for program in "${PROGRAMS_TOBUILD[@]}"
do
  if is_rtlsim_program "${program}"; then
    rtlsim_requested=true
  elif [ "${program}" = "gvm" ]; then
    gvm_requested=true
  fi
done

# Process build options
for program in "${PROGRAMS_TOBUILD[@]}"
do
  if [ "${program}" == "systemc" ];then
    build_systemc
  elif [ "${program}" == "llvm" ];then
    build_llvm
    export_elements
  elif [ "${program}" == "rt-toolchain" ];then
    build_llvm
    export_elements
    build_rt_libclc
  elif [ "${program}" == "rt-libclc" ];then
    check_if_ventus_llvm_built
    build_rt_libclc
  elif [ "${program}" == "ocl-icd" ];then
    build_icd_loader
  elif [ "${program}" == "libclc" ];then
    check_if_ventus_llvm_built
    build_libclc
  elif [ "${program}" == "spike" ]; then
    build_spike
  elif [ "${program}" == "rtlsim" ] || [ "${program}" == "rtl" ] || [ "${program}" == "gpgpu" ]; then
    if [ "${rtlsim_requested}" = "true" ] && [ "${gvm_requested}" = "true" ]; then
      if [ "${rtlsim_gvm_built}" = "false" ]; then
        build_gpgpu_rtlsim_gvm
        rtlsim_gvm_built=true
      fi
    else
      build_gpgpu_rtlsim
    fi
  elif [ "${program}" == "cyclesim" ] || [ "${program}" == "simulator" ]; then
    check_if_systemc_built
    build_gpgpu_cyclesim
  elif [ "${program}" == "sbt" ] || [ "${program}" == "sbtsim" ] || [ "${program}" == "ptx" ] || [ "${program}" == "ptxsim" ]; then
    if [ "${NVIDIA_DRIVER_AVAILABLE}" = "true" ]; then
      build_sbtsim
    else
      echo "WARNING: Skipping sbtsim build -- NVIDIA driver not available."
    fi
  elif [ "${program}" == "gvm" ]; then
    if [ "${rtlsim_requested}" = "true" ] && [ "${gvm_requested}" = "true" ]; then
      if [ "${rtlsim_gvm_built}" = "false" ]; then
        build_gpgpu_rtlsim_gvm
        rtlsim_gvm_built=true
      fi
    else
      build_gvm
    fi
  elif [ "${program}" == "driver" ]; then
    check_if_spike_built
    check_if_cyclesim_built
    check_if_rtlsim_built
    check_if_gvm_built
    check_if_gvmref_built
    if [ "${NVIDIA_DRIVER_AVAILABLE}" = "true" ]; then
      check_if_sbtsim_built
    fi
    build_driver
  elif [ "${program}" == "driver-spike" ] || [ "${program}" == "rt-driver" ]; then
    check_if_spike_built
    build_spike_driver
  elif [ "${program}" == "mesa" ] || [ "${program}" == "mesa-ventus" ]; then
    build_mesa_ventus
  elif [ "${program}" == "rt-workload" ]; then
    build_rt_workload
  elif [ "${program}" == "pocl" ]; then
    check_if_ventus_llvm_built
    check_if_ocl_icd_built
    build_pocl
  elif [ "${program}" == "rodinia" ]; then
    check_if_ventus_llvm_built
    check_if_ocl_icd_built
    check_if_spike_built
    test_rodinia
  elif [ "${program}" == "cts" ] || [ "${program}" == "opencl-cts" ] || [ "${program}" == "OpenCL-CTS" ]; then
    check_if_pocl_built
    check_if_spike_built
    build_opencl_cts
  elif [ "${program}" == "test-pocl" ]; then
    check_if_ventus_llvm_built
    check_if_ocl_icd_built
    check_if_spike_built
    test_pocl
  else
    echo "Invalid build options: \"${program}\" , try $0 --help for help"
    exit 1
  fi
done
