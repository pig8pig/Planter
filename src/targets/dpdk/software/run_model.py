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
import sys
import stat
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
    sub.run(['sudo', 'killall', 'pipeline'], capture_output=True)
    sub.run(['sudo', 'rm', '-rf', '/var/run/dpdk/rte/'], capture_output=True)
    result = sub.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return False, None, result.stderr
    p4_basename = os.path.splitext(os.path.basename(p4_file))[0]
    spec_path = os.path.join(output_dir, p4_basename + '.spec')
    if not os.path.exists(spec_path):
        return False, None, f"Spec file not found at {spec_path}"
    return True, spec_path, None


def next_valid_size(n_entries):
    """Smallest n where n/4 is a power of 2 and n >= n_entries."""
    buckets = 1
    while buckets * 4 < n_entries:
        buckets *= 2
    return buckets * 4


def patch_spec_file(spec_path, entries_dir):
    """Apply DPDK 20.11 workarounds to the compiled .spec file."""
    with open(spec_path, 'r') as f:
        spec = f.read()

    # Fix 1: wildcard -> exact (DPDK 20.11 has no wildcard table backend)
    spec = spec.replace(' wildcard', ' exact')

    # Fix 2: remove lookahead block (instruction doesn't exist in DPDK 20.11)
    import re
    lookahead_pattern = re.compile(
        r'(SWITCHINGRESSPARSER_CHECK_PLANTER_VERSION\s*:\s*)lookahead.*?'
        r'jmp SWITCHINGRESSPARSER_ACCEPT\n',
        re.DOTALL
    )
    spec = lookahead_pattern.sub(
        r'\1jmp SWITCHINGRESSPARSER_PARSE_PLANTER\n',
        spec
    )

    # Fix 3: drop -> tx (drop instruction doesn't exist in DPDK 20.11)
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
    table_entry_files = {
        'lookup_feature0': 'lookup_feature0_entries.txt',
        'lookup_feature1': 'lookup_feature1_entries.txt',
        'lookup_feature2': 'lookup_feature2_entries.txt',
        'lookup_feature3': 'lookup_feature3_entries.txt',
        'decision':        'decision_entries.txt',
    }
    for table_name, entry_file in table_entry_files.items():
        entry_path = os.path.join(entries_dir, entry_file)
        if not os.path.exists(entry_path):
            continue
        with open(entry_path) as ef:
            n_entries = sum(1 for line in ef if line.strip())
        correct_size = next_valid_size(n_entries)
        # Replace size line inside this specific table block
        spec = re.sub(
            rf'(table {table_name} {{[^}}]*?size\s+)0x[0-9a-fA-F]+',
            lambda m: m.group(1) + hex(correct_size),
            spec,
            flags=re.DOTALL
        )

    with open(spec_path, 'w') as f:
        f.write(spec)
    print(f"Patched spec written to {spec_path}")

