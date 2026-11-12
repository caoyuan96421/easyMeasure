# Safety first: find how far the back gate can go (stop at 1 nA of leakage), then
# measure the n–D map only within 90 % of that voltage.
test = run_sweep({
    "name": "leakage_test",
    "dimensions": [{"points": 201, "sweep": [{"channel": "Vbg", "start": 0, "stop": 10}],
                    "record": ["Ileak"],
                    "hooks": {"post_record": {"enabled": True,
                              "code": "if abs(values['Ileak']) > 1e-9: end_sweep()"}}}],
    "post": {"constants": [{"channel": "Vbg", "value": 0}]},
})
measured = np.isfinite(test.data["Ileak"])
v_max = float(test.setpoints[0]["Vbg"][measured][-1])
v_safe = 0.9 * v_max
log("leakage test %s: up to %.2f V, using %.2f V" % (test.status, v_max, v_safe))

# the largest |D| at n = 0 that keeps Vbg = (0.15 + D/0.0452) / 1.2 within v_safe
d_max = min(0.35, (1.2 * v_safe - 0.15) * 0.0452)
ds = run_sweep({
    "name": "n_D_map_safe",
    "dimensions": [
        {"points": 61, "sweep": [{"channel": "nD.n", "start": -1, "stop": 1}],
         "record": ["Rxx"], "snake": True},
        {"points": 15, "sweep": [{"channel": "nD.D", "start": -d_max, "stop": d_max}]}],
    "metadata": {"v_safe": v_safe},
})
log("map %s with |D| ≤ %.3f V/nm" % (ds.status, d_max))
set_many({"Vbg": 0, "Vtg": 0})
