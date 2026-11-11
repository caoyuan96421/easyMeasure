# A path that is not a straight line: circles around the charge-neutrality point in
# the (n, D) plane, at three radii.  n and D are swept together in one dimension,
# each from its own list of values, so every point is one (n, D) pair.
theta = np.linspace(0, 2 * np.pi, 73)            # 5° steps, back to the start
for radius in (0.5, 1.0, 1.5):
    n_values = radius * np.cos(theta)            # 1e12 cm^-2
    d_values = 0.2 * radius * np.sin(theta)      # V/nm: an ellipse, D is the smaller axis
    ds = run_sweep({
        "name": "circle_r%g" % radius,
        "dimensions": [{"points": len(theta),
                        "sweep": [{"channel": "n", "values": n_values.tolist()},
                                  {"channel": "D", "values": d_values.tolist()}],
                        "record": ["Rxx", "Vbg", "Vtg"]}],
        "metadata": {"radius": radius},
    })
    log("radius %g: %s, Rxx from %.0f to %.0f Ω" % (
        radius, ds.status, np.nanmin(ds.data["Rxx"]), np.nanmax(ds.data["Rxx"])))
set_many({"n": 0, "D": 0})
