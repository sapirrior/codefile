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

_ORANGE = "\033[38;5;173m"
_GREY   = "\033[38;5;244m"
_RESET  = "\033[0m"


def _o(text: str) -> str:
    return f"{_ORANGE}{text}{_RESET}"


def _g(text: str) -> str:
    return f"{_GREY}{text}{_RESET}"


def _err(text: str):
    print(f"{_ORANGE}{text}{_RESET}", file=sys.stderr)


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
            _err(f"Warning: could not read .gitignore: {e}")

    def _match_pattern(self, pattern: str, rel_posix: str, is_dir: bool) -> bool:
        parts = rel_posix.split("/")

        if "**" in pattern:
            if fnmatch.fnmatch(rel_posix, pattern):
                return True
            stem = pattern.removeprefix("**/")
            if fnmatch.fnmatch(rel_posix, stem):
                return True
            for i in range(len(parts)):
                if fnmatch.fnmatch("/".join(parts[i:]), stem):
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
    try:
        with path.open("rb") as f:
            while chunk := f.read(IO_CHUNK):
                h.update(chunk)
    except OSError:
        return "error0000000"
    return h.hexdigest()[:12]


def pack(root: Path, out_path: Path, script_path: Path):
    gi = GitignoreParser(root)
    out_resolved = out_path.resolve()
    script_resolved = script_path.resolve()

    packed: list[tuple[Path, str]] = []
    total_bytes = 0
    skipped_count = 0

    print(_o(f"Scanning: {root.name}/"))

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

            try:
                fp_resolved = fp.resolve()
            except OSError:
                skipped_count += 1
                continue

            if fp_resolved in (out_resolved, script_resolved):
                skipped_count += 1
                continue

            if fp.is_symlink():
                packed.append((fp, rel))
                continue

            if not fp.is_file():
                skipped_count += 1
                continue

            if gi.is_ignored(fp):
                skipped_count += 1
                continue

            total_bytes += fp.stat().st_size
            packed.append((fp, rel))

    print(_g(f"Packing {len(packed)} files ({total_bytes / (1024*1024):.2f} MB source), {skipped_count} skipped."))

    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    packed_rels = [rel for _, rel in packed]
    tree_str = build_tree(root.name, packed_rels)

    write_errors = 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", errors="replace", newline="\n") as out:
        out.write(f"ROOT_NAME {root.name}\n")
        out.write(f"CREATED_UTC {timestamp}\n")
        out.write(f"TOTAL_SOURCE_MB {total_bytes / (1024*1024):.2f}\n")
        out.write(f"FILE_COUNT {len(packed)}\n\n")
        out.write("START_STRUCTURE\n" + tree_str + "\nEND_STRUCTURE\n\n")

        for fp, rel in packed:
            try:
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
                _err(f"Warning: skipped {rel}: {e}")
                write_errors += 1

    out_size = out_path.stat().st_size
    msg = _o(f"Done. {out_path.name} written ({out_size / (1024*1024):.2f} MB).")
    if write_errors:
        msg += " " + _g(f"{write_errors} file(s) had read errors.")
    print(msg)


