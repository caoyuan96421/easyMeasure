"""Classify loaded hardware, independently of the setup-selection checkbox."""
def mode(instruments):
    rows={i['name']:i for i in instruments}
    required=['camera','stage','rotator']
    if any(n not in rows or rows[n].get('state')!='connected' for n in required):
        return 'incomplete','NOT READY — connect camera, stage and rotator'
    simulated=[]
    for name in required:
        i=rows[name];address=str(i.get('address','')).upper()
        simulated.append(bool(i.get('options',{}).get('simulate')) or address=='DAQ::SIM' or address.startswith('SIM::'))
    if all(simulated):return 'simulation','SIMULATION — SYNTHETIC DATA; no real camera or mirror movement'
    if any(simulated):return 'mixed','MIXED REAL/SIMULATED INSTRUMENTS — acquisition blocked'
    return 'real','REAL INSTRUMENTS — mirror state is commanded, not position feedback'
