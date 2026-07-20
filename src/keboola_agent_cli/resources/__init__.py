"""Bundled static resources shipped inside the kbagent wheel.

Data files live in subpackages (e.g. ``resources.flow``) and are read via
``importlib.resources`` so they resolve identically from a source checkout,
an installed wheel, or a zipapp. Never read them with raw filesystem paths.
"""
