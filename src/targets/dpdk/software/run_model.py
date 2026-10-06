# THIS FILE IS PART OF Planter PROJECT
# Planter.py - The core part of the Planter library
#
# THIS PROGRAM IS FREE SOFTWARE TOOL, WHICH MAPS MACHINE LEARNING ALGORITHMS TO DATA PLANE, IS LICENSED UNDER Apache-2.0
# YOU SHOULD HAVE RECEIVED A COPY OF THE LICENSE, IF NOT, PLEASE CONTACT THE FOLLOWING E-MAIL ADDRESSES
#
# Copyright (c) 2020-2021 Changgang Zheng
# Copyright (c) Computing Infrastructure Lab, Department of Engineering Science, University of Oxford
# E-mail: changgang.zheng@eng.ox.ac.uk or changgangzheng@qq.com
#
# Functions: This file is a P4 compiler and runner of the P4 target.
#            Please refer to ./Docs/Planter_User_Document.pdf or further information.
#
# Author: Yuzhong (WeiWei) Luo
# Date: 2026-06-24

import os
import re
import sys
import stat
import shutil
import subprocess as sub
import json
import time
import signal
import platform
import threading
from multiprocessing import *
import getpass
from src.functions.json_encoder import *
from src.functions.add_license import *
from src.functions.extract_log_file_info import *


def file_names(Planter_config):
    work_root = Planter_config['directory config']['work']
    model_test_root = Planter_config['directory config']['work'] + '/src/targets/dpdk/software/model_test/test_environment'
    file_name = Planter_config['model config']['model'] + '_' + Planter_config['target config']['use case'] + '_' + \
                Planter_config['data config']['dataset']
    test_file_name = 'test_switch_model_' + Planter_config['target config']['device'] + '_' + Planter_config['target config']['type']
    return work_root, model_test_root, file_name, test_file_name

def compile_p4_dpdk(p4_file, output_dir):
    """Compile P4 file with p4c-dpdk. Returns (success, spec_path, error)."""
    os.makedirs(output_dir, exist_ok=True)
    cmd = ['p4c', '--target', 'dpdk', '--arch', 'psa', p4_file, '-o', output_dir]
    # The pipeline app runs unprivileged (--no-huge), so no sudo is needed to
    # clear a previous run.
    sub.run(['pkill', '-x', 'pipeline'], capture_output=True)
    result = sub.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, None, result.stderr
    p4_basename = os.path.splitext(os.path.basename(p4_file))[0]
    spec_path = os.path.join(output_dir, p4_basename + '.spec')
    if not os.path.exists(spec_path):
        return False, None, f"Spec file not found at {spec_path}"
    return True, spec_path, None


def next_valid_size(n_entries):
    """Smallest n where n/4 is a power of 2, n >= n_entries, and n >= 8."""
    buckets = 1
    while buckets * 4 < n_entries:
        buckets *= 2
    return max(buckets * 4, 8)


# ---------------------------------------------------------------------------
# DPDK version handling
#
# DPDK 20.11 had no wildcard (ternary) table backend in the SWX pipeline:
# mask parsing in the spec was a literal /* TBD */ stub.  Ternary match was
# added in 21.08 (lib/table/rte_swx_table_wm.c, ACL-backed), so from 21.08
# onwards Planter's ternary feature tables can be emitted natively instead of
# being expanded into an exact-match entry per covered key value.
#
# Two further incompatibilities matter for entry/CLI generation:
#   * 20.11 wrapped action arguments as H(x)/N(x) to select host/network byte
#     order.  21.08+ dropped that syntax: rte_swx_ctl.c parses the argument
#     with strtoull() and rejects any trailing characters, deriving byte order
#     from the action field itself.  H(0) is a hard parse error there.
#   * 20.11 built a pipeline straight from the .spec.  21.08+ splits this into
#     codegen -> libbuild -> "build lib <so> io <io> numa <n>", with the port
#     configuration moved out of the CLI into a separate .io spec file.
# ---------------------------------------------------------------------------

DPDK_NATIVE_TERNARY_MIN = (21, 8)


def detect_dpdk_version(config=None):
    """Return the target DPDK version as an (major, minor) tuple.

    Order of precedence: explicit config override, then pkg-config, then a
    conservative 20.11 fallback so unknown environments keep the old
    behaviour.
    """
    raw = None
    if config:
        raw = config.get('dpdk config', {}).get('version')
    if not raw:
        raw = os.environ.get('PLANTER_DPDK_VERSION')
    if not raw:
        try:
            res = sub.run(['pkg-config', '--modversion', 'libdpdk'],
                          capture_output=True, text=True)
            if res.returncode == 0:
                raw = res.stdout.strip()
        except Exception:
            raw = None
    if not raw:
        return (20, 11)
    parts = re.findall(r'\d+', str(raw))
    if len(parts) < 2:
        return (20, 11)
    return (int(parts[0]), int(parts[1]))


