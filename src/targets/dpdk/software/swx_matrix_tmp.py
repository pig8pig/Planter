#!/usr/bin/env python3
import os
import shutil
import subprocess
import tempfile

SPEC = "/home/pi/Planter/src/targets/dpdk/software/model_test/test_environment/spec/DT_performance_Iris.spec"
INPCAP = "/home/pi/Planter/src/targets/dpdk/software/model_test/test_environment/test_input.pcap"
BIN = "/home/pi/dpdk_pipeline_build/build/pipeline"


def run_case(kind: str, line: str) -> None:
    tmpd = tempfile.mkdtemp(prefix="swx_case_")
    try:
        addf = os.path.join(tmpd, "add.txt")
        delf = os.path.join(tmpd, "del.txt")
        deff = os.path.join(tmpd, "def.txt")
        runf = os.path.join(tmpd, "run.cli")
        outp = os.path.join(tmpd, "out.pcap")

        if kind == "default":
            open(addf, "w").close()
            open(delf, "w").close()
            with open(deff, "w") as f:
                f.write(line + "\n")
        else:
            with open(addf, "w") as f:
                f.write(line + "\n")
            open(delf, "w").close()
            open(deff, "w").close()

        with open(runf, "w") as f:
            f.write("mempool MEMPOOL0 buffer 2304 pool 32K cache 256 cpu 0\n")
            f.write("pipeline PIPELINE0 create 0\n")
            f.write(f"pipeline PIPELINE0 port in 0 source MEMPOOL0 {INPCAP}\n")
            f.write(f"pipeline PIPELINE0 port out 0 sink {outp}\n")
            f.write(f"pipeline PIPELINE0 build {SPEC}\n")
            f.write(f"pipeline PIPELINE0 table lookup_feature0 update {addf} {delf} {deff}\n")
            f.write("quit\n")

        subprocess.run(["sudo", "killall", "pipeline"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["sudo", "rm", "-rf", "/var/run/dpdk/rte/"], check=False)

        proc = subprocess.run(
            ["sudo", BIN, "-c", "0x3", "--", "-s", runf],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = proc.stdout + proc.stderr
        if "Error in file" in out:
            print(f"FAIL | {kind} | {line}")
            for ln in out.splitlines():
                if "Error in file" in ln:
                    print(ln)
                    break
        else:
            print(f"PASS | {kind} | {line}")
    except subprocess.TimeoutExpired as e:
        out_s = e.stdout or b""
        err_s = e.stderr or b""
        if isinstance(out_s, bytes):
            out_s = out_s.decode("utf-8", errors="replace")
        if isinstance(err_s, bytes):
            err_s = err_s.decode("utf-8", errors="replace")
        out = out_s + err_s
        if "Error in file" in out:
            print(f"FAIL | {kind} | {line}")
            for ln in out.splitlines():
                if "Error in file" in ln:
                    print(ln)
                    break
        else:
            print(f"PASS_TIMEOUT | {kind} | {line}")
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def main() -> None:
    defaults = [
        "action NoAction",
        "action NoAction args none",
        "action write_default_class",
        "action write_default_class args none",
    ]
    adds = [
        "match 64 action extract_feature0 tree 00000000",
        "match 64 action extract_feature0 tree N(0)",
        "match 64 action extract_feature0 tree N(0x00000000)",
        "match 0x00000040 action extract_feature0 tree N(0)",
        "match 0x00000040 action extract_feature0 tree 0x00000000",
        "match 00000040 action extract_feature0 tree N(0)",
        "match 64 action extract_feature0 tree H(0)",
    ]

    print("=== DEFAULT CANDIDATES ===")
    for line in defaults:
        run_case("default", line)

    print("=== ADD CANDIDATES ===")
    for line in adds:
        run_case("add", line)


if __name__ == "__main__":
    main()
