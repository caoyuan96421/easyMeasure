# A sequence: one gate sweep at each of a list of fields, each in its own file
# with the field in its metadata.  The fields need not be evenly spaced.
fields = [0, 0.5, 1, 2, 4, 6, 9]
for b in fields:
    set("B", b)                                   # the magnet starts ramping
    wait_until(lambda: get("magnet.status") == "HOLDING", timeout=1200)
    sleep(1)                                      # let things settle
    ds = run_sweep({
        "name": "gate_at_%gT" % b,
        "dimensions": [{"points": 101, "snake": False,
                        "sweep": [{"channel": "Vbg", "start": -8, "stop": 8}],
                        "record": ["Rxx"]}],
        "metadata": {"B": b},
    })
    log("B = %g T: %s" % (b, ds.status))
    if ds.status != "finished":
        break                                     # aborted or failed: stop the series
set("B", 0)
set("Vbg", 0)
