#!/bin/bash
# =============================================================================
# Planter dpdk-target toolchain on Raspberry Pi OS Lite 64-bit (Debian 13
# trixie, aarch64), built natively.  Adapted from the P4Pi image overlay
# (image-build/pi-gen-overlay/stage3/02-p4c-bmv2 and 03-planter).
#
#   sudo apt install -y tmux && tmux new -s build
#   ~/Planter/scripts/setup_pi5_trixie.sh 2>&1 | tee ~/setup.log
#
# Resumable: each finished stage drops a marker in ~/.planter_setup, so after
# a reset just re-run it (check `vcgencmd get_throttled` first; want 0x0).
# =============================================================================
set -euo pipefail

P4C_TAG=v1.2.5.16
P4C_SRC=$HOME/p4c
JOBS=${JOBS:-3}
VENV=$HOME/planter-venv
STATE=$HOME/.planter_setup
mkdir -p "$STATE"

stage() { echo; echo "=== [$(date +%T)] $1 ==="; }
done_mark() { touch "$STATE/$1"; }
is_done() { [ -f "$STATE/$1" ]; }

# Keep sudo alive for the multi-hour p4c build.
sudo -v
while true; do sudo -n true; sleep 60; done 2>/dev/null &
SUDO_KEEPALIVE=$!
trap 'kill $SUDO_KEEPALIVE 2>/dev/null || true' EXIT

# --- 1. Base packages --------------------------------------------------------
if ! is_done apt; then
    stage "1. apt full-upgrade + build deps + DPDK"
    sudo apt-get update
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y install --no-install-recommends \
        autoconf automake bison build-essential ccache cmake flex git g++ \
        ninja-build meson pkg-config tmux \
        libboost-dev libboost-program-options-dev libboost-thread-dev \
        libboost-test-dev libboost-system-dev libboost-filesystem-dev \
        libboost-iostreams-dev libboost-graph-dev \
        libevent-dev libffi-dev libgmp-dev libjsoncpp-dev \
        libpcap-dev libreadline-dev libssl-dev libtool libtool-bin \
        libxxhash-dev libgc-dev libfl-dev \
        python3-dev python3-pip python3-venv python3-setuptools python3-ply \
        wget curl ca-certificates tcpdump \
        libprotobuf-dev libprotoc-dev protobuf-compiler \
        dpdk dpdk-dev
    done_mark apt
fi

# DPDK source must match the apt package exactly (24.11.4 vs 24.11.0 mismatch
# broke libbuild before): the pipeline example and rte_swx_pipeline_internal.h
# are not shipped in dpdk-dev.
DPDK_VER=$(pkg-config --modversion libdpdk)
DPDK_SRC=$HOME/dpdk-$DPDK_VER
echo "libdpdk (apt) = $DPDK_VER"

# --- 2. p4c ------------------------------------------------------------------
if ! is_done p4c; then
    stage "2. p4c $P4C_TAG (BMv2 + DPDK backends), -j$JOBS"
    if [ ! -d "$P4C_SRC/.git" ]; then
        git clone --depth 1 --branch "$P4C_TAG" --recursive --shallow-submodules \
            https://github.com/p4lang/p4c.git "$P4C_SRC"
    fi
    mkdir -p "$P4C_SRC/build"
    cd "$P4C_SRC/build"
    [ -f CMakeCache.txt ] || cmake .. \
        -DCMAKE_BUILD_TYPE=RELEASE \
        -DENABLE_BMV2=ON \
        -DENABLE_DPDK=ON \
        -DENABLE_EBPF=OFF \
        -DENABLE_UBPF=OFF \
        -DENABLE_P4TEST=OFF \
        -DENABLE_P4TC=OFF \
        -DENABLE_P4FMT=OFF \
        -DENABLE_TEST_TOOLS=OFF \
        -DENABLE_DOCS=OFF \
        -DENABLE_GTESTS=OFF
    make -j"$JOBS"
    sudo make install
    sudo ldconfig
    p4c --version
    done_mark p4c
fi

# --- 3. DPDK source tree + pipeline example ----------------------------------
if ! is_done dpdk; then
    stage "3. DPDK $DPDK_VER source + examples/pipeline"
    if [ ! -d "$DPDK_SRC" ]; then
        git clone --depth 1 --branch "v$DPDK_VER" \
            https://github.com/DPDK/dpdk-stable.git "$DPDK_SRC"
    fi
    [ "$(cat "$DPDK_SRC/VERSION")" = "$DPDK_VER" ] || {
        echo "DPDK source $(cat "$DPDK_SRC/VERSION") != apt $DPDK_VER"; exit 1; }

    # "pipeline libbuild" compiles the generated C with -I $RTE_INSTALL_DIR/build;
    # rte_build_config.h only exists in a configured tree or the dev package.
    mkdir -p "$DPDK_SRC/build"
    cp "$(dpkg -L dpdk-dev libdpdk-dev 2>/dev/null | grep '/rte_build_config.h$' | head -1)" \
        "$DPDK_SRC/build/"

    # libbuild hardcodes lib/eal/x86/include; point it at this CPU's EAL arch.
    case "$(uname -m)" in
        aarch64|arm*) sed -i 's|lib/eal/x86/include|lib/eal/arm/include|' \
                          "$DPDK_SRC/examples/pipeline/cli.c" ;;
    esac

    make -C "$DPDK_SRC/examples/pipeline"
    ! ldd "$DPDK_SRC/examples/pipeline/build/pipeline" | grep 'not found'
    done_mark dpdk
fi

# --- 4. Planter Python deps (trixie is externally-managed -> venv) -----------
# requirements_pip3.txt pins 2020-era wheels (numpy 1.19, torch 1.4) that do
# not exist for Python 3.13; use the unpinned set from 03-planter instead.
if ! is_done python; then
    stage "4. Python venv at $VENV"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip
    "$VENV/bin/pip" install scikit-learn numpy pandas scapy xgboost matplotlib \
        joblib pydotplus packaging jsonschema seaborn tqdm ipython wget \
        category_encoders
    done_mark python
fi

stage "Done"
p4c --version
echo "libdpdk $(pkg-config --modversion libdpdk)"
echo "export RTE_INSTALL_DIR=$DPDK_SRC"
echo "source $VENV/bin/activate"
