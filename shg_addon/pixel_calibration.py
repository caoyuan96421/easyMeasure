"""Native camera-pixel / physical X,Y alias calibration, based on piezo_pixel.m."""
from datetime import datetime
import csv
import uuid
import numpy as np
from shg_addon import spot_tracking as tracking

NAME = "shg_calibrate_pixels_xy"


def definition():
    return dict(name=NAME, code="from shg_addon.pixel_calibration import execute\nexecute(globals())",
                description="Calibrate microscope pixels versus physical X/Y using a selected flake feature (moves stage)",
                confirm=False)


def local_path(x, y, step=1., count=3):
    if not np.isfinite([x, y, step, count]).all() or step <= 0 or int(count) != count or not 1 <= count <= 30:
        raise ValueError("Use a positive stage step and 1..30 steps each side")
    points = [[float(x), float(y)]]
    for axis in range(2):
        for offset in (list(range(1, int(count)+1)) + list(range(int(count)-1, -int(count)-1, -1)) + list(range(-int(count)+1, 1))):
            point = [float(x), float(y)]
            point[axis] += offset*float(step)
            points.append(point)
    return validate_path(points).tolist()


def matlab_path():
    return [[30,30],[35,30],[40,30],[45,30],[40,30],[35,30],[30,30],
            [25,30],[20,30],[15,30],[20,30],[25,30],[30,30],[30,35],
            [30,40],[30,45],[30,40],[30,35],[30,30],[30,25],[30,20],[30,15]]


def defaults():
    saved = tracking.settings()
    return dict(positions=local_path(float(np.clip(saved["base_x"], 3, 97)), float(np.clip(saved["base_y"], 3, 97))),
                settle=.75, radius=300., minimum_score=.35, max_rms=3.,
                save_frames=True, return_to_first=True)


def validate_path(positions):
    a = np.asarray(positions, dtype=float)
    if a.ndim != 2 or a.shape[1] != 2 or not 3 <= len(a) <= 1000 or not np.isfinite(a).all():
        raise ValueError("Enter 3..1000 finite [X Y] positions")
    if np.any(a < 0) or np.any(a > 100):
        raise ValueError("All X/Y positions must be within 0..100 stage alias units")
    delta = a-a[0]
    if np.linalg.matrix_rank(delta) < 2 or np.linalg.cond(delta) > 1e8:
        raise ValueError("The path must contain independent, well-conditioned X and Y moves")
    return a


def prepared_path(cfg):
    points = validate_path(cfg["positions"])
    if cfg.get("return_to_first", True) and not np.array_equal(points[-1], points[0]):
        points = np.vstack([points, points[0]])
    numbers = np.asarray([cfg[k] for k in ("settle", "radius", "minimum_score", "max_rms")], float)
    settle, radius, score, rms = numbers
    if not np.isfinite(numbers).all() or settle < 0 or radius < 1 or not 0 < score <= 1 or rms <= 0:
        raise ValueError("Check settle time >=0, radius >=1, score in (0,1], and positive RMS limit")
    return points


def fit_matrix(stage_positions, pixel_positions):
    stage = validate_path(stage_positions)
    pixels = np.asarray(pixel_positions, float)
    if pixels.shape != stage.shape or not np.isfinite(pixels).all():
        raise ValueError("Every stage position needs a finite tracked [u v] position")
    delta = stage-stage[0]
    shift = pixels-pixels[0]
    fitted, _, rank, _ = np.linalg.lstsq(delta, shift, rcond=None)
    matrix = fitted.T
    if rank != 2 or not np.isfinite(matrix).all() or np.linalg.cond(matrix) > 1e8:
        raise ValueError("Measured pixel matrix is singular/unusable; track a stage-attached feature with independent motion on both axes")
    predicted = delta @ fitted
    residual = shift-predicted
    returns = np.all(np.isclose(delta, 0, atol=1e-12, rtol=0), axis=1)
    return dict(matrix=matrix, stage_delta=delta, pixel_shift=shift,
                predicted_shift=predicted, residual=residual,
                rms=np.sqrt(np.mean(residual**2, axis=0)),
                condition=float(np.linalg.cond(matrix)),
                max_return_error=float(np.max(np.linalg.norm(shift[returns], axis=1))))


def centered_crop(frame, position, size):
    w, h = size
    x, y = np.floor(np.asarray(position)-((np.asarray(size)-1)/2)+.5).astype(int)
    r = (int(np.clip(x, 0, frame.shape[1]-w)), int(np.clip(y, 0, frame.shape[0]-h)), w, h)
    return tracking.crop(frame, r)


