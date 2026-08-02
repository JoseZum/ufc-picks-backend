"""Mission-system vertical module.

Import concrete adapters such as ``router`` explicitly so pure domain and
migration modules never initialize FastAPI settings as a side effect.
"""
