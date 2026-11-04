"""easyMeasure backend: instruments, channels, sweeps, queue, storage.

Typical in-process use::

    from easymeasure.backend.service import Backend
    be = Backend()
    be.load_setup(path="examples/demo_setup.json")
    be.queue_add({"kind": "sweep", "definition": {...}}, start=True)

As a service (what the GUI uses)::

    python -m easymeasure.backend
"""
