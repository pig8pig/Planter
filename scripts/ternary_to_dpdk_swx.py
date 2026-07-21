#!/usr/bin/env python3
"""
ternary_to_dpdk_swx.py

Converts Planter's Ternary_Table.json into DPDK SWX pipeline table-entry
text files, one per feature lookup table plus one for the final decision.

Input:  ~/Planter/Tables/Ternary_Table.json
Output: ~/Planter/scripts/dpdk_entries/
          lookup_feature0_entries.txt  (one line per x in 0-255 matched)
          lookup_feature1_entries.txt
          lookup_feature2_entries.txt
          lookup_feature3_entries.txt
          decision_entries.txt

Feature entry format  [value, mask, code]:
  A raw byte value x is covered by an entry when (x & value) == (value & mask).
  Output line: match <x> action extract_feature<N> tree N(<code>)

Decision entry format  {f0 code, f1 code, f2 code, f3 code, leaf}:
  Output line: match <f0 code> <f1 code> <f2 code> <f3 code> action read_lable label N(<leaf>)
"""

import json
import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INPUT_JSON  = os.path.expanduser("~/Planter/Tables/Ternary_Table.json")
OUTPUT_DIR  = os.path.expanduser("~/Planter/scripts/dpdk_entries")

# ---------------------------------------------------------------------------
# Helper: find all byte values (0-255) covered by a ternary entry
# ---------------------------------------------------------------------------
def covered_values(value: int, mask: int) -> list:
    """Return every x in 0-255 that satisfies (x & value) == (value & mask)."""
    target = value & mask
    return [x for x in range(256) if (x & value) == target]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with open(INPUT_JSON) as f:
        table = json.load(f)

    # -- Feature lookup tables (feature 0 .. feature 3) --------------------
    for n in range(4):
        feature_key = f"feature {n}"
        entries = table[feature_key]          # dict keyed by string index
        output_path = os.path.join(OUTPUT_DIR, f"lookup_feature{n}_entries.txt")

        lines = []
        seen = {}   # x -> code from the first (highest-priority) entry that covers it
        for entry in entries.values():
            value, mask, code = entry[0], entry[1], entry[2]
            for x in covered_values(value, mask):
                if x in seen:
                    if seen[x] != code:
                        print(f"  INFO feature {n}: x={x} already claimed by code {seen[x]}; "
                              f"skipping lower-priority code {code}")
                    continue   # first match wins — respect entry priority order
                seen[x] = code
                line = (
                    f"match {x} "
                    f"action extract_feature{n} "
                    f"tree H({int(code)})"
                )
                lines.append(line)

        with open(output_path, "w") as out:
            out.write("\n".join(lines) + "\n")

        print(f"Wrote {len(lines)} lines -> {output_path}")

    # -- Decision table (code to vote) -------------------------------------
    decision_entries = table["code to vote"]
    output_path = os.path.join(OUTPUT_DIR, "decision_entries.txt")

    lines = []
    for entry in decision_entries.values():
        f0 = entry["f0 code"]
        f1 = entry["f1 code"]
        f2 = entry["f2 code"]
        f3 = entry["f3 code"]
        leaf = entry["leaf"]
        line = (
            f"match {f0} {f1} {f2} {f3} "
            f"action read_lable "
            f"label N({int(leaf)})"
        )
        lines.append(line)

    with open(output_path, "w") as out:
        out.write("\n".join(lines) + "\n")

    print(f"Wrote {len(lines)} lines -> {output_path}")


if __name__ == "__main__":
    main()
