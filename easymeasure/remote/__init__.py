"""Remote access to the backend over the internet: an HTTPS/WSS gateway, the
browser frontend it serves, user accounts and the access policy.

Quick start (on the backend computer)::

    python -m easymeasure.remote adduser alice --role admin
    python -m easymeasure.backend --remote          # backend + gateway on port 8443

then open ``https://<computer>:8443`` in a browser, or from Python::

    from easymeasure.client import RemoteClient
    c = RemoteClient("https://lab-pc:8443", "alice", password, fingerprint="AB:CD:...")

See :mod:`easymeasure.remote.gateway` for the security design and
:mod:`easymeasure.remote.policy` for what each role may do.
"""
