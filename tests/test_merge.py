"""Merge-engine tests: build 3-layer images, merge the top two, and prove
that expanding base+merged yields exactly the same tree as base+L1+L2."""

from __future__ import annotations

import tarfile
import tracemalloc
from pathlib import Path

import pytest

from app.merge import LayerMerger, MergeError
from tests.conftest import (
    build_layer_tar,
    dir_entry,
    expand_stack,
    file_entry,
    layer_entry_names,
    opaque_entry,
    whiteout_entry,
)


def merge_layers(tmp_path: Path, l1: Path, l2: Path) -> Path:
    """Run the engine over two layer tars; return the merged tar path."""
    out = tmp_path / "merged.tar"
    with open(l1, "rb") as f1, open(l2, "rb") as f2:
        t1 = tarfile.open(fileobj=f1, mode="r:")
        t2 = tarfile.open(fileobj=f2, mode="r:")
        merger = LayerMerger()
        merger.add_layer(t1, 0)
        merger.add_layer(t2, 1)
        with open(out, "wb") as fout:
            merger.write_merged([t1, t2], fout)
    return out


def check_equivalent(tmp_path: Path, base: Path, l1: Path, l2: Path) -> Path:
    merged = merge_layers(tmp_path, l1, l2)
    old_tree = expand_stack([base, l1, l2])
    new_tree = expand_stack([base, merged])
    new_tree.assert_same_as(old_tree)
    return merged


# ---------------------------------------------------------------------------
# the demo scenario: opaque whiteout masks a directory, then new files are
# added to that same directory in the same layer
# ---------------------------------------------------------------------------


def test_opaque_dir_then_add_files(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("app"), dir_entry("app/data"),
        file_entry("app/data/old.log", b"old log\n"),
        file_entry("app/data/base.txt", b"base\n"),
        dir_entry("etc"),
        file_entry("etc/keep.cfg", b"keep=1\n"),
        file_entry("etc/old.cfg", b"old=1\n"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [
        file_entry("app/data/junk.txt", b"junk\n"),
        dir_entry("app/data/tmpdir"),
        file_entry("app/data/tmpdir/scratch.bin", b"scratch"),
        file_entry("app/server.bin", b"binary-v1", mode=0o755),
        file_entry("etc/old.cfg", b"old=2\n"),  # modifies base file
    ])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        opaque_entry("app/data"),                      # mask base+L1 contents
        file_entry("app/data/fresh.txt", b"fresh\n"),  # ...then add new files
        whiteout_entry("etc/old.cfg"),                 # delete modified file
        file_entry("etc/new.cfg", b"new=1\n"),
        file_entry("app/server.bin", b"binary-v2", mode=0o755),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)

    names = layer_entry_names(merged)
    # masked lower-layer content must not leak into the merged layer
    assert "app/data/junk.txt" not in names
    assert "app/data/tmpdir" not in names
    assert "app/data/old.log" not in names
    # the opaque marker and the new file must survive
    assert "app/data/.wh..wh..opq" in names
    assert "app/data/fresh.txt" in names
    # deletion of the L1-modified file is kept as a whiteout
    assert "etc/.wh.old.cfg" in names
    assert "etc/new.cfg" in names

    tree = expand_stack([base, merged])
    assert sorted(tree.nodes) == [
        "", "app", "app/data", "app/data/fresh.txt", "app/server.bin",
        "etc", "etc/keep.cfg", "etc/new.cfg",
    ]


