FROM buildpack-deps:24.04 AS ventus-dev-os-base
RUN env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get update \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get upgrade -y \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get install -y sudo vim neovim \
       mold ccache ninja-build cmake clang clangd clang-format gdb bash-completion \
       help2man perl perl-doc flex bison libfl2 libfl-dev zlib1g zlib1g-dev libgoogle-perftools-dev numactl \
       libfmt-dev libspdlog-dev libelf-dev libyaml-cpp-dev nlohmann-json3-dev \
       device-tree-compiler bsdmainutils ruby default-jdk python3-tqdm python3-venv python3-pip \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
    && adduser ubuntu sudo

FROM ventus-dev-os-base AS builder_verilator
WORKDIR /tmp/verilator
RUN git clone https://github.com/verilator/verilator.git -b v5.034 --depth=1 \
    && cd verilator \
    && autoconf \
    && ./configure --prefix=/opt/verilator/5.034 \
    && make -j$(nproc) \
    && make test \
    && make install \
    && curl -L https://github.com/llvm/circt/releases/download/firtool-1.62.0/firrtl-bin-linux-x64.tar.gz -o /tmp/firtool.tar.gz \
    && tar -xzf /tmp/firtool.tar.gz -C /opt

FROM ventus-dev-os-base AS ventus-dev-os
COPY --from=builder_verilator /opt /opt

FROM ventus-dev-os AS ventus-dev-repo-clone
USER ubuntu
WORKDIR /home/ubuntu
ENV PATH="/opt/verilator/5.034/bin:/opt/firtool-1.62.0/bin:${PATH}"
RUN echo "export PATH=\"/opt/verilator/5.034/bin:/opt/firtool-1.62.0/bin:\${PATH}\"" >> /home/ubuntu/.bashrc \
    && git clone https://github.com/Humber-186/ventus-env.git ventus -b MICRO2025 \
    && cd ventus \
    && make init DATASET_URL="https://cloud.tsinghua.edu.cn/f/01efd6a13eba4402a0fb/?dl=1"

# FROM ventus-dev-repo-clone AS ventus-dev-llvm
# USER ubuntu
# WORKDIR /home/ubuntu/ventus
# COPY --chown=ubuntu:ubuntu ./build-ventus.sh /home/ubuntu/ventus/build-ventus.sh
# RUN bash build-ventus.sh --build systemc \
#     && bash build-ventus.sh --build llvm

# FROM ventus-dev-llvm AS ventus-dev-spike
# USER ubuntu
# WORKDIR /home/ubuntu/ventus
# RUN bash build-ventus.sh --build ocl-icd \
#     && bash build-ventus.sh --build libclc \
#     && bash build-ventus.sh --build spike

# FROM ventus-dev-spike AS ventus-dev-rtlsim
# USER ubuntu
# WORKDIR /home/ubuntu/ventus
# ENV SHELL=/bin/bash
# RUN bash build-ventus.sh --build rtlsim

# FROM ventus-dev-rtlsim AS ventus-dev-cyclesim
# USER ubuntu
# WORKDIR /home/ubuntu/ventus
# RUN bash build-ventus.sh --build cyclesim

# FROM ventus-dev-cyclesim AS ventus-dev
# USER ubuntu
# WORKDIR /home/ubuntu/ventus
# RUN bash build-ventus.sh --build driver \
#     && bash build-ventus.sh --build pocl \
#     && bash build-ventus.sh --build rodinia \
#     && bash build-ventus.sh --build test-pocl

FROM ventus-dev-repo-clone AS ventus-dev
USER ubuntu
WORKDIR /home/ubuntu/ventus
RUN bash build-ventus.sh \
    && rm -rf systemc/build llvm/build llvm/build-libcyc spike/build \
              cyclesim/build gpgpu/sim-verilator/build gpgpu/sim-verilator-nocache/build \
              ocl-icd/build driver/build rodinia_data.tar.xz \
    && ccache --clear

FROM ventus-dev AS ventus-tutorial
USER ubuntu
WORKDIR /home/ubuntu/ventus
COPY --chown=ubuntu:ubuntu _bug_patch /tmp/bug_patch
RUN bash build-ventus.sh --build gpgpu \
    && git -C gpgpu/ worktree add ../_gpgpu_with_bug \
    && cd _gpgpu_with_bug/ \
    && rm -r dependencies && ln -s ../gpgpu/dependencies . \
    && patch -p 1 < /tmp/bug_patch \
    && make -C sim-verilator/ -f gvm.mk -j RELEASE=1 GVM_TRACE=0 VLIB_NPROC_DUT=4 \
    && cd ../.. && python3 -m venv pyenv \
    && ./pyenv/bin/pip install "jupyterhub>=5.3" jupyterlab notebook bash_kernel jupyterlab_limit_output jupyterlab_myst \
    && ./pyenv/bin/python3 -m bash_kernel.install

FROM ventus-dev-os AS ventus
USER ubuntu
WORKDIR /home/ubuntu/ventus
COPY --chown=ubuntu:ubuntu --from=ventus-dev \
     /home/ubuntu/ventus/env.sh /home/ubuntu/ventus/install \
     /home/ubuntu/ventus/regression-test.py \
     /home/ubuntu/ventus/pocl /home/ubuntu/ventus/rodinia \
     /home/ubuntu/ventus/