def generate_entry_files(work_root, output_dir):
    """Expand Ternary_Table.json into per-table exact-match entry files."""
    os.makedirs(output_dir, exist_ok=True)
    ternary_json = os.path.join(work_root, 'Tables', 'Ternary_Table.json')
    with open(ternary_json) as f:
        table = json.load(f)

    def covered_values(value, mask):
        target = value & mask
        return [x for x in range(256) if (x & mask) == target]

    # Feature lookup tables
    for n in range(4):
        lines = []
        seen = {}
        for entry in table[f'feature {n}'].values():
            value, mask, code = entry[0], entry[1], entry[2]
            for x in covered_values(value, mask):
                if x not in seen:
                    seen[x] = code
                    lines.append(f"match {x} action extract_feature{n} tree {code:08x}")
        out_path = os.path.join(output_dir, f'lookup_feature{n}_entries.txt')
        with open(out_path, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print(f"  Wrote {len(lines)} entries -> {out_path}")

    # Decision table
    lines = []
    for entry in table['code to vote'].values():
        f0, f1, f2, f3 = entry['f0 code'], entry['f1 code'], entry['f2 code'], entry['f3 code']
        leaf = entry['leaf']
        lines.append(f"match {f0} {f1} {f2} {f3} action read_lable label {int(leaf):08x}")
    out_path = os.path.join(output_dir, 'decision_entries.txt')
    with open(out_path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print(f"  Wrote {len(lines)} entries -> {out_path}")


def generate_cli_script(cli_path, spec_path, entries_dir,
                         input_pcap, output_pcap):
    """Write the dpdk-pipeline CLI script."""
    feature_tables = [
        'lookup_feature0', 'lookup_feature1',
        'lookup_feature2', 'lookup_feature3', 'decision'
    ]
    with open(cli_path, 'w') as f:
        f.write("mempool MEMPOOL0 buffer 2304 pool 32K cache 256 cpu 0\n")
        f.write("pipeline PIPELINE0 create 0\n")
        f.write(f"pipeline PIPELINE0 port in 0 source MEMPOOL0 {input_pcap}\n")
        f.write(f"pipeline PIPELINE0 port out 0 sink {output_pcap}\n")
        f.write(f"pipeline PIPELINE0 build {spec_path}\n")
        for table in feature_tables:
            entry_file = os.path.join(entries_dir, f'{table}_entries.txt')
            f.write(f"pipeline PIPELINE0 table {table} update "
                    f"{entry_file} none none\n")
        f.write("thread 1 pipeline PIPELINE0 enable\n")
    print(f"CLI script written to {cli_path}")

def run_dpdk_pipeline(cli_path, pipeline_binary, log_path, output_pcap=None, timeout=30):
    """
    Launch dpdk-pipeline, wait for it to process packets, capture log.
    Returns (success, log_output)
    """
    cmd = ['sudo', pipeline_binary, '-c', '0x3', '--', '-s', cli_path]
    output_pcap = None

    def has_cli_table_errors(log_text):
        return ('Error in file "' in log_text) or ('Invalid entry in file' in log_text)

    try:
        with open(cli_path, 'r') as cli_file:
            for line in cli_file:
                if line.startswith('pipeline PIPELINE0 port out 0 sink '):
                    output_pcap = line.strip().split()[-1]
                    break
    except OSError:
        pass

    if output_pcap and os.path.exists(output_pcap):
        try:
            os.remove(output_pcap)
        except OSError:
            pass

    try:
        result = sub.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        log_output = result.stdout + result.stderr
        with open(log_path, 'w') as f:
            f.write(log_output)
        if result.returncode != 0 or has_cli_table_errors(log_output):
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
    
    spec_dir    = os.path.join(model_test_root, 'spec')
    entries_dir = os.path.join(model_test_root, 'entries')
    manual_entries_dir = os.path.join(work_root, 'scripts', 'dpdk_entries')
    input_pcap  = os.path.join(model_test_root, 'test_input.pcap')
    output_pcap = os.path.join(model_test_root, 'test_output.pcap')
    cli_path    = os.path.join(model_test_root, 'run.cli')
    log_path    = os.path.join(model_test_root, 'run.log')
    pipeline_bin = os.path.expanduser('~/dpdk_pipeline_build/build/pipeline')

    p4_file = os.path.join(work_root, 'P4', file_name + '.p4')

    required_entry_files = [
        'lookup_feature0_entries.txt',
        'lookup_feature1_entries.txt',
        'lookup_feature2_entries.txt',
        'lookup_feature3_entries.txt',
        'decision_entries.txt',
    ]
    manual_entries_available = all(
        os.path.exists(os.path.join(manual_entries_dir, name))
        for name in required_entry_files
    )

    # Step 1 — compile
    print("Compiling P4 with p4c-dpdk...")
    success, spec_path, err = compile_p4_dpdk(p4_file, spec_dir)
    if not success:
        print(f"Compile failed:\n{err}")
        return

    # Step 2 — choose entry source FIRST (patch_spec_file needs them for size calculation)
    if manual_entries_available:
        entries_source_dir = manual_entries_dir
        print(f"Using pre-validated entry files from {entries_source_dir}")
    else:
        entries_source_dir = entries_dir
        print("Generating table entry files...")
        generate_entry_files(work_root, entries_source_dir)

    # Step 3 — patch spec (now entry files exist for size counting)
    print("Patching spec file...")
    patch_spec_file(spec_path, entries_source_dir)

    # Step 4 — generate CLI script
    print("Generating CLI script...")
    generate_cli_script(cli_path, spec_path, entries_source_dir, input_pcap, output_pcap)

    # Store paths in config for test_model.py to use
    config['dpdk config'] = {
        'cli_path':     cli_path,
        'output_pcap':  output_pcap,
        'log_path':     log_path,
        'pipeline_bin': pipeline_bin,
        'entries_dir':  entries_source_dir,
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
    Planter_config['test config']['sudo password'] = getpass.getpass(
        "- Please input your password for 'sudo' command: ") or 'raspberry'
    json.dump(Planter_config, open(config_file, 'w'), indent=4, cls=NpEncoder)

    add_make_run_model(config_file, Planter_config)

    signal.signal(signal.SIGTERM, term)
    print('current pid is %s' % os.getpid())
    processes = []
    if_using_subprocess = False
    return processes, if_using_subprocess