def build_project(in_path: Path):
    root = in_path.parent.resolve()

    if not in_path.is_file():
        sys.exit(_o(f"Fatal: {in_path} not found."))

    print(_o(f"Rebuilding project from {in_path.name}...") + "\n")

    built_count = 0
    skipped_count = 0
    error_count = 0

    try:
        with in_path.open("r", encoding="utf-8", errors="replace") as f:
            line = f.readline()
            while line:
                if not line.startswith("FILE_START "):
                    line = f.readline()
                    continue

                tokens = line.strip().split(" ")
                if len(tokens) < 3:
                    _err(f"Error: Malformed FILE_START line: {line.rstrip()}")
                    error_count += 1
                    line = f.readline()
                    continue

                fid = tokens[-1]
                rel = " ".join(tokens[1:-1])
                sentinel_end = f"FILE_END {rel} {fid}\n"

                if not rel:
                    _err(f"Error: Empty path in FILE_START line: {line.rstrip()}")
                    error_count += 1
                    line = f.readline()
                    continue

                target_path = (root / rel).resolve()

                try:
                    target_path.relative_to(root)
                except ValueError:
                    _err(f"Warning: Path traversal detected for {rel}. Skipped.")
                    skipped_count += 1
                    while line and line != sentinel_end:
                        line = f.readline()
                    line = f.readline()
                    continue

                try:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    _err(f"Error: Could not create directory for {rel} ({e}). Skipped.")
                    error_count += 1
                    skipped_count += 1
                    while line and line != sentinel_end:
                        line = f.readline()
                    line = f.readline()
                    continue

                if target_path.exists():
                    print(_g(f"Notice: Overwriting existing file: {rel}"))

                line = f.readline()
                is_special = False

                if line.strip() in ("SYMLINK_CONTENT", "EMPTY_CONTENT", "BINARY_CONTENT"):
                    special_marker = line.strip()
                    next_line = f.readline()
                    if next_line == sentinel_end:
                        is_special = True
                        if special_marker == "EMPTY_CONTENT":
                            try:
                                target_path.touch()
                                print(_g(f" -> Created empty: {rel}"))
                                built_count += 1
                            except OSError as e:
                                _err(f"Error: Could not create empty file {rel} ({e})")
                                error_count += 1
                        else:
                            print(_g(f" -> Skipped special: {rel} ({special_marker})"))
                            skipped_count += 1
                        line = f.readline()
                    else:
                        prev_line = line
                        line = next_line
                else:
                    prev_line = line
                    line = f.readline()

                if not is_special:
                    try:
                        with target_path.open("w", encoding="utf-8", newline="") as out_f:
                            while line:
                                if line == sentinel_end:
                                    if prev_line is not None:
                                        stripped = prev_line[:-1] if prev_line.endswith("\n") else prev_line
                                        out_f.write(stripped)
                                    print(_o(f" -> Extracted: {rel}"))
                                    built_count += 1
                                    line = f.readline()
                                    break
                                else:
                                    if prev_line is not None:
                                        out_f.write(prev_line)
                                    prev_line = line
                                line = f.readline()
                            else:
                                _err(f"Warning: Unexpected EOF while reading {rel}.")
                                error_count += 1
                    except OSError as e:
                        _err(f"Error: Failed to write {rel} ({e})")
                        error_count += 1
                        skipped_count += 1

    except OSError as e:
        sys.exit(_o(f"Critical Error: Could not read {in_path.name} ({e})"))

    print(
        "\n" + _o(f"Build Complete: {built_count} files created, ")
        + _g(f"{skipped_count} skipped, {error_count} errors.")
    )


def main():
    parser = argparse.ArgumentParser(
        prog="codefile",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Pack a project directory into a single structured text file for LLM context.",
        epilog=(
            "Examples:\n"
            "  codefile                        Pack the current directory\n"
            "  codefile /path/to/project       Pack a specific directory\n"
            "  codefile . -o context.txt       Write output to a custom path\n"
            "  codefile --build                Reconstruct project from CodeFile.txt\n"
            "  codefile --build -o archive.txt Reconstruct from a custom pack file\n"
            "\n"
            "Output format:\n"
            "  Each file is wrapped in FILE_START / FILE_END sentinels with a SHA-256\n"
            "  fingerprint. Binary, empty, and symlink files use inline markers.\n"
            "  .gitignore rules are respected during packing."
        ),
    )
    parser.add_argument(
        "-b", "--build",
        action="store_true",
        help=f"Reconstruct project from the pack file (default: {OUT_DEFAULT} in cwd)",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Root directory to pack (default: current directory; ignored with --build)",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help=(
            f"Pack mode: output file path (default: <directory>/{OUT_DEFAULT}). "
            f"Build mode: pack file to read from (default: ./{OUT_DEFAULT})"
        ),
    )

    args = parser.parse_args()

    if args.build:
        in_path = Path(args.output).resolve() if args.output else Path.cwd().resolve() / OUT_DEFAULT
        try:
            build_project(in_path)
        except Exception as e:
            sys.exit(_o(f"Critical build failure: {e}"))
        return

    try:
        root = Path(args.directory).resolve()
    except Exception as e:
        sys.exit(_o(f"Fatal: cannot resolve directory: {e}"))

    if not root.is_dir():
        sys.exit(_o(f"Fatal: {root} is not a directory."))

    out_path = Path(args.output).resolve() if args.output else root / OUT_DEFAULT
    script_path = Path(__file__).resolve()

    try:
        pack(root, out_path, script_path)
    except Exception as e:
        sys.exit(_o(f"Critical failure: {e}"))


if __name__ == "__main__":
    main()
