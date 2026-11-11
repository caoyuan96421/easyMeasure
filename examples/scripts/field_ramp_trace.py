# Measure while the magnet ramps continuously, instead of stepping the field: a sweep
# with no swept channel is a time trace.  It records every 0.1 s and ends itself
# when the magnet reports HOLDING.
set("Vbg", 1.1)                                  # n ≈ 0.5e12 cm^-2: Shubnikov-de Haas
set("B", 0)
wait_until(lambda: get("magnet.status") == "HOLDING", timeout=1200)
set("B", 3)                                      # starts the ramp; does not wait
ds = run_sweep({
    "name": "sdh_field_ramp",
    "dimensions": [{"points": 20000, "sweep": [],
                    "record": ["B", "Rxx", "@elapsed"], "post_delay": 0.1,
                    "hooks": {"post_record": {"enabled": True, "code":
                              "if get('magnet.status') == 'HOLDING': end_sweep()"}}}],
})
n = int(np.isfinite(ds.data["B"]).sum())
log("%s after %d readings, B up to %.2f T" % (ds.status, n, np.nanmax(ds.data["B"])))
set("B", 0)
set("Vbg", 0)
