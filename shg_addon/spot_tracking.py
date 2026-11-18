"""Native SHG beam-walk calibration; no MATLAB or SpecialMeasure dependency.

Coordinates in saved native runs are zero-based camera pixels. Shifts and the
matrix convention match SHG_laserspot_tracking.m. Importing this module does
not open a camera, connect hardware, register actions, or move any output.
"""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import uuid
from datetime import datetime

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.signal import correlate, convolve2d
from shg_addon.configuration import ROOT

HOME = ROOT / "state" / "spot_tracking"
DATA = ROOT / "data" / "spot_tracking"
LOCK = threading.Lock()
ACTION_MODES = {
    "shg_track_laser": ("laser", "Track laser spot versus physical half-wave angle (moves rotator)"),
    "shg_track_flake": ("flake", "Track a selected flake feature versus physical half-wave angle (moves rotator)"),
    "shg_combine_tracking": ("combine", "Combine native laser/flake runs and configure beam-walk correction"),
}


def definitions():
    if (HOME / "disabled.json").exists():
        return []
    result = [dict(name=name, code="from shg_addon.spot_tracking import execute\nexecute(globals(), %r)" % mode,
                 description=description, confirm=False)
            for name, (mode, description) in ACTION_MODES.items()]
    from shg_addon.pixel_calibration import definition
    return result + [definition()]


def defaults():
    return dict(start=28., stop=208., step=3., settle=.75, radius=180,
                minimum_score=.35, patch_size=121, save_frames=True,
                pixel_per_piezo=[[-6.12962598, .20325898], [-.04442225, -6.11020477]],
                calibration_size=[1014, 683], laser_run="", flake_run="",
                active_table="", base_x=10., base_y=20.)


def read_json(path, default=None):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def write_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_name(p.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temp, p)
    finally:
        temp.unlink(missing_ok=True)


def settings():
    return dict(defaults(), **read_json(HOME / "settings.json", {}))


def gray(frame):
    a = np.asarray(frame, dtype=float)
    if a.ndim == 3 and a.shape[2] >= 3:
        a = a[..., :3] @ np.array([.298936, .587043, .114021])
    if a.ndim != 2 or not np.isfinite(a).all():
        raise ValueError("Camera frame must contain finite grayscale or RGB pixels")
    return a


def roi(value, shape):
    a = np.asarray(value, dtype=float)
    if a.shape != (4,) or not np.isfinite(a).all():
        raise ValueError("ROI requires x, y, width, height in camera pixels")
    x, y = np.floor(a[:2]).astype(int)
    w, h = np.ceil(a[2:]).astype(int)
    if min(x, y) < 0 or min(w, h) < 8 or x + w > shape[1] or y + h > shape[0]:
        raise ValueError("ROI must be inside the full frame and at least 8 by 8 pixels")
    return (int(x), int(y), int(w), int(h))


def crop(frame, r):
    x, y, w, h = r
    return frame[y:y+h, x:x+w]


def centered_roi(position, size, shape):
    w = h = int(size)
    if size != w or w < 8 or w > min(shape[:2]):
        raise ValueError("Laser patch must fit inside the frame and be at least 8 pixels")
    # MATLAB round uses half-away-from-zero; positions here are nonnegative.
    x, y = np.floor(np.asarray(position) - (w-1)/2 + .5).astype(int)
    return (int(np.clip(x, 0, shape[1]-w)), int(np.clip(y, 0, shape[0]-h)), w, h)


def initial_spot(frame, search_roi):
    values = gray(crop(frame, roi(search_roi, frame.shape)))
    if np.ptp(values) <= np.finfo(float).eps:
        raise ValueError("Laser search ROI has no usable intensity variation")
    background = np.median(values)
    signal = np.maximum(values-background, 0)
    smoothed = convolve2d(signal, np.ones((5, 5))/25, mode="same")
    py, px = np.unravel_index(np.argmax(smoothed.T), smoothed.T.shape)[::-1]
    x0, x1 = max(0, px-40), min(values.shape[1], px+41)
    y0, y1 = max(0, py-40), min(values.shape[0], py+41)
    local = signal[y0:y1, x0:x1]
    weights = np.maximum(local - .25*np.max(local), 0)
    if weights.sum() <= 0:
        raise ValueError("Could not find a bright laser spot in the selected ROI")
    yy, xx = np.mgrid[y0:y1, x0:x1]
    return np.array([search_roi[0] + (xx*weights).sum()/weights.sum(),
                     search_roi[1] + (yy*weights).sum()/weights.sum()])


