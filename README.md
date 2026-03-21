# codefile

[![PyPI version](https://badge.fury.io/py/codefile.svg)](https://badge.fury.io/py/codefile)
[![GitHub stars](https://img.shields.io/github/stars/sapirrior/codefile.svg)](https://github.com/sapirrior/codefile/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/sapirrior/codefile.svg)](https://github.com/sapirrior/codefile/network)
[![Downloads](https://static.pepy.tech/badge/codefile)](https://pepy.tech/project/codefile)

Pack an entire project directory into one structured text file, ready to paste into any LLM context window. Rebuild the original files back from that same file when you need to.

---

## Installation

Requires Python 3.10 or later.

```
pip install codefile
```

---

## What it does

When you run `codefile` on a project directory, it produces a single `.txt` file that contains:

- A metadata header (project name, timestamp, file count)
- An ASCII directory tree
- Every text file's content, each wrapped between `FILE_START` and `FILE_END` markers with a SHA-256 fingerprint
- Inline markers for binary files, empty files, and symlinks instead of garbage content

Your `.gitignore` rules are respected. The `.git` folder is always excluded.

---

## Usage

**Pack the current directory:**

```
codefile
```

**Pack a specific directory:**

```
codefile /path/to/project
```

**Write to a custom output path:**

```
codefile /path/to/project -o context.txt
```

**Rebuild a project from a pack file:**

```
codefile --build
```

**Rebuild from a custom-named pack file:**

```
codefile --build -o context.txt
```

---

## Options

| Argument | Description |
|---|---|
| `directory` | Directory to pack. Defaults to the current directory. Ignored when using `--build`. |
| `-o, --output` | Pack mode: where to write the output file. Build mode: which pack file to read from. |
| `-b, --build` | Rebuild project files from a pack file instead of creating one. |
| `-h, --help` | Show help and exit. |

---

## Output format

```
ROOT_NAME        myproject
CREATED_UTC      2026-03-21T12:00:00Z
TOTAL_SOURCE_MB  0.42
FILE_COUNT       18

START_STRUCTURE
myproject/
|-- src/
|   |-- main.py
|   \-- utils.py
\-- README.md
END_STRUCTURE

FILE_START src/main.py a3f9c12b8e01
def main():
    print("hello")
FILE_END src/main.py a3f9c12b8e01
```

Binary files, empty files, and symlinks appear as:

```
FILE_START assets/logo.png d7e2b45f9c33
BINARY_CONTENT
FILE_END assets/logo.png d7e2b45f9c33
```

---

## .gitignore support

`codefile` parses your `.gitignore` properly. The following pattern types all work:

- Glob patterns: `*.log`, `*.pyc`
- Directory rules: `build/`, `dist/`
- Root-anchored rules: `/.env`
- Deep globs: `**/*.min.js`
- Negation: `!important.log`

Ignored directories are pruned immediately during the walk, so large folders like `node_modules/` are never descended into.

---

## License

MIT
