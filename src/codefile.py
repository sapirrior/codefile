import os
import sys
import hashlib
import datetime
import fnmatch
import argparse
from pathlib import Path

IO_CHUNK         = 65536
BINARY_THRESHOLD = 8192
OUT_DEFAULT      = "CodeFile.txt"
VERSION          = "26.0.3"
CREATOR          = "Nolan Stark (sapirrior)"

_orangex = "\033[38;5;173m"
_greyx   = "\033[38;5;244m"
_resetx  = "\033[0m"


def _o(text):
    return f"{_orangex}{text}{_resetx}"

def _g(text):
    return f"{_greyx}{text}{_resetx}"

def _err(text):
    print(f"{_orangex}{text}{_resetx}", file=sys.stderr)


# ── binary detection ────────────────────────────────────────────────────────

def is_binary(path):
    try:
        with path.open("rb") as f:
            chunk = f.read(BINARY_THRESHOLD)
        if not chunk:
            return False
        if b"\x00" in chunk:
            return True
        try:
            chunk.decode("utf-8")
            return False
        except UnicodeDecodeError:
            pass
        return (sum(1 for b in chunk if b > 127) / len(chunk)) > 0.30
    except OSError:
        return True


# ── .gitignore parser ────────────────────────────────────────────────────────

class GitignoreParser:
    def __init__(self, root):
        self.root  = root
        self.rules = []          # (negated, dir_only, anchored, pattern, base)
        self._load_dir(root)

    def _load_dir(self, directory):
        gi = directory / ".gitignore"
        if gi.is_file():
            self._load(gi, directory)
        try:
            for entry in sorted(directory.iterdir()):
                if entry.is_dir() and entry.name != ".git":
                    self._load_dir(entry)
        except OSError:
            pass

    def _load(self, gi_path, base):
        try:
            with gi_path.open("r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.rstrip("\n\r")
                    if not line or line.startswith("#"):
                        continue
                    negated = line.startswith("!")
                    if negated:
                        line = line[1:]
                    line = line.strip()
                    if not line:
                        continue
                    dir_only = line.endswith("/")
                    if dir_only:
                        line = line.rstrip("/")
                    anchored = "/" in line.lstrip("/")
                    if line.startswith("/"):
                        line = line.lstrip("/")
                        anchored = True
                    self.rules.append((negated, dir_only, anchored, line, base))
        except OSError as e:
            _err(f"Warning: could not read {gi_path}: {e}")

    def _match(self, pattern, rel_posix):
        parts = rel_posix.split("/")
        if "**" in pattern:
            if fnmatch.fnmatch(rel_posix, pattern):
                return True
            stem = pattern
            while stem.startswith("**/"):
                stem = stem[3:]
            for i in range(len(parts)):
                if fnmatch.fnmatch("/".join(parts[i:]), stem):
                    return True
            if "/**/" in pattern:
                head, tail = pattern.split("/**/", 1)
                for i in range(1, len(parts)):
                    if (fnmatch.fnmatch("/".join(parts[:i]), head)
                            and fnmatch.fnmatch("/".join(parts[i:]), tail)):
                        return True
            return False
        if "/" in pattern:
            return fnmatch.fnmatch(rel_posix, pattern)
        return any(fnmatch.fnmatch(p, pattern) for p in parts)

    def is_ignored(self, path):
        try:
            path.relative_to(self.root)
        except ValueError:
            return True
        is_dir  = path.is_dir()
        matched = False
        for negated, dir_only, anchored, pattern, base in self.rules:
            if dir_only and not is_dir:
                continue
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                continue
            if anchored:
                hit = fnmatch.fnmatch(rel, pattern) or rel.startswith(pattern + "/")
            else:
                hit = self._match(pattern, rel)
            if hit:
                matched = not negated
        return matched


# ── tree builder ─────────────────────────────────────────────────────────────

def build_tree(root_name, rel_paths):
    tree = {}
    for p in sorted(rel_paths):
        node = tree
        for part in p.split("/"):
            node = node.setdefault(part, {})
    lines = [f"{root_name}/"]
    def walk(node, prefix):
        items = sorted(node.items(), key=lambda x: (not x[1], x[0]))
        for i, (name, children) in enumerate(items):
            last = i == len(items) - 1
            conn = "\\-- " if last else "|-- "
            lines.append(f"{prefix}{conn}{name}{'/' if children else ''}")
            if children:
                walk(children, prefix + ("    " if last else "|   "))
    walk(tree, "")
    return "\n".join(lines)


# ── hashing ───────────────────────────────────────────────────────────────────

def hash_file(path):
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while chunk := f.read(IO_CHUNK):
                h.update(chunk)
    except OSError:
        return "err000000000"   # 12 chars
    return h.hexdigest()[:12]


# ── sentinel-collision escape ─────────────────────────────────────────────────
# Lines starting with "FILE_START " or "FILE_END " in file content are
# prefixed with ESC (0x1B).  The reader strips it back before writing to disk.

_ESC = "\x1b"

def _esc(line):
    return (_ESC + line) if line.startswith(("FILE_START ", "FILE_END ")) else line

def _unesc(line):
    return line[1:] if line.startswith(_ESC) else line


# ── pack ──────────────────────────────────────────────────────────────────────
#
# Text layout in pack file:
#   FILE_START path fid
#   <escaped content lines, each ending \n>
#   [DATA_NONEWLINE\n]   <- only present when original had no trailing \n
#   FILE_END path fid
#
# The DATA_NONEWLINE marker is preceded by a bare \n (the one that pack adds
# so the marker sits on its own line).  On build, both lines are stripped and
# the reconstructed file is truncated by one byte to remove that trailing \n.

def pack(root, out_path, script_path):
    gi              = GitignoreParser(root)
    out_resolved    = out_path.resolve()
    script_resolved = script_path.resolve()

    packed        = []
    total_bytes   = 0
    skipped_count = 0

    print(_o(f"Scanning  {root.name}/"))

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        if ".git" in dp.parts:
            dirnames.clear()
            continue
        dirnames[:] = [d for d in sorted(dirnames) if not gi.is_ignored(dp / d)]
        for fname in sorted(filenames):
            fp = dp / fname
            try:
                rel = fp.relative_to(root).as_posix()
            except ValueError:
                skipped_count += 1; continue
            try:
                fp_resolved = fp.resolve()
            except OSError:
                skipped_count += 1; continue
            if fp_resolved in (out_resolved, script_resolved):
                skipped_count += 1; continue
            if fp.is_symlink():
                packed.append((fp, rel)); continue
            if not fp.is_file():
                skipped_count += 1; continue
            if gi.is_ignored(fp):
                skipped_count += 1; continue
            total_bytes += fp.stat().st_size
            packed.append((fp, rel))

    print(_g(f"  {len(packed)} files  ({total_bytes / (1024*1024):.2f} MB)  {skipped_count} skipped"))

    timestamp   = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    packed_rels = [r for _, r in packed]
    tree_str    = build_tree(root.name, packed_rels)

    write_errors  = 0
    binary_count  = 0
    symlink_count = 0
    empty_count   = 0
    text_count    = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", errors="replace", newline="\n") as out:
        out.write(f"ROOT_NAME {root.name}\n")
        out.write(f"CREATED_UTC {timestamp}\n")
        out.write(f"TOTAL_SOURCE_MB {total_bytes / (1024*1024):.2f}\n")
        out.write(f"FILE_COUNT {len(packed)}\n\n")
        out.write("START_STRUCTURE\n" + tree_str + "\nEND_STRUCTURE\n\n")

        for fp, rel in packed:
            try:
                # symlink
                if fp.is_symlink():
                    try:
                        tgt = str(os.readlink(fp))
                    except OSError:
                        tgt = ""
                    fid = hashlib.sha256(tgt.encode()).hexdigest()[:12]
                    out.write(f"FILE_START {rel} {fid}\n")
                    out.write(f"SYMLINK_CONTENT {tgt}\n")
                    out.write(f"FILE_END {rel} {fid}\n\n")
                    symlink_count += 1
                    continue

                size = fp.stat().st_size

                # empty
                if size == 0:
                    fid = "empty0000000"
                    out.write(f"FILE_START {rel} {fid}\n")
                    out.write("EMPTY_CONTENT\n")
                    out.write(f"FILE_END {rel} {fid}\n\n")
                    empty_count += 1
                    continue

                # binary
                if is_binary(fp):
                    fid = hash_file(fp)
                    out.write(f"FILE_START {rel} {fid}\n")
                    out.write("BINARY_CONTENT\n")
                    out.write(f"FILE_END {rel} {fid}\n\n")
                    binary_count += 1
                    continue

                # text
                fid  = hash_file(fp)
                text = fp.read_bytes().decode("utf-8", errors="replace")
                has_nl = text.endswith("\n")

                out.write(f"FILE_START {rel} {fid}\n")
                for ln in text.splitlines(keepends=True):
                    out.write(_esc(ln))
                if not has_nl:
                    out.write("\nDATA_NONEWLINE\n")
                out.write(f"FILE_END {rel} {fid}\n\n")
                text_count += 1

            except OSError as e:
                _err(f"  ! skipped {rel}: {e}")
                write_errors += 1

    print(_g(f"  text:{text_count}  binary:{binary_count}  empty:{empty_count}  symlinks:{symlink_count}"))
    out_size = out_path.stat().st_size
    print(_o(f"Done  ->  {out_path.name}  ({out_size / (1024*1024):.2f} MB)"))
    if write_errors:
        print(_g(f"  {write_errors} file(s) had read errors"))


# ── build ─────────────────────────────────────────────────────────────────────

def build_project(in_path):
    root = in_path.parent.resolve()

    if not in_path.is_file():
        sys.exit(_o(f"Fatal: {in_path} not found."))

    print(_o(f"Rebuilding from  {in_path.name}") + "\n")

    built_count   = 0
    skipped_count = 0
    error_count   = 0

    try:
        with in_path.open("r", encoding="utf-8", errors="replace") as f:

            def skip_to_end(sentinel):
                while True:
                    ln = f.readline()
                    if not ln or ln == sentinel:
                        return

            line = f.readline()
            while line:
                if not line.startswith("FILE_START "):
                    line = f.readline()
                    continue

                tokens = line.strip().split(" ")
                if len(tokens) < 3:
                    _err(f"  ! Malformed FILE_START: {line.rstrip()}")
                    error_count += 1
                    line = f.readline()
                    continue

                fid          = tokens[-1]
                rel          = " ".join(tokens[1:-1])
                sentinel_end = f"FILE_END {rel} {fid}\n"

                if not rel:
                    _err(f"  ! Empty path in FILE_START: {line.rstrip()}")
                    error_count += 1
                    line = f.readline()
                    continue

                # path traversal guard
                target_path = (root / rel).resolve()
                try:
                    target_path.relative_to(root)
                except ValueError:
                    _err(f"  ! Path traversal: {rel}  (skipped)")
                    skipped_count += 1
                    skip_to_end(sentinel_end)
                    line = f.readline()
                    continue

                # create parent dirs
                try:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    _err(f"  ! Cannot create dir for {rel}: {e}  (skipped)")
                    error_count  += 1
                    skipped_count += 1
                    skip_to_end(sentinel_end)
                    line = f.readline()
                    continue

                if target_path.exists() and not target_path.is_symlink():
                    print(_g(f"  ~ overwriting: {rel}"))

                first_line = f.readline()
                if not first_line:
                    _err(f"  ! Unexpected EOF reading {rel}")
                    error_count += 1
                    line = f.readline()
                    continue

                marker = first_line.rstrip("\n")

                # empty
                if marker == "EMPTY_CONTENT":
                    confirm = f.readline()
                    if confirm == sentinel_end:
                        try:
                            target_path.touch()
                            print(_g(f"  + empty    {rel}"))
                            built_count += 1
                        except OSError as e:
                            _err(f"  ! {e}")
                            error_count += 1
                    else:
                        _err(f"  ! Malformed EMPTY_CONTENT: {rel}")
                        error_count += 1
                        skip_to_end(sentinel_end)
                    line = f.readline()
                    continue

                # binary
                if marker == "BINARY_CONTENT":
                    confirm = f.readline()
                    if confirm == sentinel_end:
                        try:
                            target_path.touch()
                            print(_g(f"  + binary   {rel}  (placeholder)"))
                            built_count += 1
                        except OSError as e:
                            _err(f"  ! {e}")
                            error_count += 1
                    else:
                        _err(f"  ! Malformed BINARY_CONTENT: {rel}")
                        error_count += 1
                        skip_to_end(sentinel_end)
                    line = f.readline()
                    continue

                # symlink
                if first_line.startswith("SYMLINK_CONTENT"):
                    sl_parts   = first_line.rstrip("\n").split(" ", 1)
                    target_str = sl_parts[1] if len(sl_parts) > 1 else ""
                    confirm    = f.readline()
                    if confirm == sentinel_end:
                        try:
                            if target_path.is_symlink() or target_path.exists():
                                target_path.unlink(missing_ok=True)
                            if target_str:
                                os.symlink(target_str, target_path)
                                print(_g(f"  + symlink  {rel}  -> {target_str}"))
                            else:
                                target_path.touch()
                                print(_g(f"  + symlink  {rel}  (no target, created empty)"))
                            built_count += 1
                        except OSError as e:
                            _err(f"  ! Cannot create symlink {rel}: {e}")
                            error_count += 1
                    else:
                        _err(f"  ! Malformed SYMLINK_CONTENT: {rel}")
                        error_count += 1
                        skip_to_end(sentinel_end)
                    line = f.readline()
                    continue

                # text
                ok = _write_text(target_path, rel, first_line, sentinel_end, f)
                if ok:
                    built_count += 1
                    print(_o(f"  + text     {rel}"))
                else:
                    error_count += 1

                line = f.readline()

    except OSError as e:
        sys.exit(_o(f"Critical: could not read {in_path.name}: {e}"))

    print(
        "\n" + _o(f"Build complete  {built_count} created  ")
        + _g(f"{skipped_count} skipped  {error_count} errors")
    )


def _write_text(target_path, rel, first_line, sentinel_end, f):
    """
    Collect lines up to sentinel_end, detect DATA_NONEWLINE, write to disk.
    """
    content = [first_line]
    while True:
        ln = f.readline()
        if not ln:
            _err(f"  ! Unexpected EOF while reading {rel}")
            return False
        if ln == sentinel_end:
            break
        content.append(ln)

    # Strip DATA_NONEWLINE marker.
    # Pack writes the last content line (no \n), then "\n", then "DATA_NONEWLINE\n".
    # In the file those appear as: "last line\n" and "DATA_NONEWLINE\n" — two lines.
    # content[-2] ends with \n (the bare \n pack appended); we must strip it.
    no_trailing_nl = False
    if content and content[-1] == "DATA_NONEWLINE\n":
        content.pop()
        no_trailing_nl = True
        if content and content[-1].endswith("\n"):
            content[-1] = content[-1][:-1]

    try:
        with target_path.open("w", encoding="utf-8", newline="") as out_f:
            for ln in content:
                out_f.write(_unesc(ln))

        return True
    except OSError as e:
        _err(f"  ! Failed to write {rel}: {e}")
        return False


# ── CLI ───────────────────────────────────────────────────────────────────────

def _confirm_overwrite(path):
    try:
        ans = input(_g(f"  '{path.name}' already exists. Overwrite? [y/N] "))
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans.strip().lower() in ("y", "yes")


def main():
    if len(sys.argv) == 1:
        _print_help()
        return

    p = argparse.ArgumentParser(prog="codefile", add_help=False)
    p.add_argument("-h", "--help",    action="store_true")
    p.add_argument("-v", "--version", action="store_true")
    p.add_argument("-cr", "--creator", action="store_true")
    p.add_argument("-c", "--create",  action="store_true")
    p.add_argument("-b", "--build",   action="store_true")
    p.add_argument("directory",       nargs="?", default=".")
    p.add_argument("-o", "--output",  default=None)
    args = p.parse_args()

    if args.help:
        _print_help(); return

    if args.version:
        print(_o(f"codefile  v{VERSION}")); return

    if args.creator:
        print(_o(f"codefile  by  {CREATOR}")); return

    if args.build and args.create:
        sys.exit(_o("Fatal: --build and --create are mutually exclusive."))

    if args.build:
        in_path = (
            Path(args.output).resolve() if args.output
            else Path.cwd().resolve() / OUT_DEFAULT
        )
        try:
            build_project(in_path)
        except Exception as e:
            sys.exit(_o(f"Critical build failure: {e}"))
        return

    if args.create:
        try:
            root = Path(args.directory).resolve()
        except Exception as e:
            sys.exit(_o(f"Fatal: cannot resolve directory: {e}"))
        if not root.is_dir():
            sys.exit(_o(f"Fatal: {root} is not a directory."))

        if args.output:
            out_path = Path(args.output).resolve()
            if out_path.name != OUT_DEFAULT:
                print(_g(f"  Recommendation: using '{OUT_DEFAULT}' as output name is recommended."))
        else:
            out_path = root / OUT_DEFAULT

        if out_path.exists():
            if not _confirm_overwrite(out_path):
                print(_g("  Aborted.")); return

        try:
            pack(root, out_path, Path(__file__).resolve())
        except Exception as e:
            sys.exit(_o(f"Critical failure: {e}"))
        return

    _print_help()


def _print_help():
    print("\n".join([
        "",
        _o("  codefile") + _g(f"  v{VERSION}  by {CREATOR}"),
        "",
        _g("  Pack a project into a structured text file for LLM context,"),
        _g("  or reconstruct a project from one."),
        "",
        _o("  Usage"),
        _g("    codefile -c [directory] [-o output]"),
        _g("    codefile -b [-o codefile]"),
        "",
        _o("  Commands"),
        _g("    -c, --create    Pack a directory into a CodeFile"),
        _g("    -b, --build     Reconstruct a project from a CodeFile"),
        "",
        _o("  Options"),
        _g("    -o, --output    Output path (pack) or input path (build)"),
        _g(f"                    Default: <directory>/{OUT_DEFAULT}"),
        _g("    -v, --version   Print version"),
        _g("    -cr, --creator  Print creator info"),
        _g("    -h, --help      Print this help"),
        "",
        _o("  Examples"),
        _g("    codefile -c                      Pack current directory"),
        _g("    codefile -c /path/to/project     Pack a specific directory"),
        _g("    codefile -c . -o context.txt     Custom output name"),
        _g("    codefile -b                      Build from ./CodeFile.txt"),
        _g("    codefile -b -o archive.txt       Build from custom file"),
        "",
        _o("  Format"),
        _g("    FILE_START / FILE_END sentinels with SHA-256 fingerprints."),
        _g("    .gitignore rules respected (including nested). Binary, empty,"),
        _g("    and symlink files use inline markers."),
        "",
    ]))


if __name__ == "__main__":
    main()
