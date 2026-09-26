#!/usr/bin/env python3
"""Compact printer for a TLC `-dumpTrace json` counterexample of SyncEngine.

usage: trace.py out/<INV>.json
Prints, per step, the action with its arguments and the model state that
changed: local files (tree/path), live manifest entries, remote configs.
"""

import json
import sys


def steps(d):
    """[(label, state)]: initial state, then (action(args), successor)."""
    acts = d["counterexample"]["action"]
    out = [("Init", acts[0][0][1])]
    for _pre, act, post in acts:
        args = ",".join(f"{k}={v}" for k, v in act.get("context", {}).items())
        out.append((f"{act['name']}({args})", post[1]))
    return out


def fmt_file(f):
    flags = "".join(
        k for k, on in (("~cosm", f["cosm"]), ("~drift", f["drift"]), ("*edited", f["ed"])) if on
    )
    return f"{f['comp']}:{f['cid']} v{f['v']}{flags} (lineage {f['sid']})"


def snap(s):
    files = {f"{t}/{p}": fmt_file(f) for t, ps in s["fl"].items() for p, f in ps.items() if f["ex"]}
    man = {}
    for i, e in s["mn"].items():
        if e["ex"]:
            ph = (
                "none"
                if e["ph"][3] == 99
                else f"{e['ph'][1]}/v{e['ph'][3]}"
                + ("~c" if e["ph"][4] else "")
                + ("~d" if e["ph"][5] else "")
            )
            base = "none" if e["base"][0] == 99 else f"v{e['base'][0]}"
            man[i] = f"{e['comp']} br={e['br']} path={e['path']} pull_hash={ph} base={base}"
    rem = {
        f"{b}/{i}": f"{r['comp']} v{r['v']} name={r['nm']} lineage={r['org']}"
        for b, rs in s["rm"].items()
        for i, r in rs.items()
        if r["ex"]
    }
    return {"file": files, "manifest": man, "remote": rem, "ignored": s["ig"]}


def main():
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
    prev = None
    for n, (lab, s) in enumerate(steps(d)):
        cur = snap(s)
        print(f"--- step {n}: {lab}   lastOp={s['lastOp']['kind']}@{s['lastOp']['b']}")
        for sect in ("file", "manifest", "remote"):
            for k, v in cur[sect].items():
                if prev is None or prev[sect].get(k) != v:
                    print(f"    {sect:8} {k:10} {v}")
            if prev is not None:
                for k in prev[sect]:
                    if k not in cur[sect]:
                        print(f"    {sect:8} {k:10} (gone)")
        if prev is not None and prev["ignored"] != cur["ignored"]:
            print(f"    ignored  -> {cur['ignored']}")
        prev = cur


main()
