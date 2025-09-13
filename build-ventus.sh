#!/usr/bin/env bash

set -euo pipefail

DIR=$(cd "$(dirname "${0}")" &> /dev/null && (pwd -W 2> /dev/null || pwd))
VENTUS_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX:-${DIR}/install}
PROGRAMS_TOBUILD_DEFAULT=(systemc llvm ocl-icd libclc spike driver pocl rodinia test-pocl)
PROGRAMS_TOBUILD_DEFAULT_FULL=(systemc llvm ocl-icd libclc spike rtlsim cyclesim driver pocl rodinia test-pocl)
PROGRAMS_TOBUILD=(${PROGRAMS_TOBUILD_DEFAULT_FULL[@]})

BUILD_PARALLEL=$(( $(nproc) * 2 / 3 ))

# Helper function
help() {
  cat <<END

Build [systemc llvm, pocl, ocl-icd, libclc, driver, spike, rtlsim|gpgpu, cyclesim|simulator] programs.
Run the rodinia and test-pocl test suites.
Read ${DIR}/llvm/README.md to get started.

Usage: ${DIR}/$(basename ${0})
                          [--build <build programs>]
                          [--help | -h]

Options:
  --build <build programs>
    Chosen programs to build : [${PROGRAMS_TOBUILD}]
    Option format : "llvm;pocl", string are separated by semicolon.
    ( Note that quotation marks are necessary, or bash will parse the semicolon as command ending )
    Default : "llvm;ocl-icd;libclc;spike;rtlsim;cyclesim;driver;pocl;rodinia;test-pocl"
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

# Need to get the cpp-cycle-level-simulator folder from enviroment variables
CYCLESIM_DIR=${CYCLESIM_DIR:-${DIR}/simulator}
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

# Need to get the ventus-spike folder from enviroment variables
SPIKE_DIR=${SPIKE_DIR:-${DIR}/spike}
check_if_program_exits ${SPIKE_DIR} "spike"
SPIKE_BUILD_DIR=${SPIKE_DIR}/build

# Need to get the icd_loader folder from enviroment variables
OCL_ICD_DIR=${OCL_ICD_DIR:-${DIR}/ocl-icd}
check_if_program_exits ${OCL_ICD_DIR} "ocl-icd"
OCL_ICD_BUILD_DIR=${OCL_ICD_DIR}/build

# Need to get the gpu-rodinia folder from enviroment variables
RODINIA_DIR=${RODINIA_DIR:-${DIR}/rodinia}
check_if_program_exits ${RODINIA_DIR} "gpu-rodinia"

# Build library systemc: depended by cyclesim
build_systemc() {
  cd ${SYSTEMC_DIR}
  ./config/bootstrap
  mkdir -p ${SYSTEMC_DIR}/build
  cd ${SYSTEMC_DIR}/build
  ../configure 'CXXFLAGS=-std=c++20' --prefix=${SYSTEMC_INSTALL_DIR} --enable-debug
  make -j${BUILD_PARALLEL}
  make check
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
  cmake -G Ninja -B ${LLVM_BUILD_DIR} -S ${LLVM_DIR}/llvm \
    -DLLVM_CCACHE_BUILD=ON \
    -DLLVM_OPTIMIZED_TABLEGEN=ON \
    -DLLVM_PARALLEL_LINK_JOBS=12 \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DLLVM_ENABLE_PROJECTS="clang;lld;libclc" \
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
  mkdir -p ${DRIVER_BUILD_DIR}
  cd ${DRIVER_DIR}
  cmake -G Ninja -B ${DRIVER_BUILD_DIR} -S ${DRIVER_DIR} \
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DVENTUS_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DSPIKE_SRC_DIR=${SPIKE_DIR} \
    -DDRIVER_ENABLE_AUTOSELECT=ON \
    -DDRIVER_ENABLE_RTLSIM=ON \
    -DDRIVER_ENABLE_CYCLESIM=ON
    # -DCMAKE_C_COMPILER=clang \
    # -DCMAKE_CXX_COMPILER=clang++ \
  ninja -C ${DRIVER_BUILD_DIR}
  ninja -C ${DRIVER_BUILD_DIR} install
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
    -DCMAKE_BUILD_TYPE=${BUILD_TYPE} \
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX}
  ninja -C ${CYCLESIM_BUILD_DIR}
  ninja -C ${CYCLESIM_BUILD_DIR} install
}

