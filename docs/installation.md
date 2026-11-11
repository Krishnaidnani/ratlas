# Installation

Requires Python 3.11 or later.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,mcp]"
```

From a target repository:

```powershell
ratlas init
ratlas index
ratlas query map
ratlas query context "trace request handling" --budget 4000
```

To build the documentation locally, install `.[docs]` and run `mkdocs serve`.
