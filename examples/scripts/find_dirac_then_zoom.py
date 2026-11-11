# An adaptive measurement: a quick coarse sweep finds the Dirac point, then a slow
# fine sweep measures ±0.5 V around it.  run_sweep() returns the run's data, so
# the script can decide what to measure next.
coarse = run_sweep({
    "name": "dirac_coarse",
    "dimensions": [{"points": 81, "sweep": [{"channel": "Vbg", "start": -8, "stop": 8}],
                    "record": ["Rxx"]}],
})
vbg = coarse.setpoints[0]["Vbg"]
r = coarse.data["Rxx"]
v0 = float(vbg[np.nanargmax(r)])
log("coarse sweep: Dirac point near Vbg = %.2f V (Rxx = %.0f Ω)" % (v0, np.nanmax(r)))
state["dirac_point"] = v0                  # remembered for later scripts and actions

fine = run_sweep({
    "name": "dirac_fine",
    "dimensions": [{"points": 101,
                    "sweep": [{"channel": "Vbg", "start": v0 - 0.5, "stop": v0 + 0.5}],
                    "record": ["Rxx"], "pre_delay": 0.05}],
    "post": {"constants": [{"channel": "Vbg", "value": 0}]},
    "metadata": {"found_from": coarse.path},
})
log("fine sweep %s: peak Rxx = %.0f Ω" % (fine.status, np.nanmax(fine.data["Rxx"])))
