# SHG laboratory add-on

Native second-harmonic-generation acquisition and analysis for easyMeasure:
sample/reference spectra, Morse CWT phase and amplitude, background and phase
calibration, half-wave/MEMS/z sweeps, microscope tracking, and pixel-to-X/Y
calibration. Runtime measurements and calibration run entirely in Python.

The add-on subclasses the normal frontend/backend and uses easyMeasure's
standard Sweep editor, aliases, actions, queue, storage, and abort controls.
It does not require changes to the core drivers. This publication includes the
compatibility adapter for current upstream NI-DAQ dispatch.

## Install and launch

From a checkout, create a virtual environment and install the optional SHG
dependencies:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[shg]"
.venv\Scripts\python -m shg_addon.launcher
```

`run_shg.bat` launches that environment on Windows. An installed package also
provides the `easymeasure-shg` entry point. The SHG backend uses local port 47821
and its own state directory. Closing the GUI leaves its backend running.

### Camera and stage dependencies

Simulation requires no camera SDK or physical instrument. Real acquisition
requires the relevant vendor runtimes:

- ZWO ASI spectrometer camera: install the ASI SDK/ASIStudio and provide
  `ASICamera2.dll` through the camera's `sdk_path` option or the documented local
  SDK location. The supplied ASI binding targets Windows.
- ToupCam microscope: obtain the vendor SDK and put its `toupcam.py` plus matching
  64-bit native library in `shg_addon/vendor/`; see
  [vendor/README.txt](vendor/README.txt). Vendor code and libraries are not included.
- NI USB-6211: install NI-DAQmx for real devices; `DAQ::SIM` uses the existing
  emulator. The add-on's output-only configuration can avoid reserving counters.
- Newport SMC100: configure its actual serial port. The sample lab configuration
  uses COM5; replace it with the correct port for the installation.

## Initial setup

On a new installation, check **Load simulated setup**, then **Load selected
setup** to try the full SHG workflow without hardware. Set up the background and
run **Run center + phase calibration**, then capture sample/reference spectra.

For real use, configure instruments and aliases in the normal setup panels and
save the setup. Release instruments from other applications before connecting.
Startup restores the last SHG autosave or a locally saved laboratory profile;
connection definitions are retained, not cached composite setpoints. Supply
outputs are not enabled by the launch or preset-loading controls.

Typical aliases include `halfwave`, physical `X`/`Y`, `SampRef`, `Shutter`, and
`ZWOSpectrum`, `ZWOPhase`, `ZWOAmplitude`, `SHGframe`. The MEMS presets require an
existing `MEMS.theta`/`MEMS.Vd` composite. The z preset also needs `Vlock` and `Vz`
aliases on separate Cygnus supply channels. Adapt the device-specific formulas,
channel limits, current limits, and ramps to the apparatus before running them.

The sample stage scaling is 0–100 alias units mapped to 0–10 V. These alias units
are not assumed to be micrometers. The default camera region uses 1-based,
inclusive image coordinates. The included CWT filter banks target 5496-pixel
spectra; changing that width needs compatible filters and a new phase calibration.

## Actions

| Action | Behavior |
|---|---|
| `shg_sample` / `shg_reference` | Switch optical path and capture a fresh spectrum using the current exposure. |
| `shg_sample_center` / `shg_reference_center` | Capture at 1 s / 2 s and select the CWT analysis center. These do not center the physical laser spot. |
| `shg_calibrate_all` | Select both centers and calculate the phase offset from five sample/reference pairs. |
| `shg_refresh_reference` | Capture a fresh reference (Manual default 5 s), return to sample path, restore the previous exposure. |
| `shg_background` | Block the shutter and capture a dark background at 1 s. Reopen only on success; exposure remains 1 s. |
| `shg_acquire_point` | Apply enabled X/Y correction, refresh the reference if periodic refresh is enabled, then acquire a fresh sample. Use in the inner pre-record hook. |
| `shg_track_laser` / `shg_track_flake` | Separate microscope tracking runs versus physical half-wave angle. |
| `shg_combine_tracking` | Combine native laser/flake runs, review the correction table, optionally enable it. Combining/enabling does not itself move X/Y. |
| `shg_calibrate_pixels_xy` | Track a stage-attached feature through reviewed X/Y moves and fit the full 2×2 pixel/stage matrix. |

Loops continue until aborted. Tracking/calibration actions show settings and ROI
review windows. Abort stops at the next checkpoint; an in-progress hardware call
can finish first. The standalone `camera.*` actions are lower-level operations.

## Sweep presets

In **Sweep editor → SHG actions**, choose a preset and **Load selected preset**.
Selection/loading edits only the draft. Use **Sweep → Validate**, then **Add to
queue**; **Add & start** also starts the queue.

| Preset | Dimensions, fastest first |
|---|---|
| SHG half-wave | `halfwave` 28–208°, 61 points |
| SHG half-wave + MEMS | Half-wave × `MEMS.theta` −10–10°, 21 positions |
| SHG half-wave + MEMS + z | Half-wave × MEMS × `Vz` 0–40 V, 9 positions |

For z, edit the displayed **start**, **stop**, and **step** fields before loading.
All dimension ranges/point counts remain editable afterward, and saved sweep JSON
preserves each measurement's configuration. The default third preset records
11,529 spectra; set point counts and exposure deliberately.

All presets have a fresh-acquisition pre-record hook and a `camera.exposure = 1`
pre-sweep constant (seconds). Edit that constant before queueing to choose another
exposure. Editing the editor does not change a copy already queued. Recording a
cached spectrum channel alone does not acquire a new frame.

The MEMS presets preserve drive voltage and output enables; they require a finite
positive `MEMS.Vd`. The z preset sets `Vlock` to 70 V through the existing ramp and
requires its output enabled and measured voltage within ±0.5 V of 70 V before a
new `Vz` write. It checks again before every z step and spectrum. Outputs are
enabled explicitly by the operator. Keep its preparation and guard hooks enabled.
The checks belong to this preset, not a global restriction on Manual/raw writes.
Vlock remains at 70 V after completion/abort; Vz remains at its last command.

For a dimension, optional actions execute **before set → set → after set →
settle → pre-record → record → post-record**. The separate pre/post-sweep tabs
execute once for the complete measurement. Use Live data's **Add plot** and each
plot's channel selector to display recorded amplitude/phase in addition to spectra.

## Beam-walk and pixel/stage calibration

1. Connect the microscope and arrange laser illumination. Run `shg_track_laser`,
   verify the detected spot/template, and scan the physical half-wave grid.
2. Arrange flake illumination. Run `shg_track_flake` on the same angle grid,
   resolution, zoom, and orientation, selecting a compact distinctive feature.
3. Run `shg_calibrate_pixels_xy` to measure the pixel/stage mapping if it is not
   already calibrated for that camera geometry. Review/edit every stage position
   before starting. The default path uses local bidirectional moves; the legacy
   22-point path is also available. X/Y are moved directly, with no half-wave or
   supply commands. Cancellation/abort does not issue an automatic return move.
4. Review the matrix, RMS residuals, and return-to-origin errors. Select **Use this
   matrix…** and Save to populate future Combine dialogs with the measured matrix
   and actual image size. A fit over the chosen RMS limit cannot be applied.
5. Reopen Combine, choose the native runs, confirm the pixel calibration, and
   review the PCHIP table. Opt in to enabling it for future acquire-point hooks.

The fit convention is `[du;dv] = M [dX;dY]`, with camera columns/rows `u/v`. Combine
uses `M^-1 (laser_shift − flake_shift)`; no automatic pixel resizing is applied.
The legacy numerical defaults are apparatus-specific examples, not a calibration
for a new camera. Measure a matrix for its actual resolution, scale, and orientation.

An enabled table replaces the legacy sinusoidal X/Y correction. For raw half-wave
sweeps the acquisition hook uses `X = base X + dX(angle)`, likewise Y. Lookup uses
the commanded sweep coordinate, so small encoder readback errors do not reject a
valid endpoint. Actual out-of-range setpoints are rejected, without extrapolation.
Direct Manual X/Y writes and capture actions without the hook are not corrected.

To disable a table, reopen Combine and Save the result with **Enable…** unchecked;
Cancel preserves the previous activation setting. The result checkbox is a choice
to save, not a persistent live-status indicator. Disabling does not move X/Y back.

## Storage and privacy

The backend data directory is shown in **File → Settings**. Completed/partial
sweeps use native `.emj` journals and optional `.h5` exports; reopen them with
**Open data**. Failed/aborted runs can be continued through the Sweep menu.

Local SHG profiles, autosaves, access tokens, and calibrations are under
`shg_addon/state/`. Acquisition/tracking results are under `shg_addon/data/` or
the instrument's configured data path. Both directories are ignored by Git.
No laboratory data, saved connection profiles, vendor DLLs, or SDK bindings are
distributed here. The two shipped CWT banks are numerical filter coefficients;
the test golden fixture contains only synthetic sinusoids and impulses.

## Tests

```powershell
.venv\Scripts\python -m pip install -e ".[shg,test]"
.venv\Scripts\python -m pytest tests/shg --confcutdir=tests/shg -q
```

Tests use emulated/mocked outputs, an isolated home, and an offscreen Qt GUI.
The SHG-only command isolates teardown from the core suite's deliberate
hung-driver process-exit workaround. Test directories and generated data are not
needed for runtime installation. Native camera acquisition and optical alignment
still need apparatus-specific validation.
