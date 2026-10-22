import re


def todo_for(text: str, kind: str, name: str) -> str:
    """The `# TODO(unshackle): <kind> <name>: ...` line for an item unshackle left commented out, or ''."""
    m = re.search(rf"^# TODO\(unshackle\): {kind} (?:[\w.]*\.)?{re.escape(name)}: (.*)$", text, re.M)
    return m.group(0) if m else ""


def notebook_source(path) -> str:
    """Validate a generated .ipynb against the nbformat schema, check every code cell compiles,
    and return all cell sources joined (what a person sees in Jupyter)."""
    import nbformat

    nb = nbformat.read(str(path), as_version=4)
    nbformat.validate(nb)
    assert all(not c.get("outputs") for c in nb.cells if c.cell_type == "code"), "outputs must not be stored"
    for c in nb.cells:
        if c.cell_type == "code":
            compile(c.source, f"{path}:{c.id}", "exec")
    return "\n\n".join(c.source for c in nb.cells)
