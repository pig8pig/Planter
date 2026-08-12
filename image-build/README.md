# P4Pi Image Build with Planter (GSoC 2026)

Build overlay for producing a P4Pi system image with Planter and the
`p4c-dpdk` target pre-installed.

## Background

The original P4Pi `pi-gen` build depends on OpenSUSE OBS repositories
(`home:p4pi`, `home:p4edge`) which no longer exist:

$ curl https://api.opensuse.org/public/build/home:p4pi
<status code="unknown_project">Project not found: home:p4pi</status>


This overlay removes that dependency by building p4c and BMv2 from pinned
upstream sources instead.

## What this image contains

Included:
- p4c v1.2.5.16 built from source, with BMv2 and DPDK backends enabled
  (`p4c-bm2-ss`, `p4c-dpdk`)
- BMv2 1.15.5 built from source (`simple_switch`, `simple_switch_CLI`)
- Planter (branch `gsoc-p4c-dpdk`) at `/home/pi/Planter`
- `dpdk-pipeline` built at `/home/pi/dpdk_pipeline_build`, with the
  `n_pkts_max` initialisation patch applied
- Debian `dpdk` / `dpdk-dev` / `libdpdk-dev` packages

Omitted:
- PI, T4P4S, the custom P4Pi kernel and headers, `p4pi-web`, the old
  package-based examples, `dpdk-kmods-dkms`

## Usage

```bash
git clone https://github.com/p4lang/p4pi.git
cd p4pi/pi-gen

# Apply modifications to existing files
git apply /path/to/p4pi-planter-build.patch

# Copy in the new build stages
cp -r /path/to/pi-gen-overlay/stage2/* stage2/
cp -r /path/to/pi-gen-overlay/stage3/* stage3/
chmod +x stage2/*/00-run.sh stage3/*/00-run.sh

# Build
sudo bash build-docker.sh
```

Output lands in `deploy/`.

## Build environment

Built on Ubuntu 24.04 under WSL2 with Docker. Cross-building an ARM64 image on
x86 requires QEMU binfmt registration:

```bash
sudo apt-get install -y qemu-user-static binfmt-support
sudo update-binfmts --enable qemu-aarch64
sudo docker run --rm --privileged multiarch/qemu-user-static --reset -p yes
```

Build time was approximately 9 hours, dominated by compiling p4c under
emulation. 16 GB RAM was sufficient at `make -j2`.

## Issues encountered

Recorded here because several are not documented upstream and cost significant
time to diagnose.

**BMv2 `--without-pi` is a no-op.** In `configure.ac` the option is declared as
`AC_ARG_WITH([pi], ..., [want_pi=yes], [])` — the action-if-given sets
`want_pi=yes` regardless of whether you pass `--with-pi` or `--without-pi`.
Passing `--without-pi` therefore *enables* PI. Omitting the flag entirely is
what disables it.

**Undocumented BMv2 configure dependencies.** BMv2 1.15.5's README omits
`libxxhash-dev`, `libjsoncpp-dev`, and protobuf (`libprotobuf-dev`,
`libprotoc-dev`, `protobuf-compiler`), all of which its `configure` requires.
`install_deps.sh` is the more reliable reference.

**Packages that fail under QEMU emulation.** `python3-kms++` and
`python3-picamera2` fail during post-install configuration, and `py3compile`
segfaults (exit 139) when generating bytecode for ARM packages. The overlay
stubs out `py3compile` during the build and removes the camera packages;
bytecode is regenerated on first run on real hardware. `apt-listchanges` also
fails and is removed.

**Transient archive timeouts.** `archive.raspberrypi.org` intermittently times
out during long builds. `Acquire::Retries "5"` is set to mitigate this.

## Verification status

The image builds cleanly and the in-image smoke test passes — a minimal
v1model P4 program compiles to BMv2 JSON inside the built rootfs.

The image has **not** been flashed and booted on physical hardware. Boot
verification remains outstanding.

## Files

| File | Purpose |
|---|---|
| `p4pi-planter-build.patch` | Modifications to existing pi-gen files |
| `pi-gen-overlay/` | New build stages |
| `build-success.log` | Log from the successful build |
