"""Merge the top two layers of an OCI image into a single uncompressed tar layer.

Semantics implemented here follow the OCI image layer spec
(https://github.com/opencontainers/image-spec/blob/main/layer.md):

* regular files and directories are applied in order;
* a plain whiteout ``.wh.<name>`` deletes ``<name>`` (file or dir subtree)
  from the view of lower layers;
* an opaque whiteout ``.wh..wh..opq`` inside directory ``d`` hides everything
  that lower layers contributed under ``d``, while entries that the *same*
  layer adds under ``d`` remain visible.

Directory replacement rules (the reason whiteouts and real entries live in
separate stores):

* a whiteout only hides entries contributed by *lower* layers.  Real entries
  added by the same layer — no matter whether they appear before or after the
  whiteout marker in the tar stream — survive, and the marker is kept so the
  deletion keeps applying to the base layers below the merged layer;
* when an upper layer rebuilds as a *directory* a path that a lower layer had
  deleted (whiteout) or replaced by a regular file, the lower directory's
  children must stay hidden.  The existing whiteout is retained, or a plain
  whiteout is synthesized when the lower entry was a regular file (a file
  carries no whiteout of its own);
* a plain directory overlay (lower layer has a directory at the same path)
  stays a union: lower children that the upper layer does not touch remain
  visible;
* a regular file at a path still replaces the whole subtree at that path.

Only regular files, directories, plain whiteouts and opaque whiteouts are
supported; anything else (symlinks, hardlinks, devices, ...) raises
:class:`MergeError` so the caller can refuse the merge instead of silently
producing a wrong layer.

Resource contract
-----------------
* File contents are never loaded into memory: phase 1 reads tar headers only,
  phase 2 streams file data from the source blobs into the new layer through
  a small fixed buffer.  Memory usage is O(number of entries), independent of
  file sizes.
* Nothing but the new layer blob is written to disk.  Synthesized whiteouts
  are zero-byte metadata entries, so they add no file data.
"""

from __future__ import annotations

import tarfile
from dataclasses import dataclass

WHITEOUT_PREFIX = ".wh."
OPAQUE_WHITEOUT = ".wh..wh..opq"

# Pseudo source index for entries synthesized by the merger itself (no source
# tar backs them; they are zero-size and written from metadata alone).
SYNTHETIC_SOURCE = -1


class MergeError(Exception):
    """Raised when a layer cannot be merged (unsupported or invalid input)."""


@dataclass
class _Item:
    """A surviving tar entry plus the layer it came from."""

    tarinfo: tarfile.TarInfo
    source: int  # index into the list of source TarFiles (0 = lower, 1 = upper)


def _normalize(name: str) -> str | None:
    """Normalize a tar entry name to a clean relative path.

    Returns ``None`` for the root entry itself.  Raises :class:`MergeError`
    for absolute paths or paths that escape the root.
    """
    # Tar names use '/' separators; collapse redundant parts.
    parts: list[str] = []
    for comp in name.split("/"):
        if comp in ("", "."):
            continue
        if comp == "..":
            raise MergeError(f"tar entry escapes root: {name!r}")
        parts.append(comp)
    if not parts:
        return None
    return "/".join(parts)


def _classify(ti: tarfile.TarInfo) -> tuple[str, str]:
    """Classify a tar entry.

    Returns ``(kind, key)`` where kind is one of ``file``, ``dir``,
    ``whiteout``, ``opaque`` and key is the normalized path the entry
    applies to (for whiteouts: the *target* path; for opaque markers: the
    directory that becomes opaque; otherwise the entry's own path).
    """
    norm = _normalize(ti.name)
    if norm is None:
        return "root", ""

    parent, _, base = norm.rpartition("/")
    if base == OPAQUE_WHITEOUT:
        return "opaque", parent
    if base.startswith(WHITEOUT_PREFIX):
        target_base = base[len(WHITEOUT_PREFIX):]
        if not target_base:
            raise MergeError(f"invalid whiteout entry: {ti.name!r}")
        target = f"{parent}/{target_base}" if parent else target_base
        return "whiteout", target

    if ti.isdir():
        return "dir", norm
    if ti.isreg():
        return "file", norm
    raise MergeError(
        f"unsupported tar entry type {ti.type!r} for {ti.name!r}: "
        "only regular files, directories and whiteouts are supported"
    )


def _whiteout_name(target: str) -> str:
    """Tar entry name of the plain whiteout that hides ``target``."""
    parent, _, base = target.rpartition("/")
    return f"{parent}/{WHITEOUT_PREFIX}{base}" if parent else WHITEOUT_PREFIX + base


def _sort_key(name: str) -> list[tuple[int, str]]:
    """Ordering key for the merged tar stream.

    Requirements, in order:

    * parent entries sort before their children;
    * a plain whiteout ``.wh.<x>`` sorts before the entry/subtree it hides
      (``<x>`` itself), even though its tar name sorts after it — a strictly
      sequential applier must see the deletion before the re-creation;
    * an opaque marker sorts with the whiteouts, just before the directory's
      other children.
    """
    key: list[tuple[int, str]] = []
    for comp in name.split("/"):
        if comp == OPAQUE_WHITEOUT:
            key.append((0, ""))  # first child of its directory
        elif comp.startswith(WHITEOUT_PREFIX):
            key.append((0, comp[len(WHITEOUT_PREFIX):]))
        else:
            key.append((1, comp))
    return key


