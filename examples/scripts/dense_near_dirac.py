# Sweep the back gate with points 0.2 V apart, but 0.02 V apart within 1 V of
# the Dirac point: a non-uniform sweep, built in Python and run as an ordinary sweep.
v_dirac = 0.3
coarse = np.arange(-8, 8.001, 0.2)
fine = np.arange(v_dirac - 1, v_dirac + 1.0001, 0.02)
values = np.unique(np.round(np.concatenate([coarse, fine]), 6))   # sorted, no duplicates

ds = run_sweep({
    "name": "dirac_dense",
    "dimensions": [{"points": len(values),
                    "sweep": [{"channel": "Vbg", "values": values.tolist()}],
                    "record": ["Rxx"]}],
    "post": {"constants": [{"channel": "Vbg", "value": 0}]},
})
log("%s: %d points, file %s" % (ds.status, len(values), ds.path))