def uses_native_ternary(version):
    """True when this DPDK has a wildcard/ternary SWX table backend."""
    return tuple(version) >= DPDK_NATIVE_TERNARY_MIN


def fmt_action_arg(value, wrapper, native):
    """Format an action argument for the table entries file.

    ``wrapper`` is the DPDK 20.11 byte-order wrapper letter ('H' or 'N').
    On 21.08+ the wrapper was removed and a bare integer is required.
    """
    value = int(value)
    return str(value) if native else f'{wrapper}({value})'


def patch_spec_file(spec_path, entries_dir, native_ternary=False):
    """Apply the workarounds the compiled .spec needs to run under Planter.

    Fixes 1-2 cover features missing in DPDK 20.11 (wildcard tables, the
    lookahead instruction) and are skipped on 21.08+, which has both.  Fixes
    3-6 apply to every version; fix 3 additionally rewrites the unreachable
    LABEL_DROP body on 20.11, which has no drop instruction to parse.
    """
    with open(spec_path, 'r') as f:
        spec = f.read()

    if not native_ternary:
        # Fix 1: wildcard -> exact (DPDK 20.11 has no wildcard table backend)
        spec = spec.replace(' wildcard', ' exact')

        # Fix 2: remove lookahead block (instruction doesn't exist in DPDK 20.11)
        lookahead_pattern = re.compile(
            r'(SWITCHINGRESSPARSER_CHECK_PLANTER_VERSION\s*:\s*)lookahead.*?'
            r'jmp SWITCHINGRESSPARSER_ACCEPT\n',
            re.DOTALL
        )
        spec = lookahead_pattern.sub(
            r'\1jmp SWITCHINGRESSPARSER_PARSE_PLANTER\n',
            spec
        )


    # Fix 3: never take the drop branch.
    #
    # p4c-dpdk opens the PSA apply block with
    #     mov m.psa_ingress_output_metadata_drop 0x1
    # (PSA drops by default) and the Planter P4 has no action that clears the
    # flag, so the trailing
    #     jmpneq LABEL_DROP m.psa_ingress_output_metadata_drop 0x0
    # always jumps.  That skips the two emit instructions, so rewriting
    # LABEL_DROP's body to tx (the old DPDK 20.11 workaround) transmits a
    # zero-length packet rather than the classified one — the parser extracted
    # the headers and nothing put them back.  Clearing the flag instead lets
    # the normal "emit h.ethernet; emit h.Planter; tx" path run, which is what
    # this use case wants: every packet comes back carrying its result field.
    spec = spec.replace(
        'mov m.psa_ingress_output_metadata_drop 0x1',
        'mov m.psa_ingress_output_metadata_drop 0x0'
    )

    if not native_ternary:
        # LABEL_DROP is now unreachable, but DPDK 20.11 still has to parse it
        # and has no drop instruction.
        spec = spec.replace(
            'LABEL_DROP :\tdrop',
            'LABEL_DROP :\ttx m.psa_ingress_output_metadata_egress_port'
        )
        spec = spec.replace(
            'LABEL_DROP :    drop',
            'LABEL_DROP :    tx m.psa_ingress_output_metadata_egress_port'
        )

    # Fix 4: correct table sizes to match actual entry counts
    # (must satisfy n/4 = power of 2 for DPDK's hash bucket addressing)
    _cfg = json.load(open('src/configs/Planter_config.json'))
    _model = _cfg.get('model config', {}).get('model', 'DT')
    table_entry_files = {f'lookup_feature{n}': f'lookup_feature{n}_entries.txt' for n in range(4)}
    if _model in ('RF', 'XGB'):
        _n_trees = _cfg.get('model config', {}).get('number of trees', 5)
        for i in range(_n_trees):
            table_entry_files[f'lookup_leaf_id{i}'] = f'lookup_leaf_id{i}_entries.txt'
    table_entry_files['decision'] = 'decision_entries.txt'
    lines = spec.splitlines()
    for table_name, entry_file in table_entry_files.items():
        entry_path = os.path.join(entries_dir, entry_file)
        if not os.path.exists(entry_path):
            continue

        with open(entry_path) as ef:
            n_entries = sum(1 for line in ef if line.strip())

        # Keep one extra slot for default/action-state overhead to avoid commit failures
        # when table entry count equals nominal size.
        #
        # The n/4 = power-of-2 rule comes from the exact-match (hash) backend's
        # bucket addressing.  Wildcard tables are ACL-backed and only need the
        # declared size to be large enough, so rounding up is harmless but the
        # constraint does not apply to them.
        is_wildcard = native_ternary and table_name.startswith('lookup_feature')
        if is_wildcard:
            correct_size = max(n_entries + 1, 8)
        else:
            correct_size = next_valid_size(n_entries + 1)

        in_target_table = False
        brace_depth = 0
        for i, line in enumerate(lines):
            stripped = line.strip()

            if not in_target_table:
                if stripped.startswith(f'table {table_name} '):
                    in_target_table = True
                    brace_depth += line.count('{') - line.count('}')
                continue

            # While inside target table, replace its size line.
            if stripped.startswith('size '):
                indent = line[:len(line) - len(line.lstrip())]
                lines[i] = f"{indent}size {hex(correct_size)}"

            brace_depth += line.count('{') - line.count('}')
            if brace_depth <= 0:
                in_target_table = False
                brace_depth = 0

    spec = '\n'.join(lines) + '\n'

    # Fix 5: Correct RF leaf table key extraction in spec.
    # p4c-dpdk generates wrong shr/and amounts for bit-slice table keys.
    # For tree 0 the spec copies the full packed code_fN with no masking;
    # for trees 1-4 the shr offsets are computed incorrectly.
    # We patch to extract exactly the right bits per (tree, feature) pair,
    # matching the per-tree codes stored in Exact_Table['tree N'].
    # NOTE: the apply-block in the spec uses a single tab (\t) for indentation.
    if _model in ('RF', 'XGB') and 'width of code' in _cfg.get('p4 config', {}):
        _woc       = _cfg['p4 config']['width of code']   # [tree][feature]
        _n_trees_w = len(_woc)
        _n_feats_w = len(_woc[0]) if _n_trees_w > 0 else 4

        # --- tree 0: plain mov — add masking via Ingress_tmp (same pattern as trees 1-N) ---
        for _f in range(_n_feats_w):
            _mask    = (1 << int(_woc[0][_f])) - 1
            _key_reg = 'Ingress_key' if _f == 0 else f'Ingress_key_{_f - 1}'
            _old = f'\tmov m.{_key_reg} m.local_metadata_code_f{_f}\n'
            _new = (f'\tmov m.Ingress_tmp m.local_metadata_code_f{_f}\n'
                    f'\tand m.Ingress_tmp 0x{_mask:x}\n'
                    f'\tmov m.{_key_reg} m.Ingress_tmp\n')
            spec = spec.replace(_old, _new, 1)

        # --- trees 1-N: fix shr offset and AND mask in each 4-line key-prep block ---
        # Pattern: mov tmp code_fF; shr tmp WRONG; and tmp WRONG_MASK; mov key_Y tmp
        # key_Y register index (1-based from key_0) encodes (tree, feature):
        #   reg_idx = key_number + 1  →  tree = reg_idx // n_features,  feat = reg_idx % n_features
        def _fix_leaf_shr(m):
            tmp_reg  = m.group(1)
            feat_num = int(m.group(2))
            key_full = m.group(3)
            key_n_s  = m.group(4)          # numeric suffix of key reg, or None
            if key_n_s is None:
                return m.group(0)          # unnumbered key = tree 0, already handled
            key_n    = int(key_n_s)
            reg_idx  = key_n + 1           # Ingress_key_0 → reg 1
            tree_n   = reg_idx // _n_feats_w
            feat_f   = reg_idx % _n_feats_w
            if tree_n < 1:
                return m.group(0)
            c_shr  = int(sum(_woc[T][feat_f] for T in range(tree_n)))
            c_mask = (1 << int(_woc[tree_n][feat_f])) - 1
            return (f'\tmov m.{tmp_reg} m.local_metadata_code_f{feat_num}\n'
                    f'\tshr m.{tmp_reg} 0x{c_shr:x}\n'
                    f'\tand m.{tmp_reg} 0x{c_mask:x}\n'
                    f'\tmov m.{key_full} m.{tmp_reg}')

        _leaf_key_pat = re.compile(
            r'\tmov m\.(Ingress_tmp_?\d*) m\.local_metadata_code_f(\d+)\n'
            r'\tshr m\.\1 0x[0-9a-f]+\n'
            r'\tand m\.\1 0x[0-9a-f]+\n'
            r'\tmov m\.(Ingress_key(?:_(\d+))?) m\.\1'
        )
        spec = _leaf_key_pat.sub(_fix_leaf_shr, spec)

    # Fix 6: Align 32-bit scratch registers to 4-byte boundaries.
    # A trailing bit<8> field (e.g. local_metadata_flag) can leave the
    # Ingress_tmp / Ingress_key registers at offset%4 == 2, causing a
    # SIGBUS on ARM when the DPDK executor performs aligned 32-bit loads.
    # Insert bit<8> padding fields (never bit<16> — unsupported by DPDK 20.11)
    # before the first Ingress_tmp to reach the next 4-byte boundary.
    _first_tmp = spec.find('\n\tbit<32> Ingress_tmp\n')
    if _first_tmp != -1:
        # Compute byte offset of Ingress_tmp in the metadata struct
        _ms = spec.find('struct metadata_t {')
        _fragment = spec[_ms:_first_tmp]
        _offset = 0
        for _fl in _fragment.split('\n'):
            _fm = re.match(r'\s+bit<(\d+)>', _fl)
            if _fm:
                _offset += (int(_fm.group(1)) + 7) // 8
        _pad = (-_offset) % 4            # bytes needed to reach next 4-byte boundary
        if _pad > 0:
            # Use bit<8> fields only — DPDK 20.11 doesn't support bit<16>
            _pad_decl = ''.join(f'\n\tbit<8> _planter_align_pad_{i}' for i in range(_pad))
            spec = spec[:_first_tmp] + _pad_decl + spec[_first_tmp:]

    with open(spec_path, 'w') as f:
        f.write(spec)
    print(f"Patched spec written to {spec_path}")

