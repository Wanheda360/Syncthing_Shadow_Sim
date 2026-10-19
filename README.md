# Syncthing_Shadow_Sim
Contains a script for setting up running Syncthing in the Shadow network simulator

# setup_sim

Sets up a network of Syncthing peers for the Shadow network simulator. Given a number of hosts and a topology, it generates keys and a Syncthing config for each host (connected only to its peers) and writes a ready-to-run `shadow.yaml`.

Requires Python 3.9+ (standard library only), Shadow, and a built Syncthing binary. Set `SYNCTHING_BIN` at the top of the script to the path of the binary.

To run, from the script's directory:

```
python3 setup_sim.py
```

Files in a host's sync folder at the start of the simulation are shared with its peers. Running the script again keeps existing keys (so device IDs stay the same) and rewrites the configs and `shadow.yaml`. Other options:

- `--topology` shows which hosts connect to which, without changing anything.
- `--status` shows the state of each host.
- `--reset` deletes `sim/` and starts again from scratch, including any files in the sync folders.

Variables at the top of the script:

- `NUM_HOSTS`: number of hosts (up to 26), named `a`, `b`, `c`, ... with IPs `11.0.0.1`, `11.0.0.2`, ...
- `TOPOLOGY` (`ring`, `random_fixed` or `full_mesh`) and `PEERS_PER_HOST`: how the hosts are connected.
- `RESCAN_INTERVAL_S`: how often Syncthing rescans its folder for changes.
- `SHADOW_PARAMS`: `stop_time`, `latency_ms`, `jitter_ms`, `pcap_enabled` (packet capture on every host), `log_level` and `parallelism`. Make sure `stop_time` is long enough for the transfer to finish.

Syncthing makes a very large number of system calls, so `unblocked_syscall_latency` in `write_shadow_yaml()` (15 ms) matters: too low and the simulation takes extremely long to run, too high and the simulated time runs out before Syncthing has done its work.

The generated `shadow.yaml` is included and can be used as a template for running other applications in Shadow: change the process `path`, `args` and `environment`.