def _make_whiteout_tarinfo(target: str) -> tarfile.TarInfo:
    """Build a zero-byte plain-whiteout header for a synthesized deletion."""
    ti = tarfile.TarInfo(_whiteout_name(target))
    ti.size = 0
    ti.mode, ti.uid, ti.gid, ti.mtime = 0o644, 0, 0, 0
    ti.uname, ti.gname = "root", "root"
    return ti


class LayerMerger:
    """Accumulates two layers and writes out their merged equivalent.

    ``synthesize_implicit_dirs``: when the two merged layers are the *whole*
    image (no base layers), directories that were only created implicitly by
    tar extraction (a file needed its parent) and whose contents were all
    deleted/masked again would silently vanish from the merged result.  With
    no base layers we have complete information, so explicit directory
    entries (0755 root:root, matching applier defaults) are synthesized for
    those.  With base layers present this is never done: the base may hold
    the directory with its own metadata, which must not be clobbered.
    """

    def __init__(self, synthesize_implicit_dirs: bool = False) -> None:
        # real (file/dir) entries, keyed by normalized path
        self._real: dict[str, _Item] = {}
        # plain whiteouts, keyed by normalized target path
        self._whiteouts: dict[str, _Item] = {}
        # opaque markers, keyed by normalized directory path
        self._opaque: dict[str, _Item] = {}
        # directories that exist only implicitly (no explicit dir entry),
        # mapped to the layer index that first implied them
        self._implicit: dict[str, int] = {}
        self._synthesize = synthesize_implicit_dirs

    # -- state mutation helpers -------------------------------------------

    def _iter_stores(self):
        yield self._real
        yield self._whiteouts
        yield self._opaque
        # _implicit maps path -> source int rather than path -> _Item
        yield self._implicit

    @staticmethod
    def _entry_source(store, key) -> int:
        value = store[key]
        # _implicit maps path -> source int; the other stores hold _Item
        return value if isinstance(value, int) else value.source

    def _drop_subtree(self, path: str) -> None:
        """Forget everything at ``path`` and below, from every layer.

        Used when a regular file takes over a path: a file cannot have
        children, and its creation replaces whatever (including any lower
        directory subtree or whiteout) used to live there.
        """
        prefix = path + "/"
        for store in self._iter_stores():
            store.pop(path, None)
            for key in [k for k in store if k.startswith(prefix)]:
                del store[key]

    def _prune_lower(self, path: str, source: int) -> None:
        """Forget entries at ``path`` and below that came from lower layers.

        Entries contributed by ``source`` itself (or a higher layer already
        folded in) are kept: a whiteout/opaque marker hides *lower* content
        only, so same-layer re-creations survive regardless of where the
        marker sits in the tar stream.
        """
        prefix = path + "/"
        for store in self._iter_stores():
            for key in [k for k in store if k == path or k.startswith(prefix)]:
                if self._entry_source(store, key) < source:
                    del store[key]

    def _ensure_lower_subtree_hidden(self, path: str) -> None:
        """Make sure a rebuilt directory ``path`` still hides lower children.

        A surviving plain whiteout or opaque marker at ``path`` already does
        the job; otherwise synthesize a plain whiteout (zero-byte metadata
        only) so base-layer children cannot show through the rebuilt dir.
        """
        if path in self._whiteouts or path in self._opaque:
            return
        self._whiteouts[path] = _Item(
            tarinfo=_make_whiteout_tarinfo(path), source=SYNTHETIC_SOURCE
        )

    def _put_file(self, path: str, item: _Item) -> None:
        # A file replaces anything that used to live at/below this path
        # (e.g. a directory subtree, a whiteout on this path, ...).  The
        # replacement itself hides whatever lower layers held here, so no
        # whiteout is needed for the path.
        self._drop_subtree(path)
        self._real[path] = item

    def _put_dir(self, path: str, item: _Item) -> None:
        previous = self._real.get(path)
        if previous is not None and previous.tarinfo.isreg():
            # A (lower- or same-layer) regular file used to occupy this path
            # and had destroyed the lower directory subtree.  Rebuilding a
            # directory here must not let the old lower children reappear.
            self._ensure_lower_subtree_hidden(path)
        # An explicit dir entry: no longer merely implicit.
        self._implicit.pop(path, None)
        # Lower children are intentionally NOT pruned: a plain directory
        # overlay is a union and untouched lower entries stay visible.
        self._real[path] = item

    def _put_whiteout(self, target: str, item: _Item, source: int) -> None:
        # The whiteout deletes the target subtree as contributed by *lower*
        # layers.  Same-layer entries survive even when the marker is placed
        # after them in the tar stream.
        self._prune_lower(target, source)
        current = self._real.get(target)
        if current is not None and current.tarinfo.isreg():
            # A regular file at the target already replaces everything lower
            # layers held there; a whiteout next to it would be redundant.
            return
        # Keep the whiteout (also alongside a same/upper-layer real directory
        # entry): the deletion must keep applying to the base layers below
        # the merged layer, while the rebuilt directory itself stays visible.
        self._whiteouts[target] = item

    def _put_opaque(self, directory: str, item: _Item, source: int) -> None:
        # The opaque marker hides entries strictly below ``directory`` that
        # came from *lower* layers.  Entries from the same layer (no matter
        # whether they appear before or after the marker in the tar stream)
        # stay visible.
        prefix = directory + "/" if directory else ""
        for store in self._iter_stores():
            for key in [k for k in store if k != directory and k.startswith(prefix)]:
                if self._entry_source(store, key) < source:
                    del store[key]
        self._opaque[directory] = item

    def _note_implicit_parents(self, path: str, source: int) -> None:
        """Record ancestors of a real entry that have no explicit dir entry."""
        comps = path.split("/")
        for i in range(1, len(comps)):
            ancestor = "/".join(comps[:i])
            if ancestor not in self._real and ancestor not in self._implicit:
                self._implicit[ancestor] = source

    # -- public API --------------------------------------------------------

    def add_layer(self, tf: tarfile.TarFile, source: int) -> None:
        """Scan a layer tar (headers only) and fold it into the merge state."""
        for ti in tf:
            kind, key = _classify(ti)
            if kind == "root":
                continue
            # Normalize the stored name so the merged layer is clean and
            # deterministic; scrub stale pax overrides of the old name.
            if kind in ("file", "dir"):
                ti.name = key
            elif kind == "whiteout":
                ti.name = _whiteout_name(key)
            else:  # opaque
                ti.name = f"{key}/{OPAQUE_WHITEOUT}" if key else OPAQUE_WHITEOUT
            ti.pax_headers.pop("path", None)
            ti.pax_headers.pop("linkpath", None)

            item = _Item(tarinfo=ti, source=source)
            if kind == "file":
                self._put_file(key, item)
                self._note_implicit_parents(key, source)
            elif kind == "dir":
                self._put_dir(key, item)
                self._note_implicit_parents(key, source)
            elif kind == "whiteout":
                self._put_whiteout(key, item, source)
            else:
                self._put_opaque(key, item, source)

    def _synthesized_dir_items(self) -> list[_Item]:
        """Explicit dir entries for implicit dirs that nothing else creates.

        A surviving real entry or opaque marker re-creates its ancestors
        during extraction; a whiteout does not.  Any implicit dir not covered
        by those would be lost, so materialize it with applier defaults.
        """
        creators: list[str] = list(self._real.keys())
        creators.extend(self._opaque.keys())  # opaque dir materializes itself

        synthesized: list[_Item] = []
        for imp in sorted(self._implicit):
            prefix = imp + "/"
            if any(c == imp or c.startswith(prefix) for c in creators):
                continue
            ti = tarfile.TarInfo(imp)
            ti.type = tarfile.DIRTYPE
            ti.mode, ti.uid, ti.gid, ti.mtime = 0o755, 0, 0, 0
            ti.uname, ti.gname = "root", "root"
            synthesized.append(_Item(tarinfo=ti, source=SYNTHETIC_SOURCE))
        return synthesized

    def write_merged(self, sources: list[tarfile.TarFile | None], fileobj) -> int:
        """Stream the merged layer as an uncompressed tar into ``fileobj``.

        ``sources`` are the still-open source tars (index matches the
        ``source`` argument of :meth:`add_layer`); an entry is ``None`` for
        an empty source layer, which no surviving item can reference.
        Returns the number of entries written.
        """
        items = (
            list(self._real.values())
            + list(self._whiteouts.values())
            + list(self._opaque.values())
        )
        if self._synthesize:
            items += self._synthesized_dir_items()
        # Parents sort before their children; whiteouts sort before the
        # subtree they delete (see _sort_key), which is required for correct
        # application by a strictly sequential tar extractor.
        items.sort(key=lambda it: _sort_key(it.tarinfo.name))

        count = 0
        with tarfile.open(fileobj=fileobj, mode="w", format=tarfile.PAX_FORMAT) as out:
            for item in items:
                ti = item.tarinfo
                if item.source == SYNTHETIC_SOURCE:
                    # Synthesized dirs/whiteouts carry no source data (they
                    # are zero-size); write them from metadata alone.
                    out.addfile(ti)
                elif ti.isreg():
                    src_tf = sources[item.source]
                    if src_tf is None:  # pragma: no cover - defensive
                        raise MergeError(f"entry {ti.name!r} refers to an empty layer")
                    src = src_tf.extractfile(ti)
                    if src is None:  # pragma: no cover - defensive
                        raise MergeError(f"cannot read data for {ti.name!r}")
                    with src:
                        out.addfile(ti, src)
                else:
                    out.addfile(ti)
                count += 1
        return count
