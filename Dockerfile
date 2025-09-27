FROM buildpack-deps:24.04 AS ventus-dev-os-base
RUN env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get update \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get upgrade -y \
    && env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY apt-get install -y sudo vim neovim \
       mold ccache ninja-build cmake clang clangd clang-format gdb bash-completion \
       help2man perl perl-doc flex bison libfl2 libfl-dev zlib1g zlib1g-dev libgoogle-perftools-dev numactl \
       libfmt-dev libspdlog-dev libelf-dev libyaml-cpp-dev device-tree-compiler bsdmainutils ruby default-jdk \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* \
    && adduser ubuntu sudo

FROM ventus-dev-os-base AS builder_verilator
WORKDIR /tmp/verilator
RUN git clone https://github.com/verilator/verilator \
    && cd verilator \
    && git checkout v5.034 \
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
    && git clone https://github.com/Humber-186/ventus-env.git ventus \
    && cd ventus


ENTRYPOINT ["/bin/bash", "/home/ubuntu/ventus/build-ventus.sh"]