def measure(ns, cfg):
    points = prepared_path(cfg)
    tracking.require_microscope(ns)
    size = np.asarray(ns["get"]("microscope.sensor_size"), int)
    exposure = float(ns["get"]("microscope.exposure"))
    if size.shape != (2,) or np.any(size < 8) or not np.isfinite(exposure) or not 0 < exposure <= 4:
        raise ValueError("Check microscope resolution and set exposure to 4 seconds or less")
    # Presence/state checks use the namespace's registered handle, not I/O.
    if ns["instrument"]("stage").state != "connected":
        raise ValueError("Connect the X/Y stage before calibration")
    root = tracking.DATA / ("pixel_calibration_"+datetime.now().strftime("%Y%m%d_%H%M%S_%f")+"_"+uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    meta = dict(format="easyMeasure-pixel-xy-calibration-1", status="running", config=cfg,
                camera_size=size.tolist(), unit="physical X/Y alias units", created=datetime.now().isoformat(),
                convention="[du;dv] = matrix * [dX;dY]")
    pixels, scores, first, template, selected_roi = [], [], None, None, None
    def checkpoint(status):
        meta.update(status=status, completed=len(pixels), total=len(points))
        tracking.write_json(root/"run.json", meta)
        tracking._save_npz(root/"calibration.npz", planned_stage=points,
                           stage_positions=points[:len(pixels)],
                           pixel_positions=np.asarray(pixels).reshape(-1, 2), scores=np.asarray(scores),
                           camera_size=size, complete=np.array(status=="complete"))
    checkpoint("running")
    try:
        from PIL import Image
        for i, point in enumerate(points):
            ns["check"]()
            ns["set_many"]({"X":float(point[0]), "Y":float(point[1])})
            ns["sleep"](float(cfg["settle"]))
            frame = tracking.fresh_frame(ns)
            if [frame.shape[1], frame.shape[0]] != size.tolist():
                raise ValueError("Camera resolution changed during X/Y calibration")
            if first is None:
                first = frame
                selection = tracking.dialog(ns, "roi", dict(mode="flake", config=tracking.settings()), first)
                if selection is None:
                    checkpoint("cancelled")
                    ns["log"]("Pixel/X/Y calibration cancelled at ROI selection; stage remains at first point %s", point)
                    return None
                selected_roi = tracking.roi(selection["roi"], frame.shape)
                template = tracking.gray(tracking.crop(frame, selected_roi)).copy()
                if np.std(template) <= np.finfo(float).eps:
                    raise ValueError("Select a distinctive flake feature with usable intensity variation")
                x, y, w, h = selected_roi
                position, score = np.array([x+(w-1)/2, y+(h-1)/2]), np.nan
                meta["roi"] = list(selected_roi)
            else:
                position, score = tracking.track_template(frame, template, pixels[-1], float(cfg["radius"]))
                if not np.isfinite(score) or score < cfg["minimum_score"]:
                    raise ValueError("Flake tracking lost at X=%.4f, Y=%.4f: correlation %.4f below %.4f" %
                                     (*point, score, cfg["minimum_score"]))
            pixels.append(np.asarray(position)); scores.append(score)
            checkpoint("running")
            if cfg.get("save_frames", True):
                for folder_name, image in (("images", frame), ("tracked_crops", centered_crop(frame, position, selected_roi[2:]))):
                    folder = root/folder_name; folder.mkdir(exist_ok=True)
                    Image.fromarray(np.clip(image, 0, 255).astype(np.uint8)).save(folder/("%04d_X%.5f_Y%.5f.png" % (i, *point)))
            ns["log"]("Pixel/X/Y %d/%d: stage [%.4f, %.4f], feature [%.3f, %.3f], correlation %s",
                        i+1, len(points), *point, *position, "reference" if not i else "%.4f" % score)
        result = fit_matrix(points, pixels)
        checkpoint("complete")
        tracking._save_npz(root/"fit.npz", **result, stage_positions=points,
                           pixel_positions=np.asarray(pixels), camera_size=size, scores=np.asarray(scores))
        with (root/"calibration.csv").open("x", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(["X", "Y", "u_px", "v_px", "du_px", "dv_px", "fitted_du_px", "fitted_dv_px", "residual_u_px", "residual_v_px", "correlation"])
            writer.writerows(np.column_stack([points, pixels, result["pixel_shift"], result["predicted_shift"], result["residual"], scores]))
        meta.update(matrix=result["matrix"].tolist(), rms_px=result["rms"].tolist(),
                    condition=result["condition"], max_return_error_px=result["max_return_error"])
        tracking.write_json(root/"run.json", meta)
    except BaseException as exc:
        meta["error"] = str(exc)
        checkpoint("incomplete")
        raise
    ns["log"]("Pixel/X/Y matrix %s; RMS %s pixels; saved %s", result["matrix"].tolist(), result["rms"].tolist(), root)
    response = tracking.dialog(ns, "pixel_calibration_result", dict(fit=str(root/"fit.npz"), max_rms=cfg["max_rms"]))
    if response and response.get("use"):
        if np.any(result["rms"] > cfg["max_rms"]):
            raise ValueError("Fit RMS exceeds the chosen limit; calibration was saved for review but not applied")
        config = tracking.settings()
        # Preserve other tracking choices and existing native run paths.
        config.update(pixel_per_piezo=result["matrix"].tolist(), calibration_size=size.tolist(),
                      pixel_calibration_file=str(root/"fit.npz"))
        tracking.write_json(tracking.HOME/"settings.json", config)
        ns["log"]("New %d x %d pixel/X/Y calibration saved for future Combine dialogs. Existing combined tables are unchanged.", *size)
    return root/"fit.npz"


def execute(ns):
    if (tracking.HOME/"disabled.json").exists():
        raise ValueError("SHG tracking/calibration was rolled back; reinstall before use")
    if not tracking.LOCK.acquire(blocking=False):
        raise ValueError("Close the running tracking/Combine dialog or finish its action before X/Y calibration")
    try:
        tracking.require_microscope(ns)
        cfg = tracking.dialog(ns, "pixel_calibration_configure", dict(config=defaults()))
        if cfg is None:
            ns["log"]("Pixel/X/Y calibration cancelled before hardware commands")
            return
        prepared_path(cfg)
        return measure(ns, cfg)
    finally:
        tracking.LOCK.release()
