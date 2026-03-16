import os
import sys
import hashlib
import datetime
import fnmatch
import argparse
from pathlib import Path

IO_CHUNK = 65536
BINARY_THRESHOLD = 8192
OUT_DEFAULT = "CodeFile.txt"


def is_binary(path: Path) -> bool:
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
        high_bytes = sum(1 for b in chunk if b > 127)
        return (high_bytes / len(chunk)) > 0.30
    except OSError:
        return True


class GitignoreParser:
    def __init__(self, root: Path):
        self.root = root
        self.rules: list[tuple[bool, bool, bool, str]] = []
        self._load(root / ".gitignore")

    def _load(self, gitignore_path: Path):
        if not gitignore_path.is_file():
            return
        try:
            with gitignore_path.open("r", encoding="utf-8", errors="ignore") as f:
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

                    self.rules.append((negated, dir_only, anchored, line))
        except OSError as e:
            print(f"Warning: could not read .gitignore: {e}", file=sys.stderr)

    def _match_pattern(self, pattern: str, rel_posix: str, is_dir: bool) -> bool:
        parts = rel_posix.split("/")

        if "**" in pattern:
            if fnmatch.fnmatch(rel_posix, pattern):
                return True
            if fnmatch.fnmatch(rel_posix, pattern.lstrip("**/")):
                return True
            return False

        if "/" in pattern:
            return fnmatch.fnmatch(rel_posix, pattern)

        for part in parts:
            if fnmatch.fnmatch(part, pattern):
                return True
        return False

    def is_ignored(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            return True

        is_dir = path.is_dir()
        matched = False

        for negated, dir_only, anchored, pattern in self.rules:
            if dir_only and not is_dir:
                continue

            if anchored:
                hit = fnmatch.fnmatch(rel, pattern) or rel.startswith(pattern + "/")
            else:
                hit = self._match_pattern(pattern, rel, is_dir)

            if hit:
                matched = not negated

        return matched


def build_tree(root_name: str, rel_paths: list[str]) -> str:
    tree: dict = {}
    for p in sorted(rel_paths):
        node = tree
        for part in p.split("/"):
            node = node.setdefault(part, {})

    lines = [f"{root_name}/"]

    def walk(node: dict, prefix: str):
        items = sorted(node.items(), key=lambda x: (not x[1], x[0]))
        for i, (name, children) in enumerate(items):
            last = i == len(items) - 1
            conn = "\\-- " if last else "|-- "
            lines.append(f"{prefix}{conn}{name}{'/' if children else ''}")
            if children:
                walk(children, prefix + ("    " if last else "|   "))

    walk(tree, "")
    return "\n".join(lines)


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(IO_CHUNK):
            h.update(chunk)
    return h.hexdigest()[:12]


def pack(root: Path, out_path: Path, script_path: Path):
    gi = GitignoreParser(root)
    out_name = out_path.name
    script_name = script_path.name

    packed: list[Path] = []
    packed_rels: list[str] = []
    total_bytes = 0
    skipped_count = 0

    print(f"Scanning: {root.name}/")

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)

        if ".git" in dp.parts:
            dirnames.clear()
            continue

        dirnames[:] = [
            d for d in sorted(dirnames)
            if not gi.is_ignored(dp / d)
        ]

        for fname in sorted(filenames):
            fp = dp / fname
            try:
                rel = fp.relative_to(root).as_posix()
            except ValueError:
                skipped_count += 1
                continue

            if fname in (out_name, script_name) and dp == root:
                skipped_count += 1
                continue

            if fp.is_symlink():
                packed.append(fp)
                packed_rels.append(rel)
                continue

            if not fp.is_file():
                skipped_count += 1
                continue

            if gi.is_ignored(fp):
                skipped_count += 1
                continue

            total_bytes += fp.stat().st_size
            packed.append(fp)
            packed_rels.append(rel)

    print(f"Packing {len(packed)} files ({total_bytes / (1024*1024):.2f} MB source), {skipped_count} skipped.")

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tree_str = build_tree(root.name, packed_rels)

    write_errors = 0

    with out_path.open("w", encoding="utf-8", errors="replace", newline="\n") as out:
        out.write(f"ROOT_NAME {root.name}\n")
        out.write(f"CREATED_UTC {timestamp}\n")
        out.write(f"TOTAL_SOURCE_MB {total_bytes / (1024*1024):.2f}\n")
        out.write(f"FILE_COUNT {len(packed)}\n\n")
        out.write("START_STRUCTURE\n" + tree_str + "\nEND_STRUCTURE\n\n")

        for fp in packed:
            try:
                rel = fp.relative_to(root).as_posix()

                if fp.is_symlink():
                    fid = "symlink00000"
                    out.write(f"FILE_START {rel} {fid}\n")
                    out.write("SYMLINK_CONTENT")
                    out.write(f"\nFILE_END {rel} {fid}\n\n")
                    continue

                size = fp.stat().st_size

                if size == 0:
                    fid = "empty000000"
                    out.write(f"FILE_START {rel} {fid}\n")
                    out.write("EMPTY_CONTENT")
                    out.write(f"\nFILE_END {rel} {fid}\n\n")
                    continue

                if is_binary(fp):
                    fid = hash_file(fp)
                    out.write(f"FILE_START {rel} {fid}\n")
                    out.write("BINARY_CONTENT")
                    out.write(f"\nFILE_END {rel} {fid}\n\n")
                    continue

                fid = hash_file(fp)
                out.write(f"FILE_START {rel} {fid}\n")
                with fp.open("rb") as rb:
                    while chunk := rb.read(IO_CHUNK):
                        out.write(chunk.decode("utf-8", errors="replace"))
                out.write(f"\nFILE_END {rel} {fid}\n\n")

            except OSError as e:
                print(f"Warning: skipped {fp.name}: {e}", file=sys.stderr)
                write_errors += 1

    out_size = out_path.stat().st_size
    print(f"Done. {out_path.name} written ({out_size / (1024*1024):.2f} MB).", end="")
    if write_errors:
        print(f" {write_errors} file(s) had read errors.", end="")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Pack a project directory into a single text file for LLM context."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to pack (default: current directory)"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help=f"Output file path (default: <directory>/{OUT_DEFAULT})"
    )
    args = parser.parse_args()

    try:
        root = Path(args.directory).resolve()
    except Exception as e:
        sys.exit(f"Fatal: cannot resolve directory: {e}")

    if not root.is_dir():
        sys.exit(f"Fatal: {root} is not a directory.")

    out_path = Path(args.output).resolve() if args.output else root / OUT_DEFAULT
    script_path = Path(__file__).resolve()

    try:
        pack(root, out_path, script_path)
    except Exception as e:
        sys.exit(f"Critical failure: {e}")


if __name__ == "__main__":
    main()
