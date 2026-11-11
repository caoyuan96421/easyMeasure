# Testing

## The test suite

```bash
pip install -e ".[test]"
python -m pytest -q tests          # a few minutes; GUI tests run offscreen
python -m pytest -q tests/test_composites.py -k loops     # one file / test
```

| file | covers |
|---|---|
| `test_codec_exprs.py` | the wire codec (fuzzed), the expression sandbox |
| `test_resources_instruments.py` | fair / priority locks, port arbitration, hung / crashing / garbage-returning drivers, circuit breaker |
| `test_channels.py`, `test_composites.py` | limits, ramp rates, scaling; composite writes, reads, mappings, nesting, loops |
| `test_sweep.py` | N-D sweeps with vector and image data, mappings, hooks, flow control, abort / pause |
| `test_storage.py` | crash recovery of truncated or corrupted files, disk failure during a sweep, HDF5 |
| `test_robustness.py` | scripts (including an un-cooperative infinite loop), a frozen client, a SIGKILLed frontend, a backend restart under a live GUI |
| `test_server.py` | the protocol, events, many clients |
| `test_drivers.py`, `test_cygnus.py`, `test_ni63xx.py` | every hardware driver against its emulator |
| `test_dynamic_channels.py` | allowed values; channel lists that change with modes, also in sweeps |
| `test_actions.py`, `test_user_actions.py` | instrument actions, user actions |
| `test_interfaces.py` | addresses, interfaces, discovery |
| `test_driver_wizard.py` | the wizard: templates, generated drivers, try-it, save / reload |
| `test_gui.py` | the desktop panels, offscreen |
| `test_remote.py`, `test_acme.py` | the gateway and the certificate client (see below) |
| `test_web_ui.py` | the web app in Chromium (Playwright) |
| `test_docs.py` | this documentation's generated pages |

### Driver tests

`tests/test_drivers.py` runs every hardware driver against its emulator. It checks:

* that every command a driver sends exists in the instrument's command table;
* that settings round-trip through all their allowed values;
* that mode switches rename channels and change units;
* that refused values come back with the instrument's own error message;
* that a noisy serial line is survived;
* that several instruments can share one GPIB adapter;
* that an I–V sweep in current-source mode runs from the example setup.

### Remote access tests

`tests/test_remote.py` checks the gateway:

* TLS versions and pinning, security headers, static-file traversal and cookie flags;
* generic login errors and brute-force lock-out;
* WebSocket authentication and Origin checks;
* every role's permissions, end to end;
* sessions closed on account changes;
* malformed, oversized and non-WebSocket traffic;
* rate, in-flight and connection limits;
* slow-loris sockets, a client that never reads, and idle connections;
* backend and gateway restarts (sessions survive), and a SIGKILLed gateway (the
  backend restarts it);
* downloads, the expression sandbox and performance.

`tests/test_acme.py` runs the Let's Encrypt client against a local fake ACME server.

### Web app tests

`tests/test_web_ui.py` drives the web app in Chromium. It needs:

```bash
pip install -e ".[webtest]"
python -m playwright install chromium
```

## Stress tests

```bash
python examples/stress_test.py --minutes 10                    # faulty hardware
python examples/remote_stress_test.py --minutes 10 --browser   # remote access under attack
```

**`stress_test.py`** runs a backend process with the faulty setup. Six clients
hammer it with:

* reads, out-of-limit writes and configuration-channel writes;
* every instrument action, including ones that fail, hang forever or raise `SystemExit`;
* sweeps (some with actions in their hooks) and scripts;
* pause / abort, malformed requests and abrupt disconnects.

A frozen client and a client sending random bytes join in.

At the end it checks that:

* the backend is alive and responsive;
* every protected value is within its limits;
* a normal sweep still completes;
* every data file loads.

It also reports the backend's memory over time.

**`remote_stress_test.py`** runs the faulty-hardware stress test while legitimate
remote clients work and attackers:

* guess passwords;
* send random, malformed and oversized messages;
* send forbidden commands and sandbox escapes;
* flood requests and open slow-loris sockets;
* in one case, never read.

It checks that:

* nothing forbidden ever succeeded;
* the probe client never failed;
* both processes survived with bounded memory;
* a remote sweep still completes.

A 10-minute run on a 2-core machine at load 12 sent 130 000 fuzz messages, 590
password guesses, 11 700 forbidden requests and 155 000 rate-limited requests.
Results:

* no forbidden request succeeded;
* 0 of 1545 probe pings failed (p50 7 ms, p99 0.4 s through the gateway);
* the gateway stayed at 180–215 MB.

## Documentation

```bash
pip install -e ".[docs]"
mkdocs build --strict
```

The pages for drivers, commands, expressions, the command line and the Python API
are generated from the code (`docs/gen_reference.py`), so they cannot go stale.
`tests/test_docs.py` checks that the generator covers every driver and command.
