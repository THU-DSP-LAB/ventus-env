# Project Agent Rules

@README.md

* ventus 有关的软件工具使用 `./install` 目录下的可执行程序或库，通过 `source ./env.sh` 设置环境变量让 OpenCL App 在 ventus 环境中运行
* ventus 编译器：`./install/bin/clang -cl-std=CL2.0 -target riscv32 -mcpu=ventus-gpgpu kernel.cl -o kernel.riscv -nodefaultlibs -Wl,${VENTUS_ENV_PATH}/install/lib/crt0.o -Wl,${VENTUS_ENV_PATH}/install/lib/riscv32clc.o -Wl,--gc-sections -L${VENTUS_ENV_PATH}/install/lib -lworkitem -I${VENTUS_ENV_PATH}/installinclude/clc -O1 -Wl,-T,${VENTUS_ENV_PATH}/install/lib/ldscripts/ventus/elf32lriscv.ld -Wl,--init=${KERNEL_FUNC_NAME} -w -D__opencl_c_generic_address_space=1 -D__opencl_c_named_address_space_builtins=1 -D__OPENCL_VERSION__=200` 注意替换 `${VENTUS_ENV_PATH}` 和 `${KERNEL_FUNC_NAME}`
* ventus 反汇编器：`./install/bin/llvm-objdump -d --mattr=+v,+zfinx kernel.riscv > kernel.dump`

运行仿真时 rtl, rtl-nocache, gvm 等会直接输出日志到 stdout 推荐重定向到文件
使用 `VENTUS_SPIKE_LOG=1` 时 spike 会输出日志到当前 cwd 下的文件
通常日志文件都极长，禁止直接读入上下文，即使搜索也推荐限制最大输出长度

## Tools Script Rules

- `tools/` 一级目录只放用户直接执行的入口脚本，避免把同一工具的实现文件散落在 `tools/` 根目录。
- 如果一个工具需要多文件实现，必须放入统一子目录：`tools/<tool_name>/`。
- 多文件工具必须在 `tools/` 一级提供唯一入口脚本：`tools/<tool_name>.py`。
- 入口脚本必须可直接通过标准方式运行，例如 `python3 tools/<tool_name>.py ...`，不得依赖 `PYTHONPATH`、临时 shell 注入或其他临时环境技巧。
- 入口脚本顶部注释需简要说明：
  - 背景需求
  - 实现方案或运行流程
  - 使用方法
  - 关键维护说明
- 若某个 `tools/` 一级脚本没有对应的额外文档，则该脚本本身必须保持单文件可维护性；不要把维护所需上下文藏到分散文件里。
- 多文件工具的共享逻辑、模型、封装和测试辅助代码应收敛在同名子目录中，不要继续在 `tools/` 根目录新增平铺模块。
- `tools/` 下新增或修改 Python 工具时，优先使用包内导入、标准模块结构和明确入口，不要在计划、实现或测试命令中引入 `PYTHONPATH` 依赖。
