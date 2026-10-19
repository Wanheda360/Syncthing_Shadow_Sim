#!/usr/bin/env python3
"""
setup_sim.py — Create and manage Shadow/Syncthing simulation directories.

Usage:
    python3 setup_sim.py              # set up all hosts
    python3 setup_sim.py --reset      # wipe everything and start fresh
    python3 setup_sim.py --status     # print current state without changes
    python3 setup_sim.py --topology   # print peer map without making changes

Adding hosts: change NUM_HOSTS and re-run (existing certs are preserved).
"""

import argparse
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── Configuration — edit this section ────────────────────────────────────────

SYNCTHING_BIN    = "/home/anga/Documents/Project/syncthing/bin/syncthing"
SIM_DIR          = Path("sim").resolve()

# Number of hosts — names: a, b, c, ...  IPs: 11.0.0.1, 11.0.0.2, ...
NUM_HOSTS        = 6

# Number of peers each host connects to.
# Actual peer count may be slightly higher for some nodes after symmetry
# enforcement (if node j is chosen as node i's peer, i is added to j's list
# even if j already has PEERS_PER_HOST peers).
PEERS_PER_HOST   = 2

# Topology — currently supported: "random_fixed", "ring", "full_mesh"
#   random_fixed : deterministic random peers per node (seed = node index)
#   ring         : k nearest neighbours in a cycle
#   full_mesh    : every node connects to every other node
TOPOLOGY         = "ring"

FOLDER_ID        = "sync"
RESCAN_INTERVAL_S = 30

SHADOW_PARAMS = {
    "stop_time":    "3 min",
    "log_level":    "info",
    "parallelism":  1,
    "latency_ms":   10,
    "jitter_ms":    1,
    "pcap_enabled": True,
}

# ── Host generation ───────────────────────────────────────────────────────────

def build_hosts() -> list:
    if NUM_HOSTS > 26:
        raise ValueError("NUM_HOSTS cannot exceed 26")
    return [
        {
            "name":       chr(ord('a') + i),
            "ip":         f"11.0.0.{i + 1}",
            "start_time": f"{3 + i} sec",
        }
        for i in range(NUM_HOSTS)
    ]

# ── Topology / peer selection ─────────────────────────────────────────────────

def _initial_peers(idx: int, n: int, k: int) -> set:
    """
    Compute the initial (pre-symmetry) peer indices for host `idx`.
    """
    k = min(k, n - 1)

    if TOPOLOGY == "full_mesh":
        return set(range(n)) - {idx}

    elif TOPOLOGY == "ring":
        peers = set()
        half = k // 2
        for j in range(1, half + 1):
            peers.add((idx + j) % n)
            peers.add((idx - j) % n)
        if k % 2 == 1:                          # odd k: one extra forward
            peers.add((idx + half + 1) % n)
        return peers

    elif TOPOLOGY == "random_fixed":
        rng = random.Random(idx)                # seed = node index → stable
        candidates = [j for j in range(n) if j != idx]
        return set(rng.sample(candidates, k))

    else:
        raise ValueError(f"Unknown topology: '{TOPOLOGY}'")


def build_adjacency(all_hosts: list) -> dict[str, list]:
    """
    Build the full symmetric peer map.

    Returns {host_name: [peer_host_dict, ...]} where every edge is
    bidirectional: if a→b then b→a.
    """
    n   = len(all_hosts)
    adj = {i: _initial_peers(i, n, PEERS_PER_HOST) for i in range(n)}

    # Enforce symmetry
    for i in range(n):
        for j in list(adj[i]):
            adj[j].add(i)

    return {
        all_hosts[i]["name"]: [all_hosts[j] for j in sorted(adj[i])]
        for i in range(n)
    }

# ── Internal helpers ──────────────────────────────────────────────────────────

def config_dir(name: str) -> Path:
    return SIM_DIR / name / ".local" / "state" / "syncthing"

def sync_folder(name: str) -> Path:
    return SIM_DIR / name / "sync"

def cert_path(name: str) -> Path:
    return config_dir(name) / "cert.pem"

def generate_keys(name: str) -> None:
    config_dir(name).mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "HOME": str(SIM_DIR / name),
           "STNORESTART": "1", "STNOUPGRADE": "1"}
    result = subprocess.run(
        [SYNCTHING_BIN, "generate"],
        env=env, capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  [!] syncthing generate stderr: {result.stderr.strip()}",
              file=sys.stderr)

