# A hysteresis loop: 0 → +5 V → −5 V → 0 in one sweep, from an explicit list.
up = np.linspace(0, 5, 51)
down = np.linspace(5, -5, 101)[1:]
back = np.linspace(-5, 0, 51)[1:]
values = np.concatenate([up, down, back])
ds = run_sweep({
    "name": "gate_hysteresis",
    "dimensions": [{"points": len(values),
                    "sweep": [{"channel": "Vbg", "values": values.tolist()}],
                    "record": ["Rxx", "@elapsed"]}],
})
log("hysteresis loop: %s" % ds.status)
