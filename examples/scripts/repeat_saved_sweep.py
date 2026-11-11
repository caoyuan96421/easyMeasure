# Repeat a sweep designed in the sweep editor (and saved there with "Save…") at
# several top-gate voltages.  The Dirac peak moves and grows: the top gate adds
# carriers and a displacement field.
base = load_sweep("examples/graphene_dirac.json")    # a full path works too
for vtg in (-2, -1, 0, 1, 2):
    set("Vtg", vtg)
    sweep = dict(base, name="dirac_at_Vtg_%+g" % vtg, metadata={"Vtg": vtg})
    ds = run_sweep(sweep)
    v = ds.setpoints[0]["Vbg"]
    peak = int(np.nanargmax(ds.data["Rxx"]))
    log("Vtg = %+g V: peak %.0f Ω at Vbg = %.2f V" % (vtg, ds.data["Rxx"][peak], v[peak]))
set("Vtg", 0)
