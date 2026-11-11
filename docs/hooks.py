"""MkDocs hooks: tidy reST roles left in docstrings (``:class:`Foo``` → ``Foo``)."""

import re

_ROLE = re.compile(r":(?:mod|class|meth|func|data|attr|exc|obj|ref):(?=<code>)")
_ROLE_TEXT = re.compile(r":(?:mod|class|meth|func|data|attr|exc|obj|ref):`~?([^`]+)`")


def on_page_content(html, page, config, files):
    return _ROLE.sub("", html)


def on_page_markdown(markdown, page, config, files):
    return _ROLE_TEXT.sub(r"`\1`", markdown)
