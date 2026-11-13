"""Contracts between this integration and the python-aidot-cameras library.

Two of the worst outages in this fleet's history were cross-repo: the library
and the integration each behaved correctly on their own, and the combination
did not. Neither repo's tests could see it - the library has no integration,
and the integration only ever tests RELEASED library versions.

These are the assertions that make the coupling explicit here, in the repo
that depends on it. The library side runs this same suite against unreleased
library code (its `downstream-integration` CI job), so a break is caught
before the release exists.
"""

import ast
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENT = os.path.join(REPO, "custom_components", "aidot")
MANIFEST = os.path.join(COMPONENT, "manifest.json")


def _source(name: str) -> str:
    with open(os.path.join(COMPONENT, name)) as fh:
        return fh.read()


def test_go2rtc_url_is_never_passed_to_start_keepalive():
    """Passing go2rtc_url switches on the library's own registration.

    The library then PUTs the same stream this integration already registers,
    re-pointing the source mid-flight. On this fleet that left every DTLS
    camera producing nothing while the build without it served all four.
    The reason to pass it (asking go2rtc who is watching) no longer applies.
    """
    tree = ast.parse(_source("camera.py"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name != "start_keepalive":
            continue
        for kw in node.keywords:
            if kw.arg == "go2rtc_url":
                offenders.append(node.lineno)
    assert not offenders, (
        "camera.py passes go2rtc_url to start_keepalive at line(s) "
        f"{offenders}. That enables the library's _register_with_go2rtc(), "
        "which duplicates this integration's own registration and killed "
        "video on the DTLS cameras."
    )


def test_manifest_requires_the_library_with_the_webrtc_extra():
    with open(MANIFEST) as fh:
        manifest = json.load(fh)
    reqs = manifest.get("requirements") or []
    lib = [r for r in reqs if "python-aidot-cameras" in r]
    assert lib, f"manifest must require python-aidot-cameras; got {reqs}"
    assert len(lib) == 1, f"exactly one library requirement expected; got {lib}"
    req = lib[0]
    assert "[webrtc]" in req, (
        f"the [webrtc] extra is required for live streaming; got {req!r}"
    )
    assert ">=" in req, (
        f"the library requirement must carry a version floor; got {req!r}. "
        "Without one, a user can install a library too old for this release."
    )


def test_manifest_caps_the_library_below_its_next_minor():
    """The requirement must carry an upper bound below the library's next minor
    version. A floor alone, when it names a pre-release, lets pip pick any newer
    pre-release, so an installed stable release could be given a library beta
    nobody tested it with. ``<X.Y`` also excludes X.Y's own betas (PEP 440)."""
    with open(MANIFEST) as fh:
        manifest = json.load(fh)
    req = next(r for r in manifest["requirements"] if "python-aidot-cameras" in r)
    floor = re.search(r">=\s*([0-9]+)\.([0-9]+)", req)
    cap = re.search(r"<\s*([0-9]+)\.([0-9]+)(?![.0-9])", req)
    assert floor and cap, f"the requirement needs >=X.Y.Z and <X.Y; got {req!r}"
    nxt = (int(floor.group(1)), int(floor.group(2)) + 1)
    assert (int(cap.group(1)), int(cap.group(2))) == nxt, (
        f"cap the library at its next minor version, <{nxt[0]}.{nxt[1]}; got {req!r}"
    )


def test_manifest_library_floor_is_a_valid_version():
    with open(MANIFEST) as fh:
        manifest = json.load(fh)
    req = next(r for r in manifest["requirements"] if "python-aidot-cameras" in r)
    m = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", req)
    assert m, f"could not parse a version floor out of {req!r}"
    parts = m.group(1).split(".")
    assert len(parts) >= 3, f"floor should be a full X.Y.Z; got {m.group(1)}"
    assert all(p.isdigit() for p in parts), m.group(1)


def _optional_imports(tree) -> set:
    """ImportFrom nodes inside a ``try`` whose handlers catch ImportError (or
    ModuleNotFoundError) - an import the code already treats as optional."""
    found: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches = set()
        for h in node.handlers:
            types = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
            catches |= {t.id for t in types if isinstance(t, ast.Name)}
        if catches & {"ImportError", "ModuleNotFoundError"}:
            for stmt in node.body:
                found |= {n for n in ast.walk(stmt) if isinstance(n, ast.ImportFrom)}
    return found


def test_optional_imports_are_only_those_guarded_by_import_error():
    tree = ast.parse(
        "try:\n    from aidot_cameras.x import a\nexcept ImportError:\n    a = None\n"
        "try:\n    from aidot_cameras.y import b\nexcept ValueError:\n    pass\n"
        "from aidot_cameras.z import c\n"
    )
    names = {n.module for n in _optional_imports(tree)}
    assert names == {"aidot_cameras.x"}


def test_every_library_symbol_the_integration_imports_exists():
    """Mirror of the library's own upstream-compat test, pointing downstream.

    A library refactor that moves or renames a symbol this integration imports
    must fail HERE, by name, rather than at runtime in someone's Home
    Assistant.
    """
    import importlib

    missing = []
    for fname in sorted(os.listdir(COMPONENT)):
        if not fname.endswith(".py"):
            continue
        try:
            tree = ast.parse(_source(fname))
        except SyntaxError as exc:
            # The component targets py312+ (PEP 695 `type X = ...`), so this
            # only trips on an older interpreter - or on genuinely broken
            # source, which is worth failing for either way.
            missing.append(f"{fname}: does not parse ({exc})")
            continue
        optional = _optional_imports(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith(("aidot", "aidot_cameras")):
                continue
            if node in optional:
                # Guarded by `except ImportError`: written to run against a
                # library that lacks it, so its absence is not a break.
                continue
            try:
                mod = importlib.import_module(node.module)
            except Exception as exc:
                missing.append(f"{fname}: cannot import {node.module} ({exc})")
                continue
            for alias in node.names:
                if alias.name == "*" or hasattr(mod, alias.name):
                    continue
                # ``from pkg import submodule`` imports the submodule; it is an
                # attribute of the package only once something has imported
                # it, so this passed or failed by test order.
                try:
                    importlib.import_module(f"{node.module}.{alias.name}")
                except ImportError:
                    missing.append(f"{fname}: {node.module}.{alias.name} is gone")
    assert not missing, (
        "library API the integration depends on has moved:\n" + "\n".join(missing)
    )


def test_camera_entity_uses_a_supported_media_signal():
    """SDES sessions never call on_frame; don't gate liveness on frames alone.

    The library's SDES path hands media to ffmpeg and never invokes on_frame,
    so any "is it streaming?" check written against frame callbacks silently
    reports dead for every battery/PTZ camera.
    """
    src = _source("camera.py")
    # If the integration ever grows a frame-count liveness check, it must also
    # consult a transport-agnostic signal.
    if re.search(r"\bon_frame\b.*\bcount\b|\bframe_count\b", src):
        assert "media_stats" in src or "last_media_monotonic" in src, (
            "camera.py appears to gate liveness on frame counts; SDES sessions "
            "never call on_frame, so it must also use media_stats() / "
            "last_media_monotonic"
        )
