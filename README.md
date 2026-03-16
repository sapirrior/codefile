# codefile

[![PyPI version](https://badge.fury.io/py/codefile.svg)](https://badge.fury.io/py/codefile)

A CLI tool that bundles an entire project directory into a single, structured text file — purpose-built for feeding codebases to LLMs like Claude, ChatGPT, Gemini, and Grok.

---

## How It Works

`codefile` walks your project root, respects your `.gitignore`, and writes every relevant file into one output file with a clear format: a metadata header, an ASCII directory tree, and each file's content wrapped in `FILE_START` / `FILE_END` sentinels with a SHA-256 fingerprint. Binary files, symlinks, and empty files are represented with inline markers instead of garbage content.

---

## Installation

```bash
pip install codefile
```

Requires Python 3.10 or later.

---

## Usage

```bash
# Pack the current directory
codefile

# Pack a specific directory
codefile /path/to/project

# Write output to a custom path
codefile /path/to/project -o context.txt
```

### Options

| Argument | Description |
|---|---|
| `directory` | Root directory to pack. Defaults to the current directory. |
| `-o, --output` | Output file path. Defaults to `<directory>/CodeFile.txt`. |

---

## Output Format

```
ROOT_NAME        myproject
CREATED_UTC      2025-08-01T12:00:00Z
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

FILE_START assets/logo.png d7e2b45f9c33
BINARY_CONTENT
FILE_END assets/logo.png d7e2b45f9c33
```

### Special Content Markers

| Marker | Meaning |
|---|---|
| `BINARY_CONTENT` | File is binary (image, compiled object, etc.) |
| `EMPTY_CONTENT` | File exists but has zero bytes |
| `SYMLINK_CONTENT` | File is a symbolic link |

---

## `.gitignore` Support

`codefile` implements a proper `.gitignore` parser, not just glob matching. The following pattern types are all handled correctly:

- **Glob patterns** — `*.log`, `*.pyc`
- **Directory-only rules** — `build/`, `dist/`
- **Root-anchored rules** — `/.env`, `/config/secrets.json`
- **Deep glob** — `**/*.min.js`
- **Negation** — `!important.log` re-includes a previously excluded file

Directories matched by an ignore rule are pruned immediately during the walk — `node_modules/` or `build/` with thousands of files are never descended into.

---

## Project Structure (src layout)

```
your-project/
├── src/
│   ├── codefile.py          ← main module
│   └── config/
│       └── version.txt      ← single source of truth for version
├── pyproject.toml
├── README.md
└── LICENSE
```

The version is read directly from `src/config/version.txt` at build time. To release a new version, update that file.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

## Author

Nolan Stark