def test_whiteout_of_file_created_in_lower_layer(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("keep.txt", b"k")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("temp.txt", b"t")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("temp.txt")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    # conservative: the whiteout stays so the deletion also applies to the
    # base layers (which might contain temp.txt — we cannot know)
    assert ".wh.temp.txt" in layer_entry_names(merged)
    assert "temp.txt" not in layer_entry_names(merged)


def test_whiteout_then_readd_in_upper_layer(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("cfg", b"base-cfg")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [whiteout_entry("cfg")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("cfg", b"new-cfg")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "cfg" in names and ".wh.cfg" not in names


def test_opaque_in_lower_layer_is_preserved(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("d"), file_entry("d/from-base", b"b"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [opaque_entry("d"), file_entry("d/a", b"a")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("d/b", b"b2")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "d/.wh..wh..opq" in names
    assert "d/a" in names and "d/b" in names
    assert "d/from-base" not in names


def test_upper_layer_deletes_dir_populated_by_lower(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("d"), file_entry("d/base-file", b"x")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("d/x", b"1"), file_entry("d/y", b"2")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("d")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert names == [".wh.d"]


def test_metadata_and_ownership_preserved(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("app")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [
        file_entry("app/secret", b"s3cret", mode=0o640, uid=1000, gid=2000),
        dir_entry("app/conf", mode=0o750, uid=1000, gid=2000),
        file_entry("app/conf/yaml", b"k: v", mode=0o600, uid=1000, gid=2000),
    ])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("app/readme", b"hi", mode=0o444, uid=65534, gid=65534)])

    merged = check_equivalent(tmp_path, base, l1, l2)
    tree = expand_stack([base, merged])
    assert tree.nodes["app/secret"].mode == 0o640
    assert (tree.nodes["app/secret"].uid, tree.nodes["app/secret"].gid) == (1000, 2000)
    assert tree.nodes["app/conf"].mode == 0o750
    assert tree.nodes["app/readme"].uid == 65534


def test_upper_version_of_file_wins(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("f", b"base")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("f", b"v1")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("f", b"v2", mode=0o755)])

    merged = check_equivalent(tmp_path, base, l1, l2)
    tree = expand_stack([base, merged])
    import hashlib

    assert tree.nodes["f"].content == (hashlib.sha256(b"v2").hexdigest(), 2)
    assert tree.nodes["f"].mode == 0o755


def test_file_replaces_dir_subtree(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("other", b"o")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [dir_entry("d"), file_entry("d/x", b"1")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("d", b"now-a-file")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "d" in names and "d/x" not in names


# ---------------------------------------------------------------------------
# directory replacement: a rebuilt directory must keep hiding the children
# that lower layers (including the base layer below the merged one) held at
# the same path
# ---------------------------------------------------------------------------


def test_lower_whiteout_then_upper_rebuilds_dir_keeps_base_hidden(tmp_path):
    """L1 deletes a base-populated directory; L2 rebuilds it explicitly.

    The merged layer must still carry a whiteout so the base-layer children
    cannot reappear under the rebuilt directory.
    """
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("app"), dir_entry("app/.config"),
        file_entry("app/.config/base.ini", b"[base]\n"),
        file_entry("app/.config/old.cfg", b"old\n"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [whiteout_entry("app/.config")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        dir_entry("app/.config", mode=0o700, uid=10, gid=20),
        file_entry("app/.config/new.ini", b"[new]\n", mode=0o640, uid=10, gid=20),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "app/.wh..config" in names  # deletion still applies to the base
    assert "app/.config" in names and "app/.config/new.ini" in names
    assert "app/.config/base.ini" not in names
    assert "app/.config/old.cfg" not in names

    tree = expand_stack([base, merged])
    assert sorted(p for p in tree.nodes if p) == [
        "app", "app/.config", "app/.config/new.ini",
    ]
    # metadata of the rebuilt directory and new file is preserved
    assert tree.nodes["app/.config"].mode == 0o700
    assert (tree.nodes["app/.config"].uid, tree.nodes["app/.config"].gid) == (10, 20)
    assert tree.nodes["app/.config/new.ini"].mode == 0o640


def test_lower_file_replaced_by_upper_dir_synthesizes_hiding_whiteout(tmp_path):
    """L1 turns base directory ``d`` into a regular file; L2 rebuilds ``d``
    as a directory.  No whiteout existed in the inputs, so the merger must
    synthesize one — otherwise base children leak through the new dir."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("d"), file_entry("d/x", b"x"),
        dir_entry("d/sub"), file_entry("d/sub/deep", b"deep"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("d", b"d is a file now")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [dir_entry("d"), file_entry("d/new", b"n")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert names == [".wh.d", "d", "d/new"]

    tree = expand_stack([base, merged])
    assert sorted(p for p in tree.nodes if p) == ["d", "d/new"]
    assert tree.nodes["d"].kind == "dir"


def test_same_layer_whiteout_after_rebuilt_dir_entries(tmp_path):
    """The upper layer deletes a lower directory and rebuilds it in the same
    tar, with the whiteout marker sorted *after* the new entries.  The new
    directory and its files must survive; the lower children stay hidden."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("app"), dir_entry("app/.config"),
        file_entry("app/.config/base.ini", b"[base]\n"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [])  # empty lower layer
    # tar order is deliberately: new dir entries first, whiteout last
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        dir_entry("app"),
        dir_entry("app/.config"),
        file_entry("app/.config/new.ini", b"[new]\n"),
        whiteout_entry("app/.config"),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)
    # whiteout is emitted before the rebuilt subtree so strictly sequential
    # tar appliers delete the lower directory before re-creating it
    with tarfile.open(merged, "r:") as tf:
        stream_order = [m.name for m in tf]
    assert stream_order == [
        "app", "app/.wh..config", "app/.config", "app/.config/new.ini",
    ]
    names = layer_entry_names(merged)
    assert set(names) == set(stream_order)
    # and the whiteout really deletes the lower subtree at apply time
    tree = expand_stack([base, merged])
    assert sorted(p for p in tree.nodes if p) == [
        "app", "app/.config", "app/.config/new.ini",
    ]
    assert "app/.config/base.ini" not in tree.nodes

def test_same_layer_whiteout_before_rebuilt_dir_entries(tmp_path):
    """Same rebuild, but with the conventional marker-first tar ordering."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("d"), file_entry("d/base", b"b")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        whiteout_entry("d"), dir_entry("d"), file_entry("d/only", b"o"),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)
    assert layer_entry_names(merged) == [".wh.d", "d", "d/only"]
    tree = expand_stack([base, merged])
    assert sorted(p for p in tree.nodes if p) == ["d", "d/only"]


def test_plain_dir_overlay_keeps_untouched_lower_children(tmp_path):
    """A directory that merely overlays a lower directory stays a union."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("d", mode=0o700, uid=3, gid=4),
        file_entry("d/keep", b"k"), file_entry("d/gone", b"g"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("d/a", b"a"), whiteout_entry("d/gone")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [dir_entry("d", mode=0o755), file_entry("d/b", b"b")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "d/.wh.gone" in names and "d/a" in names and "d/b" in names
    assert ".wh.d" not in names  # no directory replacement happened
    tree = expand_stack([base, merged])
    assert sorted(p for p in tree.nodes if p) == ["d", "d/a", "d/b", "d/keep"]
    # explicit upper dir entry wins for the directory's own metadata
    assert tree.nodes["d"].mode == 0o755
    assert tree.nodes["d/keep"].content[1] == 1


def test_dir_rebuilt_over_file_with_base_siblings_still_hidden(tmp_path):
    """End-to-end style: base holds ``etc/`` plus a populated ``etc/cfg``;
    L1 replaces ``etc/cfg`` by a file; L2 makes it a directory again with
    new content.  Nothing from the old ``etc/cfg`` subtree may resurface."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("etc"), dir_entry("etc/cfg"),
        file_entry("etc/cfg/a", b"a"), file_entry("etc/cfg/b", b"b"),
        file_entry("etc/host", b"host\n"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("etc/cfg", b"single file")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        dir_entry("etc/cfg"), file_entry("etc/cfg/c", b"c"),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)
    tree = expand_stack([base, merged])
    assert sorted(p for p in tree.nodes if p) == [
        "etc", "etc/cfg", "etc/cfg/c", "etc/host",
    ]


def test_rebuilt_dir_metadata_and_owner_matched_in_full_stack(tmp_path):
    """Paths, contents, permissions and owners must match through the merge."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("d"), file_entry("d/old", b"old")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [whiteout_entry("d")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        dir_entry("d", mode=0o711, uid=77, gid=88),
        file_entry("d/new", b"new", mode=0o600, uid=77, gid=88),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)
    tree_old = expand_stack([base, l1, l2])
    tree_new = expand_stack([base, merged])
    # compare observable attributes only (the bookkeeping "layer" field
    # naturally differs between the original stack and the squashed one)
    for path in ("d", "d/new"):
        a, b = tree_new.nodes[path], tree_old.nodes[path]
        assert (a.kind, a.mode, a.uid, a.gid, a.content) == (
            b.kind, b.mode, b.uid, b.gid, b.content
        )


def test_duplicate_entries_within_one_layer_last_wins(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("f", b"base")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("f", b"first"), file_entry("f", b"second")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("g", b"g")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    import hashlib

    tree = expand_stack([base, merged])
    assert tree.nodes["f"].content == (hashlib.sha256(b"second").hexdigest(), 6)


def test_empty_upper_layer(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("a", b"a")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("b", b"b")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [])  # valid tar, zero entries

    merged = check_equivalent(tmp_path, base, l1, l2)
    assert layer_entry_names(merged) == ["b"]


def test_deep_nesting_and_multiple_whiteouts(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("a"), dir_entry("a/b"), dir_entry("a/b/c"),
        file_entry("a/b/c/deep.txt", b"deep"),
        file_entry("a/b/c/keep.txt", b"keep"),
        file_entry("a/b/gone.txt", b"gone"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [
        file_entry("a/b/c/deep.txt", b"deep-v1"),
        dir_entry("a/b/c/sub"), file_entry("a/b/c/sub/leaf", b"leaf"),
    ])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        whiteout_entry("a/b/c/sub"),       # delete L1's new subtree
        whiteout_entry("a/b/gone.txt"),    # delete a base file
        file_entry("a/b/c/deep.txt", b"deep-v2"),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "a/b/.wh.gone.txt" in names
    assert "a/b/c/.wh.sub" in names
    assert "a/b/c/sub/leaf" not in names


def test_unsupported_entry_type_rejected(tmp_path):
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("ok", b"ok")])
    l2 = tmp_path / "l2.tar"
    # hand-craft a layer containing a symlink
    import io

    with tarfile.open(l2, "w") as tf:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)

    with open(l1, "rb") as f1, open(l2, "rb") as f2:
        t1 = tarfile.open(fileobj=f1, mode="r:")
        t2 = tarfile.open(fileobj=f2, mode="r:")
        merger = LayerMerger()
        merger.add_layer(t1, 0)
        with pytest.raises(MergeError, match="unsupported tar entry type"):
            merger.add_layer(t2, 1)


def test_path_traversal_rejected(tmp_path):
    bad = tmp_path / "bad.tar"
    import io

    with tarfile.open(bad, "w") as tf:
        ti = tarfile.TarInfo("../escape")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))

    merger = LayerMerger()
    with open(bad, "rb") as f:
        tf = tarfile.open(fileobj=f, mode="r:")
        with pytest.raises(MergeError, match="escapes root"):
            merger.add_layer(tf, 0)


def merge_layers_full(tmp_path: Path, l1: Path, l2: Path, synthesize: bool) -> Path:
    out = tmp_path / f"merged-{synthesize}.tar"
    with open(l1, "rb") as f1, open(l2, "rb") as f2:
        t1 = tarfile.open(fileobj=f1, mode="r:")
        t2 = tarfile.open(fileobj=f2, mode="r:")
        merger = LayerMerger(synthesize_implicit_dirs=synthesize)
        merger.add_layer(t1, 0)
        merger.add_layer(t2, 1)
        with open(out, "wb") as fout:
            merger.write_merged([t1, t2], fout)
    return out


def test_implicit_dirs_synthesized_when_no_base(tmp_path):
    """Two-layer image: a dir created only implicitly whose contents are all
    deleted again must still exist after merging (it did before)."""
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("etc/old.cfg", b"x")])  # no explicit etc/ entry
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("etc/old.cfg")])

    merged = merge_layers_full(tmp_path, l1, l2, synthesize=True)
    names = layer_entry_names(merged)
    assert "etc" in names and "etc/.wh.old.cfg" in names
    expand_stack([merged]).assert_same_as(expand_stack([l1, l2]))


def test_implicit_dirs_not_synthesized_with_base(tmp_path):
    """With base layers the merged layer must not gain dir entries that could
    clobber base metadata; the base provides the directories."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("etc", mode=0o700, uid=5, gid=6)])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("etc/old.cfg", b"x")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("etc/old.cfg")])

    merged = merge_layers_full(tmp_path, l1, l2, synthesize=False)
    assert layer_entry_names(merged) == ["etc/.wh.old.cfg"]
    tree = expand_stack([base, merged])
    assert tree.nodes["etc"].mode == 0o700  # base metadata untouched
    expand_stack([base, merged]).assert_same_as(expand_stack([base, l1, l2]))


def test_memory_usage_independent_of_file_size(tmp_path):
    """A 32 MiB file must be merged with bounded memory (streaming)."""
    import os

    big = os.urandom(32 * 1024 * 1024)
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("seed", b"s")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("big.bin", big)])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("small.txt", b"small")])

    del big  # only the on-disk tars should matter now
    tracemalloc.start()
    try:
        merged = merge_layers(tmp_path, l1, l2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024, f"peak memory {peak / 2**20:.1f} MiB too high"

    check_equivalent(tmp_path, base, l1, l2)
    # merged layer carries the big file exactly once
    assert (tmp_path / "merged.tar").stat().st_size > 32 * 1024 * 1024
