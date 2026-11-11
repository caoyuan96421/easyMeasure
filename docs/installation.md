# Installation

easyMeasure needs **Python 3.9 or newer** and runs on Windows, Linux and macOS.
Measurements with real instruments are usually done on Windows, where the vendors'
VISA and DAQ runtimes are best supported.

## From the repository

```bash
git clone https://github.com/caoyuan96421/easyMeasure.git
cd easyMeasure
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Linux / macOS
pip install -e .
```

`pip install -e .` installs easyMeasure with everything it needs:

| package | for |
|---|---|
| `numpy`, `h5py` | data, HDF5 export |
| `PySide6`, `pyqtgraph`, `matplotlib` | desktop GUI, plots, slide figures |
| `python-pptx` (+ `pywin32` on Windows) | PowerPoint slides, also into an open presentation |
| `pyvisa`, `pyserial` | GPIB / USB / LAN / serial instruments |
| `nidaqmx` | NI DAQ cards (`ni6211`, `ni63xx`) |
| `aiohttp`, `cryptography` | remote access (HTTPS / WSS gateway, certificates) |

Optional extras:

```bash
pip install -e ".[hardware]"     # + pyvisa-py: VISA without NI-VISA (LAN, USB-TMC, serial)
pip install -e ".[test]"         # + pytest, pytest-timeout
pip install -e ".[webtest]"      # + playwright (browser test of the web app)
python -m playwright install chromium   # after [webtest]
```

### Windows shortcuts

The repository has three batch files:

| file | does |
|---|---|
| `setup_venv.bat` | creates `.venv` and installs everything |
| `run_demo_backend.bat` | starts a backend with the simulated demo instruments |
| `run_gui.bat` | starts the GUI |

## Vendor runtimes for real instruments

pip cannot install the vendors' driver runtimes. Install the ones your
instruments need:

| instruments | install |
|---|---|
| GPIB, USB-TMC, VISA LAN (`GPIB0::…`, `USB0::…`, `TCPIP0::…`) | a VISA library: **NI-VISA** or **Keysight IO Libraries Suite**, plus the GPIB card's driver (NI-488.2 for NI cards). Without a vendor VISA, `pip install pyvisa-py` covers LAN, USB-TMC and serial. |
| NI DAQ (`DAQ::Dev1`) | **NI-DAQmx** (the Python package `nidaqmx` already comes with easyMeasure) |
| Serial / USB-serial (`COM3`, `/dev/ttyUSB0`) | nothing beyond the adapter's driver (FTDI, CH340 …) |
| Raw TCP sockets, Prologix GPIB-Ethernet (`TCP::host::port`) | nothing |

You can check what the backend computer sees from the GUI: *Instruments → Add…*
lists the ports and devices found per interface, and **Refresh** scans again. The
backend command [`discover_resources`](reference/commands.md#discover_resources)
does the same.

If pyvisa, a VISA library or the NI-DAQmx runtime is missing, the scan says so.
Nothing else is affected, and the simulated instruments always work.

## Check the installation

```bash
python -m easymeasure.backend --help
python -m easymeasure.frontend --start-backend     # GUI + a backend in the background
```

The GUI opens and asks for a data folder and a PowerPoint file the first time.
Continue with the [Quick start](quickstart.md).

## Run the tests (optional)

```bash
pip install -e ".[test]"
python -m pytest -q tests            # a few minutes; GUI tests run offscreen
```

See [Testing](guide/testing.md) for the stress tests.

## Build this documentation

```bash
pip install -e ".[docs]"
mkdocs serve                          # http://127.0.0.1:8000
```

The reference pages for drivers, commands and modules are generated from the code
at every build. The site on GitHub Pages is rebuilt by a GitHub Actions workflow on
every push to `main`.