# Build ventus cpp cycle-level simulator
build_gpgpu_rtlsim() {
  cd ${GPGPU_DIR}/sim-verilator-nocache
  make -j${BUILD_PARALLEL} RELEASE=1
  make install RELEASE=1 PREFIX=${VENTUS_INSTALL_PREFIX}
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
    -DCMAKE_INSTALL_PREFIX=${VENTUS_INSTALL_PREFIX} \
    -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    # -DCMAKE_C_COMPILER=clang \
    # -DCMAKE_CXX_COMPILER=clang++ \
  ninja -C ${POCL_BUILD_DIR}
  ninja -C ${POCL_BUILD_DIR} install
}

# Build libclc for pocl
build_libclc() {
  if [ -e "${LLVM_DIR}/prebuilt" ]; then
    echo "Using prebuilt llvm libclc, skip building"
    cp --reflink=auto -a ${LLVM_DIR}/install ${VENTUS_INSTALL_PREFIX}
    return 0
  fi
  if [ ! -d "${LIBCLC_BUILD_DIR}" ]; then
    mkdir ${LIBCLC_BUILD_DIR}
  fi
  cd ${LIBCLC_BUILD_DIR}
  cmake -G Ninja -B ${LIBCLC_BUILD_DIR} -S ${LLVM_DIR}/libclc \
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
  ninja
  ninja install
  # TODO: There are bugs in linking all libclc object files now
  echo "************* Building riscv32 libclc object file ************"
  bash ${LLVM_DIR}/libclc/build_riscv32clc.sh ${LLVM_DIR}/libclc ${LIBCLC_BUILD_DIR} ${VENTUS_INSTALL_PREFIX} || true

  DstDir=${VENTUS_INSTALL_PREFIX}/share/pocl
  if [ ! -d "${DstDir}" ]; then
    mkdir -p ${DstDir}
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
  export SYSTEMC_HOME=${SYSTEMC_INSTALL_DIR}
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

# Check gpgpu rtlsim sim-verilator is built or not
check_if_rtlsim_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libVentusRTL.so" ];then
    echo "Please build Ventus Chisel RTL sim-verilator (rtlsim) first!"
    exit 1
  fi
}

# Check gpgpu cpp cycle-level simulator is built or not
check_if_cyclesim_built() {
  if [ ! -f "${VENTUS_INSTALL_PREFIX}/lib/libVentusCycleSim.so" ];then
    echo "Please build Ventus Chisel C++ cycle-level simulator (cyclesim) first!"
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

# Process build options
for program in "${PROGRAMS_TOBUILD[@]}"
do
  if [ "${program}" == "systemc" ];then
    build_systemc
  elif [ "${program}" == "llvm" ];then
    build_llvm
    export_elements
  elif [ "${program}" == "ocl-icd" ];then
    build_icd_loader
  elif [ "${program}" == "libclc" ];then
    check_if_ventus_llvm_built
    build_libclc
  elif [ "${program}" == "spike" ]; then
    build_spike
  elif [ "${program}" == "rtlsim" ] || [ "${program}" == "rtl" ] || [ "${program}" == "gpgpu" ]; then
    build_gpgpu_rtlsim
  elif [ "${program}" == "cyclesim" ] || [ "${program}" == "simulator" ]; then
    check_if_systemc_built
    build_gpgpu_cyclesim
  elif [ "${program}" == "driver" ]; then
    check_if_spike_built
    check_if_cyclesim_built
    check_if_rtlsim_built
    build_driver
  elif [ "${program}" == "pocl" ]; then
    check_if_ventus_llvm_built
    check_if_ocl_icd_built
    build_pocl
  elif [ "${program}" == "rodinia" ]; then
    check_if_ventus_llvm_built
    check_if_ocl_icd_built
    check_if_spike_built
    test_rodinia
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