def normalized_correlation(image, template):
    """MATLAB normxcorr2's fully-overlapping region, with zero variance rejected."""
    image, template = np.asarray(image, float), np.asarray(template, float)
    if image.ndim != 2 or template.ndim != 2 or any(a < b for a, b in zip(image.shape, template.shape)):
        raise ValueError("Search image must contain the whole tracking template")
    centered = template-template.mean()
    energy = np.sum(centered**2)
    if not np.isfinite(energy) or energy <= np.finfo(float).eps:
        raise ValueError("Tracking template has no usable intensity variation")
    # Local sums via integral images avoid a large all-ones convolution.
    def box_sum(a):
        integral = np.pad(a, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
        h, w = template.shape
        return integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]
    total = box_sum(image)
    variance = np.maximum(box_sum(image*image)-total*total/template.size, 0)
    denominator = np.sqrt(energy*variance)
    numerator = correlate(image, centered, mode="valid", method="fft")
    result = np.full(numerator.shape, -1.)
    np.divide(numerator, denominator, out=result, where=denominator > np.finfo(float).eps)
    return np.clip(result, -1, 1)


def parabolic(values, index):
    if index <= 0 or index >= len(values)-1:
        return 0.
    left, center, right = values[index-1:index+2]
    denominator = left-2*center+right
    return float(np.clip(.5*(left-right)/denominator, -.5, .5)) if abs(denominator) > 1e-12 else 0.


def track_template(frame, template, previous, radius):
    frame = gray(frame)
    h, w = template.shape
    # Equivalent to the MATLAB one-based search limits, converted to zero-based.
    x0 = max(0, int(np.floor(previous[0]-radius-w/2)))
    y0 = max(0, int(np.floor(previous[1]-radius-h/2)))
    x1 = min(frame.shape[1], int(np.ceil(previous[0]+radius+w/2))+1)
    y1 = min(frame.shape[0], int(np.ceil(previous[1]+radius+h/2))+1)
    scores = normalized_correlation(frame[y0:y1, x0:x1], template)
    px, py = np.unravel_index(np.argmax(scores.T), scores.T.shape)
    position = [x0+px+parabolic(scores[py, :], px)+(w-1)/2,
                y0+py+parabolic(scores[:, px], py)+(h-1)/2]
    return np.asarray(position), float(scores[py, px])


def angles_from_config(cfg):
    values = np.asarray([cfg[k] for k in ("start", "stop", "step", "settle", "radius", "minimum_score")], float)
    start, stop, step, settle, radius, score = values
    if not np.isfinite(values).all() or not 0 <= start < stop <= 340 or step <= 0 or settle < 0 or radius < 1 or not 0 < score <= 1:
        raise ValueError("Check angles (0..340), positive step/radius, settle >=0, and score (0..1]")
    count = int(np.floor((stop-start)/step+1e-9))+1
    if count < 2 or count > 10000:
        raise ValueError("Choose between 2 and 10000 half-wave points")
    return start+step*np.arange(count)


def _flush_tracking(driver):
    if not driver.options["simulate"]:
        driver.camera.put_Option(driver.api.TOUPCAM_OPTION_FLUSH, 3)
        driver.ready.clear()
    return True


def fresh_frame(ns):
    # Also attaches to the class already registered by a running SHG backend.
    # The instrument proxy executes this under easyMeasure's resource lock.
    from shg_addon.toup_microscope import ToupMicroscope
    ToupMicroscope.flush_tracking = _flush_tracking
    ns["check"]()
    ns["instrument"]("microscope").call("flush_tracking")
    # Discard a possibly in-flight exposure. The next exposure starts after
    # flushing; preview may consume frames but cannot reintroduce old frames.
    ns["get"]("microscope.image")
    ns["check"]()
    result = np.asarray(ns["get"]("microscope.image"), float)
    gray(result)
    return result