def generate_entry_files_rf(work_root, output_dir, native_ternary=False):
    """Write RF table JSON files out as per-table entry files for dpdk-pipeline."""
    os.makedirs(output_dir, exist_ok=True)

    config_path = os.path.join(work_root, 'src', 'configs', 'Planter_config.json')
    with open(config_path) as f:
        planter_config = json.load(f)
    # width_of_code[tree][feature] — bit width allocated for each tree's code
    # in each feature's packed metadata field (stored by the model generator)
    width_of_code = planter_config['p4 config']['width of code']
    n_trees = len(width_of_code)

    ternary_json = os.path.join(work_root, 'Tables', 'Ternary_Table.json')
    exact_json   = os.path.join(work_root, 'Tables', 'Exact_Table.json')
    with open(ternary_json) as f:
        ternary_table = json.load(f)
    with open(exact_json) as f:
        exact_table = json.load(f)

    # Planter's Ternary_Table stores each entry as [mask, value, code] and the
    # dict key is the priority (0 = highest, first match wins) — see
    # src/functions/Range_to_TCAM_Top_Down.py, which tests a key with
    # `key & mask == mask & value`.  covered_values() below therefore takes the
    # mask first; the parameter names are inherited from the original code.
    def covered_values(mask, value):
        target = value & mask
        return [x for x in range(256) if (x & mask) == target]

    def pack_codes(codes_list, feature_n):
        """Pack per-tree codes into the single combined metadata value for feature_n.

        Each tree's code occupies a contiguous bit slice of the combined field,
        starting at the cumulative shift determined by the widths of all
        preceding trees for this feature (matching the P4 key-slice layout).
        """
        packed = 0
        shift = 0
        for t in range(n_trees):
            packed |= (int(codes_list[t]) << shift)
            shift += int(width_of_code[t][feature_n])
        return packed

    # Feature lookup tables (ternary) — write the packed combined code
    for n in range(4):
        lines = []
        if native_ternary:
            # One native wildcard entry per TCAM row.  The JSON key order is
            # the priority order produced by Table_to_TCAM, and DPDK's wildcard
            # backend treats a lower key_priority as higher precedence
            # (rte_swx_table_wm.c: RTE_ACL_MAX_PRIORITY - key_priority), which
            # matches Planter's first-match-wins convention directly.
            for priority, entry in enumerate(ternary_table[f'feature {n}'].values()):
                mask, value, code_list = entry[0], entry[1], entry[2]
                code = pack_codes(code_list, n)
                arg = fmt_action_arg(code, 'H', native_ternary)
                lines.append(f"match {value}/{mask} priority {priority} "
                             f"action extract_feature{n} tree {arg}")
        else:
            # DPDK 20.11 has no wildcard backend: expand each TCAM row into an
            # exact entry per covered key value, keeping first-match-wins by
            # never overwriting a value already claimed by a higher priority.
            seen = {}
            for entry in ternary_table[f'feature {n}'].values():
                mask, value, code_list = entry[0], entry[1], entry[2]
                code = pack_codes(code_list, n)
                for x in covered_values(mask, value):
                    if x not in seen:
                        seen[x] = code
                        lines.append(f"match {x} action extract_feature{n} tree H({code})")
        out_path = os.path.join(output_dir, f'lookup_feature{n}_entries.txt')
        with open(out_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print(f"  Wrote {len(lines)} entries -> {out_path}")

    # Leaf lookup tables (exact) — one per tree
    for n in range(n_trees):
        lines = []
        for entry in exact_table[f'tree {n}'].values():
            f0   = entry['f0 code']
            f1   = entry['f1 code']
            f2   = entry['f2 code']
            f3   = entry['f3 code']
            leaf = entry['leaf']
            prob_arg = fmt_action_arg(0, 'H', native_ternary)
            vote_arg = fmt_action_arg(leaf, 'H', native_ternary)
            lines.append(f"match {f0} {f1} {f2} {f3} "
                         f"action read_prob{n} prob {prob_arg} vote {vote_arg}")
        out_path = os.path.join(output_dir, f'lookup_leaf_id{n}_entries.txt')
        with open(out_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print(f"  Wrote {len(lines)} entries -> {out_path}")

    # Decision table — dynamic so it works for any number of trees
    lines = []
    for entry in exact_table['decision'].values():
        votes = ' '.join(str(entry[f't{i} vote']) for i in range(n_trees))
        cls   = entry['class']
        lines.append(f"match {votes} action read_lable "
                     f"label {fmt_action_arg(cls, 'N', native_ternary)}")
    out_path = os.path.join(output_dir, 'decision_entries.txt')
    with open(out_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"  Wrote {len(lines)} entries -> {out_path}")


def generate_entry_files(work_root, output_dir, native_ternary=False):
    """Write table JSON files out as per-table entry files for dpdk-pipeline."""
    config_path = os.path.join(work_root, 'src', 'configs', 'Planter_config.json')
    with open(config_path) as f:
        planter_config = json.load(f)
    model = planter_config.get('model config', {}).get('model', 'DT')

    if model in ('RF', 'XGB'):
        generate_entry_files_rf(work_root, output_dir, native_ternary=native_ternary)
        return

    # DT path
    os.makedirs(output_dir, exist_ok=True)
    ternary_json = os.path.join(work_root, 'Tables', 'Ternary_Table.json')
    with open(ternary_json) as f:
        table = json.load(f)

    # See generate_entry_files_rf(): entries are [mask, value, code], keyed by
    # priority, matched as `key & mask == mask & value`.
    def covered_values(mask, value):
        target = value & mask
        return [x for x in range(256) if (x & mask) == target]

    # Feature lookup tables
    for n in range(4):
        lines = []
        if native_ternary:
            for priority, entry in enumerate(table[f'feature {n}'].values()):
                mask, value, code = entry[0], entry[1], int(entry[2])
                arg = fmt_action_arg(code, 'H', native_ternary)
                lines.append(f"match {value}/{mask} priority {priority} "
                             f"action extract_feature{n} tree {arg}")
        else:
            seen = {}
            for entry in table[f'feature {n}'].values():
                mask, value, code = entry[0], entry[1], int(entry[2])
                for x in covered_values(mask, value):
                    if x not in seen:
                        seen[x] = code
                        lines.append(f"match {x} action extract_feature{n} tree H({code})")
        out_path = os.path.join(output_dir, f'lookup_feature{n}_entries.txt')
        with open(out_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print(f"  Wrote {len(lines)} entries -> {out_path}")

    # Decision table
    lines = []
    for entry in table['code to vote'].values():
        f0, f1, f2, f3 = entry['f0 code'], entry['f1 code'], entry['f2 code'], entry['f3 code']
        leaf = entry['leaf']
        lines.append(f"match {f0} {f1} {f2} {f3} action read_lable "
                     f"label {fmt_action_arg(leaf, 'N', native_ternary)}")
    out_path = os.path.join(output_dir, 'decision_entries.txt')
    with open(out_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"  Wrote {len(lines)} entries -> {out_path}")


def pipeline_table_names(model='DT', n_trees=5):
    """Tables that receive entries, in the order they must be populated."""
    tables = [f'lookup_feature{n}' for n in range(4)]
    if model in ('RF', 'XGB'):
        tables += [f'lookup_leaf_id{i}' for i in range(n_trees)]
    tables.append('decision')
    return tables


def generate_io_file(io_path, input_pcap, output_pcap):
    """Write the .io port-spec file used by DPDK 21.08+.

    Every token in this file must be shorter than RTE_SWX_NAME_SIZE (64); the
    parser rejects the whole file with "Token too big." otherwise.  This is why
    the pcap files live in a short run directory rather than under the Planter
    test_environment tree.
    """
    for path in (input_pcap, output_pcap):
        if len(path) >= 64:
            raise ValueError(
                f"Path too long for the DPDK .io parser (limit 63 chars): {path}")
    with open(io_path, 'w') as f:
        f.write("mirroring slots 4 sessions 64\n")
        f.write(f"port in 0 source mempool MEMPOOL0 file {input_pcap} loop 1 packets 0\n")
        f.write(f"port out 0 sink file {output_pcap}\n")
    print(f"IO spec written to {io_path}")


def generate_cli_script(cli_path, spec_path, entries_dir,
                        input_pcap, output_pcap, model='DT', n_trees=5,
                        native_ternary=False, run_dir=None):
    """Write the dpdk-pipeline CLI script for the target DPDK version."""
    tables = pipeline_table_names(model, n_trees)

    if not native_ternary:
        # DPDK 20.11: the pipeline is built straight from the .spec and the
        # ports are configured through the CLI.
        with open(cli_path, 'w') as f:
            f.write("mempool MEMPOOL0 buffer 2304 pool 32K cache 256 cpu 0\n")
            f.write("pipeline PIPELINE0 create 0\n")
            f.write(f"pipeline PIPELINE0 port in 0 source MEMPOOL0 {input_pcap}\n")
            f.write(f"pipeline PIPELINE0 port out 0 sink {output_pcap}\n")
            f.write(f"pipeline PIPELINE0 build {spec_path}\n")
            for table in tables:
                entry_file = os.path.join(entries_dir, f'{table}_entries.txt')
                f.write(f"pipeline PIPELINE0 table {table} update "
                        f"{entry_file} none none\n")
            f.write("thread 1 pipeline PIPELINE0 enable\n")
        print(f"CLI script written to {cli_path}")
        return

    # DPDK 21.08+: codegen -> libbuild -> build from the shared object, with
    # the ports supplied by a separate .io spec.
    run_dir = run_dir or os.path.dirname(cli_path)
    os.makedirs(run_dir, exist_ok=True)
    io_path  = os.path.join(run_dir, 'p.io')
    code_c   = os.path.join(run_dir, 'p.c')
    code_so  = os.path.join(run_dir, 'p.so')
    generate_io_file(io_path, input_pcap, output_pcap)

    with open(cli_path, 'w') as f:
        f.write("mempool MEMPOOL0 meta 0 pkt 2176 pool 32K cache 256 numa 0\n")
        f.write(f"pipeline codegen {spec_path} {code_c}\n")
        f.write(f"pipeline libbuild {code_c} {code_so}\n")
        f.write(f"pipeline PIPELINE0 build lib {code_so} io {io_path} numa 0\n")
        for table in tables:
            entry_file = os.path.join(entries_dir, f'{table}_entries.txt')
            f.write(f"pipeline PIPELINE0 table {table} add {entry_file}\n")
        f.write("pipeline PIPELINE0 commit\n")
        f.write("pipeline PIPELINE0 enable thread 1\n")
    print(f"CLI script written to {cli_path}")

def find_rte_install_dir():
    """Locate a DPDK source tree, needed by "pipeline libbuild".

    The libbuild command shells out to gcc with -I paths under this directory
    and needs the in-tree headers (notably rte_swx_pipeline_internal.h), which
    are not part of the installed dev package.  When RTE_INSTALL_DIR is unset
    the DPDK CLI falls back to its own cwd, which is almost never right.
    """
    env = os.environ.get('RTE_INSTALL_DIR')
    if env and os.path.exists(os.path.join(env, 'lib', 'pipeline',
                                           'rte_swx_pipeline_internal.h')):
        return env
    for base in (os.path.expanduser('~'), '/usr/src', '/opt'):
        try:
            names = sorted(os.listdir(base), reverse=True)
        except OSError:
            continue
        for name in names:
            if not name.startswith('dpdk'):
                continue
            cand = os.path.join(base, name)
            if os.path.exists(os.path.join(cand, 'lib', 'pipeline',
                                           'rte_swx_pipeline_internal.h')):
                return cand
    return None


def _output_pcap_from_cli(cli_path):
    """Find the sink pcap for either CLI dialect (20.11 inline, 21.08+ .io)."""
    try:
        with open(cli_path) as cli_file:
            cli_lines = cli_file.readlines()
    except OSError:
        return None

    for line in cli_lines:
        if line.startswith('pipeline PIPELINE0 port out 0 sink '):
            return line.strip().split()[-1]

    for line in cli_lines:
        if ' io ' in line and ' build lib ' in line:
            tokens = line.split()
            io_path = tokens[tokens.index('io') + 1]
            try:
                with open(io_path) as io_file:
                    for io_line in io_file:
                        if io_line.startswith('port out 0 sink file '):
                            return io_line.strip().split()[-1]
            except (OSError, ValueError, IndexError):
                return None
    return None


def run_dpdk_pipeline(cli_path, pipeline_binary, log_path, output_pcap=None,
                      timeout=30, file_prefix='planter'):
    """
    Launch dpdk-pipeline, wait for it to process packets, capture log.
    Returns (success, log_output)
    """
    cmd = [pipeline_binary, '--no-huge', '-m', '256', '-l', '0-1',
           '--file-prefix', file_prefix, '--', '-s', cli_path]

    env = dict(os.environ)
    install_dir = find_rte_install_dir()
    if install_dir:
        env['RTE_INSTALL_DIR'] = install_dir

    def has_cli_table_errors(log_text):
        markers = (
            'Error in file "',            # 20.11 entry file error
            'Invalid entry in file',      # 21.08+ entry file error
            'Pipeline build failed',
            'Library build failed',
            'Token too big',
            'Invalid value for argument',
            'Cannot open file',
            'Command "pipeline" failed',
        )
        return any(m in log_text for m in markers)

    output_pcap = _output_pcap_from_cli(cli_path) or output_pcap

    if output_pcap and os.path.exists(output_pcap):
        try:
            os.remove(output_pcap)
        except OSError:
            pass

    # A leftover runtime directory from a killed run makes EAL refuse to start.
    for rt_base in (f'/run/user/{os.getuid()}/dpdk', '/var/run/dpdk'):
        stale = os.path.join(rt_base, file_prefix)
        if os.path.isdir(stale):
            shutil.rmtree(stale, ignore_errors=True)

    try:
        result = sub.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        log_output = result.stdout + result.stderr
        with open(log_path, 'w') as f:
            f.write(log_output)
        if result.returncode != 0 or has_cli_table_errors(log_output):
            if result.returncode != 0:
                log_output = f'[DPDK exit code: {result.returncode}]\n' + log_output
            return False, log_output
        return True, log_output
    except sub.TimeoutExpired as e:
        # Normal — pipeline runs forever until killed, timeout = success
        log_output = (e.stdout or b'').decode('utf-8', errors='replace') + \
             (e.stderr or b'').decode('utf-8', errors='replace')
        with open(log_path, 'w') as f:
            f.write(log_output)
        if has_cli_table_errors(log_output):
            return False, log_output
        return True, log_output
    except Exception as e:
        return False, str(e)

def add_make_run_model(fname, config):
    work_root, model_test_root, file_name, test_file_name = file_names(config)
    
    dpdk_version = detect_dpdk_version(config)
    native_ternary = uses_native_ternary(dpdk_version)
    print(f"Target DPDK {dpdk_version[0]}.{dpdk_version[1]:02d} — "
          f"{'native ternary tables' if native_ternary else 'ternary expanded to exact match'}")

    spec_dir    = os.path.join(model_test_root, 'spec')
    entries_dir = os.path.join(model_test_root, 'entries')
    manual_entries_dir = os.path.join(work_root, 'scripts', 'dpdk_entries')
    cli_path    = os.path.join(model_test_root, 'run.cli')
    log_path    = os.path.join(model_test_root, 'run.log')

    # DPDK 21.08+ parses the pcap paths out of the .io file, where every token
    # must be under RTE_SWX_NAME_SIZE (64) chars.  The Planter test_environment
    # path alone is ~89 chars, so the pcaps go in a short run directory.
    run_dir = config.get('dpdk config', {}).get('run dir') or \
        os.path.expanduser('~/.planter_dpdk')
    if native_ternary:
        os.makedirs(run_dir, exist_ok=True)
        input_pcap  = os.path.join(run_dir, 'in.pcap')
        output_pcap = os.path.join(run_dir, 'out.pcap')
    else:
        input_pcap  = os.path.join(model_test_root, 'test_input.pcap')
        output_pcap = os.path.join(model_test_root, 'test_output.pcap')

    pipeline_bin = config.get('dpdk config', {}).get('pipeline_bin') or ''
    if not os.path.exists(pipeline_bin):
        candidates = [os.path.expanduser('~/dpdk_pipeline_build/build/pipeline')]
        install_dir = find_rte_install_dir()
        if install_dir:
            candidates.insert(0, os.path.join(install_dir, 'examples', 'pipeline',
                                              'build', 'pipeline'))
        pipeline_bin = next((c for c in candidates if os.path.exists(c)),
                            candidates[-1])

    p4_file = os.path.join(work_root, 'P4', file_name + '.p4')

    _model_name  = config['model config']['model']
    _n_trees_req = config['model config'].get('number of trees', 5)
    required_entry_files = [f'lookup_feature{n}_entries.txt' for n in range(4)]
    if _model_name in ('RF', 'XGB'):
        required_entry_files += [f'lookup_leaf_id{i}_entries.txt' for i in range(_n_trees_req)]
    required_entry_files.append('decision_entries.txt')
    manual_entries_available = all(
        os.path.exists(os.path.join(manual_entries_dir, name))
        for name in required_entry_files
    )
    force_generate_entries = os.environ.get('PLANTER_FORCE_GENERATE_ENTRIES', '0') == '1'

    # Step 1 — compile
    print("Compiling P4 with p4c-dpdk...")
    success, spec_path, err = compile_p4_dpdk(p4_file, spec_dir)
    if not success:
        print(f"Compile failed:\n{err}")
        return

    # Step 2 — choose entry source FIRST (patch_spec_file needs them for size calculation)
    if manual_entries_available and not force_generate_entries:
        entries_source_dir = manual_entries_dir
        print(f"Using pre-validated entry files from {entries_source_dir}")
    else:
        entries_source_dir = entries_dir
        if force_generate_entries:
            print("Force-generating table entry files (PLANTER_FORCE_GENERATE_ENTRIES=1)...")
        else:
            print("Generating table entry files...")
        generate_entry_files(work_root, entries_source_dir,
                             native_ternary=native_ternary)

    # Step 3 — patch spec (now entry files exist for size counting)
    print("Patching spec file...")
    patch_spec_file(spec_path, entries_source_dir, native_ternary=native_ternary)

    # Step 4 — generate CLI script
    print("Generating CLI script...")
    model = config['model config']['model']
    n_trees = config['model config'].get('number of trees', 5)
    generate_cli_script(cli_path, spec_path, entries_source_dir, input_pcap, output_pcap,
                        model=model, n_trees=n_trees,
                        native_ternary=native_ternary, run_dir=run_dir)

    # Store paths in config for test_model.py to use
    config['dpdk config'] = {
        'cli_path':     cli_path,
        'input_pcap':   input_pcap,
        'output_pcap':  output_pcap,
        'log_path':     log_path,
        'pipeline_bin': pipeline_bin,
        'entries_dir':  entries_source_dir,
        'run dir':      run_dir,
        'version':      f'{dpdk_version[0]}.{dpdk_version[1]:02d}',
        'native ternary': native_ternary,
    }
    json.dump(config, open('src/configs/Planter_config.json', 'w'), indent=4)
    print("run_model setup complete — ready to run pipeline")


def term(sig_num, addition):
    print('Killing pid %s with group id %s' % (os.getpid(), os.getpgrp()))
    os.killpg(os.getpgid(os.getpid()), signal.SIGKILL)


def main(if_using_subprocess):
    if platform.system() != 'Linux':
        print('DPDK target requires Linux.')
        exit()

    config_file = 'src/configs/Planter_config.json'
    Planter_config = json.load(open(config_file, 'r'))
    # The pipeline app runs unprivileged (--no-huge), so the password is only
    # kept for compatibility with other targets and must not block a
    # non-interactive run.
    try:
        Planter_config['test config']['sudo password'] = getpass.getpass(
            "- Please input your password for 'sudo' command: ") or 'raspberry'
    except (EOFError, OSError):
        print("- No TTY available; skipping sudo password prompt "
              "(not needed for the unprivileged DPDK pipeline).")
        Planter_config.setdefault('test config', {}).setdefault(
            'sudo password', 'raspberry')
    json.dump(Planter_config, open(config_file, 'w'), indent=4, cls=NpEncoder)

    add_make_run_model(config_file, Planter_config)

    signal.signal(signal.SIGTERM, term)
    print('current pid is %s' % os.getpid())
    processes = []
    if_using_subprocess = False
    return processes, if_using_subprocess