def extract_device_id(name: str) -> str:
    env = {
        **os.environ,
        "HOME": str(SIM_DIR / name),
        "STNORESTART": "1", "STNOUPGRADE": "1",
        "http_proxy": "", "HTTP_PROXY": "",
        "https_proxy": "", "HTTPS_PROXY": "",
        "no_proxy": "*", "NO_PROXY": "*",
    }
    for lock in (SIM_DIR / name).rglob("*.lock"):
        try:
            lock.unlink()
        except FileNotFoundError:
            pass

    pattern   = re.compile(r'device=([A-Z2-7]{7}(?:-[A-Z2-7]{7}){7})')
    TIMEOUT_S = 15

    proc = subprocess.Popen(
        [SYNCTHING_BIN, "serve", "--no-browser"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    collected, device_id, start = [], None, time.monotonic()
    try:
        while time.monotonic() - start < TIMEOUT_S:
            line = proc.stdout.readline()
            if line:
                collected.append(line)
                m = pattern.search(line)
                if m:
                    device_id = m.group(1)
                    break
            elif proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()

    if device_id:
        return device_id
    raise RuntimeError(
        f"Could not extract device ID for '{name}' within {TIMEOUT_S}s.\n"
        f"Output:\n{''.join(collected)[:1500]}"
    )

def write_config(host: dict, peers: list) -> None:
    """
    Write config.xml for `host` where `peers` is the list of peer host dicts
    as determined by the topology (already symmetric).
    """
    name = host["name"]
    ip   = host["ip"]
    cdir = config_dir(name)
    sdir = sync_folder(name)

    folder_devices = "\n".join(
        f'        <device id="{p["device_id"]}" introducedBy="">'
        f'<encryptionPassword></encryptionPassword></device>'
        for p in peers
    )
    # Top-level device entries: self + peers only
    device_entries = "".join(
        f'    <device id="{h["device_id"]}" name="{h["name"]}" '
        f'compression="metadata" introducedBy="">\n'
        f'        <address>tcp://{h["ip"]}:22000</address>\n'
        f'    </device>\n'
        for h in [host] + peers
    )

    xml = f"""<configuration version="52">
    <folder id="{FOLDER_ID}" label="Sync"
            path="{sdir.resolve()}"
            type="sendreceive" rescanIntervalS="{RESCAN_INTERVAL_S}"
            fsWatcherEnabled="false" fsWatcherDelayS="10" autoNormalize="true">
{folder_devices}
        <minDiskFree unit="%">1</minDiskFree>
        <versioning><cleanupIntervalS>3600</cleanupIntervalS></versioning>
        <markerName>.stfolder</markerName>
    </folder>
{device_entries}
    <gui enabled="true" tls="false" sendBasicAuthPrompt="false">
        <address>127.0.0.1:8384</address>
        <apikey>{name}apikey1234</apikey>
        <theme>default</theme>
    </gui>
    <ldap></ldap>
    <options>
        <listenAddress>tcp://{ip}:22000</listenAddress>
        <globalAnnounceEnabled>false</globalAnnounceEnabled>
        <localAnnounceEnabled>false</localAnnounceEnabled>
        <relaysEnabled>false</relaysEnabled>
        <natEnabled>false</natEnabled>
        <startBrowser>false</startBrowser>
        <autoUpgradeIntervalH>0</autoUpgradeIntervalH>
        <crashReportingEnabled>false</crashReportingEnabled>
        <stunServer></stunServer>
        <urAccepted>-1</urAccepted>
        <reconnectionIntervalS>20</reconnectionIntervalS>
    </options>
</configuration>
"""
    (cdir / "config.xml").write_text(xml)

def write_shadow_yaml(all_hosts: list) -> None:
    p = SHADOW_PARAMS
    host_blocks = ""
    for h in all_hosts:
        sim_home  = (SIM_DIR / h["name"]).resolve()
        pcap_line = (
            "    host_options:\n"
            "      pcap_enabled: true\n"
            "      pcap_capture_size: 65535\n"
        ) if p["pcap_enabled"] else ""
        host_blocks += (
            f"  {h['name']}:\n"
            f"    network_node_id: 0\n"
            f"    ip_addr: {h['ip']}\n"
            f"{pcap_line}"
            f"    processes:\n"
            f"    - path: {SYNCTHING_BIN}\n"
            f"      args: serve\n"
            f"      start_time: \"{h['start_time']}\"\n"
            f"      expected_final_state: running\n"
            f"      environment:\n"
            f"        HOME: {sim_home}\n"
            f"        STNORESTART: \"1\"\n"
            f"        STNOUPGRADE: \"1\"\n"
            f"        GOMAXPROCS: \"1\"\n"
            f"        GODEBUG: \"asyncpreempt=0\"\n\n"
        )
    yaml = (
        f"general:\n"
        f"  stop_time: \"{p['stop_time']}\"\n"
        f"  log_level: \"{p['log_level']}\"\n"
        f"  parallelism: {p['parallelism']}\n"
        f"  heartbeat_interval: \"1 sec\"\n"
        f"  model_unblocked_syscall_latency: true\n\n"
        f"experimental:\n"
        f"  unblocked_vdso_latency: \"0 ns\"\n"
        f"  unblocked_syscall_latency: \"15 ms\"\n\n"
        f"network:\n"
        f"  graph:\n"
        f"    type: 1_gbit_switch\n"
        f"    options:\n"
        f"      loss: 0.0\n"
        f"      latency: \"{p['latency_ms']} ms\"\n"
        f"      jitter: \"{p['jitter_ms']} ms\"\n\n"
        f"hosts:\n{host_blocks}"
    )
    Path("shadow.yaml").write_text(yaml)

def print_topology(all_hosts: list, adj: dict) -> None:
    print(f"\nTopology: {TOPOLOGY}  (PEERS_PER_HOST={PEERS_PER_HOST})\n")
    print(f"  {'Host':<6} {'Peers'}")
    print(f"  {'-'*6} {'-'*50}")
    for h in all_hosts:
        peers = adj[h["name"]]
        peer_str = ", ".join(p["name"] for p in peers)
        marker = " *" if len(peers) > PEERS_PER_HOST else ""
        print(f"  {h['name']:<6} {peer_str}{marker}")
    if any(len(adj[h["name"]]) > PEERS_PER_HOST for h in all_hosts):
        print("\n  * extra peer added to enforce bidirectionality")

# ── Actions ───────────────────────────────────────────────────────────────────

def do_setup(reset: bool) -> None:
    hosts = build_hosts()
    adj   = build_adjacency(hosts)

    if reset and SIM_DIR.exists():
        print(f"Removing {SIM_DIR}")
        shutil.rmtree(SIM_DIR)

    print(f"Simulation directory : {SIM_DIR}")
    print(f"Hosts ({len(hosts)})          : {', '.join(h['name'] for h in hosts)}")
    print_topology(hosts, adj)
    print()

    for h in hosts:
        if cert_path(h["name"]).exists():
            print(f"[{h['name']}] cert.pem exists — skipping key generation")
        else:
            print(f"[{h['name']}] Generating keys...")
            generate_keys(h["name"])

    print()
    for h in hosts:
        print(f"[{h['name']}] Reading device ID...", end=" ", flush=True)
        h["device_id"] = extract_device_id(h["name"])
        print(h["device_id"])

    print()
    for h in hosts:
        write_config(h, adj[h["name"]])
        print(f"[{h['name']}] Wrote config.xml  "
              f"(peers: {', '.join(p['name'] for p in adj[h['name']])})")

    print()
    for h in hosts:
        sdir = sync_folder(h["name"])
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / ".stfolder").touch()
        print(f"[{h['name']}] Sync folder: {sdir}")

    print()
    write_shadow_yaml(hosts)
    print("Wrote shadow.yaml")

    print("\n" + "=" * 68)
    print(f"  {'Host':<6} {'IP':<14} {'Start':<10} {'Device ID'}")
    print("-" * 68)
    for h in hosts:
        print(f"  {h['name']:<6} {h['ip']:<14} {h['start_time']:<10} {h['device_id']}")
    print("=" * 68)
    print("\nTo run the simulation:")
    print("  find sim -name '*.lock' -delete (to remove stale locks)") 
    print("  rm -rf shadow.data && shadow shadow.yaml > shadow.log")

def do_status() -> None:
    hosts = build_hosts()
    adj   = build_adjacency(hosts)
    print(f"  {'Host':<6} {'IP':<14} {'Cert':<6} {'Config':<8} {'Sync':<6} {'Peers':<20} {'Device ID'}")
    print("-" * 100)
    for h in hosts:
        name       = h["name"]
        has_cert   = "✓" if cert_path(name).exists() else "✗"
        has_config = "✓" if (config_dir(name) / "config.xml").exists() else "✗"
        has_sync   = "✓" if (sync_folder(name) / ".stfolder").exists() else "✗"
        peers_str  = ",".join(p["name"] for p in adj[name])
        did = "(no cert)"
        if cert_path(name).exists():
            try:
                did = extract_device_id(name)
            except Exception:
                did = "(error)"
        print(f"  {name:<6} {h['ip']:<14} {has_cert:<6} {has_config:<8} "
              f"{has_sync:<6} {peers_str:<20} {did}")

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reset",    action="store_true",
                    help="wipe SIM_DIR and regenerate everything")
    ap.add_argument("--status",   action="store_true",
                    help="print current state without making changes")
    ap.add_argument("--topology", action="store_true",
                    help="print peer map without making changes")
    args = ap.parse_args()

    hosts = build_hosts()
    adj   = build_adjacency(hosts)

    if args.topology:
        print_topology(hosts, adj)
    elif args.status:
        do_status()
    else:
        do_setup(reset=args.reset)

if __name__ == "__main__":
    main()