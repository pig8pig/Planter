# p4c-dpdk Target for Planter

Documentation for the `p4c-dpdk` target added on the `gsoc-p4c-dpdk` branch.
For general Planter documentation, see the [main README](./README.md).

Google Summer of Code 2026 · Project 3.3 · The P4 Language Consortium
Contributor: Yuzhong (WeiWei) Luo · Mentor: Dr Peng Qian · University of Oxford

---

## Overview

Planter converts trained scikit-learn models into P4 programs and match–action
table entries, then deploys them to a target. Its low-cost options previously
ended at BMv2, which suits functional validation but not sustained traffic. This
branch adds the missing path to `p4c-dpdk`, so the same generated model can run
on DPDK's SWX software pipeline on [P4Pi](https://github.com/p4lang/p4pi).

Three contributions:

1. **A `p4c-dpdk` target adapter** — `src/targets/dpdk/software/`
2. **Fixes to Planter's PSA architecture generator** — `src/architectures/psa/`,
   which had not previously been validated for ML model generation
3. **A P4Pi image build overlay** — `image-build/`, rebuilding the P4Pi image
   with Planter preinstalled

---

## Quick start

Assumes a Raspberry Pi 4 running the
[P4Pi SIGCOMM 2022 image](https://github.com/p4lang/p4pi/releases).

```bash
git clone -b gsoc-p4c-dpdk https://github.com/pig8pig/Planter.git
cd Planter
chmod +x setup.sh && ./setup.sh      # dependencies + dpdk-pipeline build
python3 Planter.py -m
```

At the prompts:

| Prompt | Value |
|---|---|
| Model | `DT` or `RF` |
| Type | `EB` |
| Dataset | `Iris` |
| Architecture | `psa` |
| Device | `dpdk` |
| Mode | `software` |

The run trains the model, generates PSA P4, compiles it with `p4c-dpdk`, patches
the `.spec`, writes table entry files, launches `dpdk-pipeline`, and reports
accuracy against the scikit-learn baseline.

---

## Results

Iris, 70/30 split, Raspberry Pi 4, DPDK 24.11. On 24.11 the DPDK pipeline
reproduces the software match–action tables exactly:

| Model | scikit-learn | DPDK 24.11 |
|---|---|---|
| Decision Tree (depth 4) | 95.56% | **95.56%** |
| Random Forest (5 trees, depth 4) — run 1 | 93.33% | **93.33%** |
| Random Forest — run 2 | 95.56% | **95.56%** |
| Random Forest — run 3 | 95.56% | **95.56%** |
| Random Forest — run 4 | 97.78% | **97.78%** |

The four Random Forest rows are four independently trained models, not repeats of
one model (RF training is unseeded — see [Known issues](#known-issues)). One of
them was additionally checked per sample: 45/45 agreement with scikit-learn.

Native ternary match also shrinks the tables: a feature table drops from 128
exact-match entries to 11–13 wildcard entries, roughly 90% fewer.

---

## How the adapter works

`src/targets/dpdk/software/run_model.py` implements the following stages:

| Function | Responsibility |
|---|---|
| `compile_p4_dpdk()` | Runs `p4c --target dpdk --arch psa`, locates the generated `.spec` |
| `detect_dpdk_version()` | Resolves the DPDK version from `pkg-config`, a config override, or `PLANTER_DPDK_VERSION`; gates every version-specific behaviour |
| `patch_spec_file()` | Applies the version-appropriate `.spec` fixes (20.11 workarounds are skipped on 21.08+) |
| `generate_entry_files()` | Writes table entries: native `<val>/<mask> priority <N>` on 21.08+, exact-match expansion on 20.11 (model-aware: DT and RF differ) |
| `generate_cli_script()` | Writes the `dpdk-pipeline` CLI script with the correct table load order |
| `run_dpdk_pipeline()` | Launches the pipeline in `--no-huge` mode and captures output |

`test_model.py` builds the test packets, runs the pipeline, decodes the result
field from the output pcap (including a byte-order correction) and reports
accuracy.

---

## PSA generator fixes

`src/architectures/psa/p4_generator.py` existed but had not been exercised for ML
model generation, and its output did not compile. Eight fixes were required:

1. Missing `struct metadata_t {}` wrapper — metadata fields were emitted at file
   top level
2. Missing `out empty_t` parameters in the ingress deparser
3. Missing `out empty_t` parameters in the egress deparser
4. `empty_t` not defined in this build's `psa.p4` — now emitted explicitly
5. Missing trailing `in empty_t` parameters in the ingress parser
6. Missing trailing `in empty_t` parameters in the egress parser
7. Wrong metadata type in the egress control
   (`psa_egress_parser_input_metadata_t` → `psa_egress_input_metadata_t`)
8. The BMv2 target adapter always invoked the v1model compiler regardless of the
   configured architecture

After these, both `p4c-bm2-psa` and `p4c --target dpdk --arch psa` compile the
generated PSA cleanly. These fixes affect any use of the PSA generator, not only
the DPDK path.

---

## DPDK version support

The adapter supports both the DPDK 20.11.5 that ships with the P4Pi SIGCOMM 2022
image and current DPDK (tested on 24.11). `detect_dpdk_version()` resolves the
version from `pkg-config`, an explicit config override, or the
`PLANTER_DPDK_VERSION` environment variable; `DPDK_NATIVE_TERNARY_MIN = (21, 8)`
is the cut-off above which the native path is used. Every version-specific
behaviour is gated on that check, so the 20.11 path is unchanged.

### Native ternary/wildcard match (21.08+)

The wildcard backend that was missing in 20.11 is complete in current DPDK:

- `rte_swx_pipeline.c` registers `wildcard` against
  `rte_swx_table_wildcard_match_ops`, an ACL-based backend in
  `rte_swx_table_wm.c`. No `/* TBD */` stubs remain in `lib/pipeline` or
  `lib/table`.
- Mask parsing is fully implemented, including `field_hton` for header fields, so
  a `0xC0` mask on a 32-bit header field correctly wildcards the low bits.
- Priority is `RTE_ACL_MAX_PRIORITY - key_priority` — a lower number wins, which
  matches Planter's priority-0-first, first-match-wins convention.

Feature tables therefore emit native `<val>/<mask> priority <N>` entries instead
of the exact-match expansion.

### Fixes required on 21.08+

Three unrelated issues had to be fixed before the native path worked. All are
version-gated.

1. **`H()`/`N()` action-argument syntax was removed in 21.08+.**
   `table_entry_action_argument_read` uses `strtoull()` and rejects trailing
   characters, so `H(0)` is a hard parse error and the entry file is rejected.
   The adapter emits plain-integer action arguments on 21.08+ and keeps
   `H()`/`N()` for 20.11.
2. **The drop flag was never cleared, producing an empty emit.** p4c opens the
   apply block with `mov m.psa_ingress_output_metadata_drop 0x1`, and Planter's
   P4 never clears it, so `jmpneq LABEL_DROP` always jumps and skips the emit.
   The 20.11 workaround rewrote the drop label to `tx`, which transmitted
   zero-length packets (`incl_len=0`). The fix clears the drop flag so the real
   emit + tx path runs.
3. **`.io` token length.** Every whitespace-separated token in the `.io` file
   must be shorter than `RTE_SWX_NAME_SIZE` (64). Long run paths (~89
   characters) produced `Pipeline build failed (-22)` inside
   `pipeline_iospec_parse`. The fix is a short run directory plus a length guard
   on the generated `.io`.

---

## DPDK 20.11 limitations

The P4Pi SIGCOMM 2022 image ships DPDK 20.11.5. Six limitations in its SWX
runtime are not documented upstream; `patch_spec_file()` works around each:

| Limitation | Workaround |
|---|---|
| No wildcard/ternary table backend (`rte_swx_table_wildcard_match` was added in DPDK 21.08) | Expand ternary ranges into exact-match entries using Planter's matching formula `(x & V) == (V & M)` |
| Mask parsing unimplemented in the text entry format — the source contains `/* TBD Set entry->key_mask */` and the mask is never written | Exact-match expansion makes masks unnecessary |
| `lookahead` absent from the SWX instruction set | Remove the lookahead block; the etherType check already gates the path |
| `drop` absent from the SWX instruction set | Replace with `tx` to a sink port |
| Table size must satisfy `n/4 = 2^k` for hash bucket addressing | Compute the size from the actual entry count and round up |
| `n_pkts_max` uninitialised in the `dpdk-pipeline` sample app | Patch `cli.c` to set `n_pkts_max = 0` |

The first two no longer apply on 21.08+ and are gated off there; see
[DPDK version support](#dpdk-version-support). `--no-huge` is used throughout,
removing the hugepage reservation step that would otherwise need reapplying
after every reboot.

---

## Example applications

| Example | Model | Description |
|---|---|---|
| [`examples/flow_classification/`](examples/flow_classification/) | Decision Tree | Classifying flows by packet header features |
| [`examples/anomaly_detection/`](examples/anomaly_detection/) | Random Forest | Multi-class traffic classification |

Each includes a README written for classroom use.

---

## P4Pi image build

[`image-build/`](image-build/) rebuilds the P4Pi image with Planter, `p4c`, BMv2
and `dpdk-pipeline` preinstalled, so no setup is needed after flashing.

This was necessary because the original P4Pi build depends on OpenSUSE OBS
repositories that no longer exist:

```
$ curl https://api.opensuse.org/public/build/home:p4pi
<status code="unknown_project">Project not found: home:p4pi</status>
```

The overlay replaces those package sources with builds from pinned upstream tags
— p4c v1.2.5.16 and BMv2 1.15.5, with the BMv2 and DPDK backends enabled. See
[`image-build/README.md`](image-build/README.md) for the build procedure and the
cross-build issues encountered.

---

## Known issues

- **Random Forest training is not seeded.** `random_state` is left unset, so
  accuracy varies from run to run. Always compare DPDK against software for the
  *same* trained model. The model code is deliberately left unchanged.
- **XGBoost.** Compiles and loads its feature tables, but the pipeline crashes at
  runtime on DPDK 20.11. Runtime behaviour on 24.11 has not yet been re-tested.
- **Image boot verification.** The image builds cleanly and passes an in-image
  smoke test, but has not yet been flashed and booted on physical hardware.
- **TAP interfaces.** DPDK creates TAP devices successfully, but the `link` and
  `ethdev` port types in the DPDK 20.11 `dpdk-pipeline` sample app do not accept
  them without additional ethdev initialisation. Testing currently uses pcap
  source/sink ports.

### Corrected: the Random Forest "accuracy gap"

Earlier revisions of this document reported a 91.11% vs 93.33% Random Forest gap
and attributed it to a lossy ternary-to-exact-match expansion. That was wrong.
`covered_values()` is correct — verified against `Range_to_TCAM_Top_Down.py`,
whose value/mask parameters are swapped relative to Planter's `[mask, value,
code]` layout, but whose computation and priority ordering agree. The real causes
were the three 21.08+ issues above: the `H()`/`N()` action-argument syntax, the
uncleared drop flag, and the `.io` token length limit.

---

## Files added or modified

```
src/targets/dpdk/software/     p4c-dpdk target adapter (new)
src/architectures/psa/         PSA generator (fixed)
src/targets/bmv2/software/     Architecture-aware compiler selection (fixed)
examples/                      Example applications (new)
image-build/                   P4Pi image build overlay (new)
scripts/                       Ternary-to-exact-match conversion utility
setup.sh                       One-command environment setup (new)
```

---

## Upstream

- Python 3.12 compatibility fix:
  [Planter PR #10](https://github.com/In-Network-Machine-Learning/Planter/pull/10)
- PSA generator fixes and the DPDK target: PRs to
  [Planter](https://github.com/In-Network-Machine-Learning/Planter) to follow
- Image build fixes: PR to [P4Pi](https://github.com/p4lang/p4pi) to follow