def dialog(ns, task, payload, frame=None):
    """Independent Qt UI keeps the existing backend and GUI alive; abort closes it."""
    request_dir = HOME / "requests" / uuid.uuid4().hex
    request_dir.mkdir(parents=True)
    request = request_dir / "request.json"
    if frame is not None:
        np.save(request_dir / "frame.npy", frame, allow_pickle=False)
    write_json(request, dict(task=task, payload=payload))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen([sys.executable, "-B", "-m", "shg_addon.spot_tracking_dialog", str(request)],
                               cwd=str(ROOT.parent), env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            ns["sleep"](.1)
        ns["check"]()
        response = read_json(request_dir / "response.json")
        if response is None:
            raise RuntimeError("Tracking dialog failed to open or exited unexpectedly")
        if response.get("error"):
            raise ValueError(response["error"])
        return response.get("value")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        # Request images can be large; preserve no duplicate frame after UI exit.
        for p in request_dir.iterdir():
            p.unlink(missing_ok=True)
        request_dir.rmdir()


def _save_npz(path, **arrays):
    p = Path(path)
    temp = p.with_name(p.name + ".tmp")
    with temp.open("wb") as stream:
        np.savez_compressed(stream, **arrays)
    os.replace(temp, p)


def require_microscope(ns):
    """Inspect the registered handle only; never connect or query hardware."""
    ns["check"]()
    from easymeasure.common.errors import ChannelError
    try:
        microscope = ns["instrument"]("microscope")
    except ChannelError as exc:
        raise ValueError(
            "Microscope camera is not in the loaded setup. Open the Microscope "
            "view and click Connect microscope, then run this action again. "
            "The instrument named 'camera' is the SHG spectrometer, not the "
            "ToupCam microscope. Release the camera in MATLAB/ToupView if it "
            "is in use there. No half-wave or X/Y movement was requested."
        ) from exc
    if microscope.state != "connected":
        raise ValueError(
            "Microscope camera is %s. Reconnect the microscope in Setup > "
            "Instruments, then run tracking again. Release it in MATLAB/ToupView "
            "if needed. No half-wave or X/Y movement was requested."
            % microscope.state)


def acquire_tracking(ns, mode, cfg):
    angles = angles_from_config(cfg)
    require_microscope(ns)
    # Fail before motion if microscope is disconnected or exposure is excessive.
    size = np.asarray(ns["get"]("microscope.sensor_size"), int)
    exposure = float(ns["get"]("microscope.exposure"))
    if not np.isfinite(exposure) or not 0 < exposure <= 4:
        raise ValueError("Set microscope exposure to 4 seconds or less before tracking")
    ns["get"]("halfwave")
    ns["check"]()
    ns["set"]("halfwave", float(angles[0]))
    ns["sleep"](float(cfg["settle"]))
    first = fresh_frame(ns)
    if list(size) != [first.shape[1], first.shape[0]]:
        raise ValueError("Microscope sensor size disagrees with the captured frame")
    selection = dialog(ns, "roi", dict(mode=mode, config=cfg), first)
    if selection is None:
        ns["log"]("SHG %s tracking cancelled at ROI selection; half-wave remains at %.3f degrees", mode, angles[0])
        return None
    selected_roi = roi(selection["roi"], first.shape)
    if mode == "laser":
        initial = np.asarray(selection["position"], float)
        if initial.shape != (2,) or not np.isfinite(initial).all() or not (0 <= initial[0] < size[0] and 0 <= initial[1] < size[1]):
            raise ValueError("Selected laser center is outside the camera frame")
        patch_roi = centered_roi(initial, cfg["patch_size"], first.shape)
    else:
        patch_roi = selected_roi
        x, y, w, h = patch_roi
        initial = np.array([x+(w-1)/2, y+(h-1)/2])
    template = gray(crop(first, patch_roi)).copy()
    if np.std(template) <= np.finfo(float).eps:
        raise ValueError("Selected template has no intensity variation; select a distinctive feature")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
    root = DATA / (mode + "_" + stamp)
    root.mkdir(parents=True)
    meta = dict(format="easyMeasure-shg-tracking-1", mode=mode, status="running", config=cfg,
                camera_size=size.tolist(), roi=list(selected_roi), patch_roi=list(patch_roi),
                coordinates="zero-based full-frame camera pixels", created=datetime.now().isoformat())
    positions, scores = [], []
    def save(status):
        meta.update(status=status, completed=len(positions), total=len(angles))
        write_json(root / "run.json", meta)
        _save_npz(root / "tracking.npz", angles=angles[:len(positions)],
                  positions=np.asarray(positions).reshape(-1, 2), scores=np.asarray(scores),
                  camera_size=size, mode=np.array(mode), complete=np.array(status == "complete"))
    try:
        from PIL import Image
        for k, angle in enumerate(angles):
            ns["check"]()
            if k:
                ns["set"]("halfwave", float(angle))
                ns["sleep"](float(cfg["settle"]))
                frame = fresh_frame(ns)
                if frame.shape != first.shape:
                    raise ValueError("Camera resolution changed during tracking")
                position, score = track_template(frame, template, positions[-1], cfg["radius"])
                if not np.isfinite(score) or score < cfg["minimum_score"]:
                    raise ValueError("%s tracking lost at %.3f degrees: correlation %.4f below %.4f" %
                                     (mode, angle, score, cfg["minimum_score"]))
            else:
                frame, position, score = first, initial, np.nan
            positions.append(np.asarray(position)); scores.append(score)
            # Checkpoint partial numeric data at every successfully tracked point.
            save("running")
            if cfg.get("save_frames", True):
                for dirname, image in (("images", frame), ("roi_images", crop(frame, selected_roi))):
                    folder = root / dirname
                    folder.mkdir(exist_ok=True)
                    Image.fromarray(np.clip(image, 0, 255).astype(np.uint8)).save(folder / ("%04d_hw_%.6f.png" % (k, angle)))
            ns["log"]("SHG %s %d/%d: halfwave %.3f, position [%.3f, %.3f], score %s",
                        mode, k+1, len(angles), angle, *position, "reference" if not k else "%.4f" % score)
        save("complete")
    except BaseException as exc:
        meta["error"] = str(exc)
        save("incomplete")
        raise
    latest = settings()
    latest[mode+"_run"] = str(root / "tracking.npz")
    write_json(HOME / "settings.json", latest)
    ns["log"]("SHG %s tracking complete: %s. Half-wave stays at %.3f; X/Y were not moved.", mode, root, angles[-1])
    return root / "tracking.npz"


def load_run(path, expected):
    with np.load(path, allow_pickle=False) as data:
        d = {k: data[k].copy() for k in data.files}
    if str(d.get("mode")) != expected or not bool(d.get("complete", False)):
        raise ValueError("Select a completed native %s run (tracking.npz)" % expected)
    angles, positions = d["angles"], d["positions"]
    if angles.ndim != 1 or len(angles) < 2 or positions.shape != (len(angles), 2) or not np.isfinite(positions).all() or not np.isfinite(angles).all() or np.any(np.diff(angles) <= 0):
        raise ValueError("Invalid or incomplete tracking positions/angles")
    return d


def combine_runs(laser, flake, matrix, calibration_size):
    if laser["angles"].shape != flake["angles"].shape or not np.allclose(laser["angles"], flake["angles"], atol=1e-9, rtol=0):
        raise ValueError("Laser and flake runs must have identical half-wave angles")
    if not np.array_equal(laser["camera_size"], flake["camera_size"]):
        raise ValueError("Laser and flake frame sizes differ; repeat them at the same camera resolution")
    if not np.array_equal(laser["camera_size"], calibration_size):
        raise ValueError("Pixel calibration is for %s, but runs are %s. Supply a calibration measured at this resolution; no automatic pixel rescaling is applied." %
                         (list(calibration_size), laser["camera_size"].tolist()))
    matrix = np.asarray(matrix, float)
    if matrix.shape != (2, 2) or not np.isfinite(matrix).all() or np.linalg.cond(matrix) > 1e8:
        raise ValueError("Pixel-per-piezo matrix must be finite, 2 by 2, and nonsingular")
    laser_shift = laser["positions"]-laser["positions"][0]
    flake_shift = flake["positions"]-flake["positions"][0]
    relative = laser_shift-flake_shift
    correction = np.linalg.solve(matrix, relative.T).T
    return dict(angles=laser["angles"], laser_shift=laser_shift, flake_shift=flake_shift,
                relative_shift=relative, correction=correction, pixel_per_piezo=matrix,
                camera_size=laser["camera_size"])


def combine_tracking(ns, cfg):
    laser, flake = load_run(cfg["laser_run"], "laser"), load_run(cfg["flake_run"], "flake")
    result = combine_runs(laser, flake, cfg["pixel_per_piezo"], cfg["calibration_size"])
    root = DATA / ("combined_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    table = root / "correction.npz"
    _save_npz(table, **result)
    write_json(root / "profile.json", dict(format="easyMeasure-shg-correction-1", config=cfg,
                                          interpolation="PCHIP", angle="physical half-wave degrees",
                                          sign="laser shift minus flake shift", stage_unit="X/Y alias units"))
    with (root / "correction.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["halfwave_deg", "laser_shift_x_px", "laser_shift_y_px", "flake_shift_x_px", "flake_shift_y_px", "relative_shift_x_px", "relative_shift_y_px", "dX_piezo", "dY_piezo"])
        writer.writerows(np.column_stack([result[k] for k in ("angles", "laser_shift", "flake_shift", "relative_shift", "correction")]))
    response = dialog(ns, "result", dict(table=str(table), config=cfg))
    # No hardware API is called by combining or enabling a saved table.
    latest = settings()
    latest.update({k: cfg[k] for k in ("laser_run", "flake_run", "pixel_per_piezo", "calibration_size")})
    if response is not None:
        latest.update(active_table=str(table) if response["enable"] else "",
                      base_x=response["base_x"], base_y=response["base_y"])
    write_json(HOME / "settings.json", latest)
    ns["log"]("SHG correction saved: %s; table %s. No hardware moved.", table,
                "enabled for future corrected moves/acquire_point hooks" if latest["active_table"] else "disabled")
    return table


def execute(ns, mode):
    if (HOME / "disabled.json").exists():
        raise ValueError("SHG tracking was rolled back; reinstall it before use")
    if mode not in ("laser", "flake", "combine"):
        raise ValueError("Unknown SHG tracking action")
    if not LOCK.acquire(blocking=False):
        raise ValueError("Another SHG tracking action is already open/running")
    try:
        if mode != "combine":
            require_microscope(ns)
        cfg = dialog(ns, "configure", dict(mode=mode, config=settings()))
        if cfg is None:
            ns["log"]("SHG %s tracking cancelled before any hardware operation", mode)
            return
        if mode == "combine":
            return combine_tracking(ns, cfg)
        angles_from_config(cfg)
        # Remember acquisition preferences, preserving the active correction.
        latest = settings()
        latest.update({k: cfg[k] for k in ("start", "stop", "step", "settle", "radius", "minimum_score", "patch_size", "save_frames")})
        write_json(HOME / "settings.json", latest)
        return acquire_tracking(ns, mode, cfg)
    finally:
        LOCK.release()


def table_offset(angle, table):
    with np.load(table, allow_pickle=False) as data:
        angles, corrections = data["angles"], data["correction"]
    if angles.ndim != 1 or len(angles) < 2 or corrections.shape != (len(angles), 2) or not np.isfinite(angles).all() or not np.isfinite(corrections).all() or np.any(np.diff(angles) <= 0):
        raise ValueError("Invalid tracking correction table; disable it or recombine valid runs")
    if not np.isfinite(angle) or not angles[0] <= angle <= angles[-1]:
        raise ValueError("Half-wave %.12g is outside the tracking calibration %.12g..%.12g degrees" % (angle, angles[0], angles[-1]))
    return PchipInterpolator(angles, corrections, axis=0, extrapolate=False)(angle)


def corrected_xy(x, y, polarization):
    cfg = settings()
    if not cfg["active_table"] or (HOME / "disabled.json").exists():
        from shg_addon.analysis import corrected_xy as old_correction
        return old_correction(x, y, polarization)
    angle = 28+float(polarization)/2
    dx, dy = table_offset(angle, cfg["active_table"])
    return float(x)+dx, float(y)+dy, angle


def before_physical_point(ns):
    """Explicitly enabled raw-halfwave sweep correction, before SHG acquisition."""
    cfg = settings()
    if not cfg["active_table"] or (HOME / "disabled.json").exists():
        return
    # Calibration angles describe commanded half-wave positions. Use the same
    # coordinate in a sweep: tiny encoder errors at 28/208 must not turn valid
    # endpoints into uncalibrated positions. Outside a sweep, retain the measured
    # angle and strict range check. Do not clamp or extrapolate outside the table.
    setpoints = ns.get("setpoints", {})
    angle = float(setpoints["halfwave"]) if "halfwave" in setpoints else float(ns["get"]("halfwave"))
    dx, dy = table_offset(angle, cfg["active_table"])
    x, y = float(cfg["base_x"])+dx, float(cfg["base_y"])+dy
    if not np.isfinite([x, y]).all() or not (0 <= x <= 100 and 0 <= y <= 100):
        raise ValueError("Tracking-corrected X/Y exceeds 0..100 stage bounds")
    ns["set"]("X", float(x)); ns["set"]("Y", float(y))
    ns["sleep"](.1)
