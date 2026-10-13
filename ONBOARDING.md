# Installing SPARQL Doctor

SPARQL Doctor is a command-line tool. It needs **Python 3.10 or newer** and nothing else.
Install it on the machine that has the connection problem (a customer server, an application host, a CI runner),
because the results describe the network path *from that machine* to the endpoint.

## 1. Check Python

```bash
python3 --version
```

You need `Python 3.10` or higher. If not:

- **macOS:** `brew install python@3.12`
- **Debian/Ubuntu:** `sudo apt install python3 python3-venv python3-pip`
- **RHEL/Rocky/Alma:** `sudo dnf install python3.11`
- **Windows:** install from https://www.python.org/downloads/ and tick "Add python.exe to PATH". In the commands below, use `py` instead of `python3`.

## 2. Install

Pick one option.

### Option A: pipx (recommended for users)

[pipx](https://pipx.pypa.io) installs the tool in its own isolated environment and puts `sparql-doctor` on your PATH.

```bash
pipx install git+https://github.com/germanbraun/SPARQL_Doctor.git
```

Add the HTTP/2 extra (optional, see below):

```bash
pipx install "sparql-doctor[http2] @ git+https://github.com/germanbraun/SPARQL_Doctor.git"
```

### Option B: virtual environment + pip

```bash
python3 -m venv ~/.venvs/sparql-doctor
source ~/.venvs/sparql-doctor/bin/activate        # Windows: %USERPROFILE%\.venvs\sparql-doctor\Scripts\activate
pip install git+https://github.com/germanbraun/SPARQL_Doctor.git
```

### Option C: no install at all (locked-down servers)

The tool only uses the Python standard library, so a plain copy of the source works. Copy the `sparql_doctor/`
folder (for example from a downloaded ZIP of the repository) to the server and run it as a module from the folder
that contains it:

```bash
python3 -m sparql_doctor https://your-endpoint/sparql
```

## 3. Verify

```bash
sparql-doctor --version
sparql-doctor https://query.wikidata.org/sparql --quick
```

The second command takes about 30 seconds. It should finish with a summary line and one **WARN** for the Wikidata
User-Agent policy. That warning is expected and shows the tool works.

## Optional: HTTP/2 extra

Without extras, SPARQL Doctor detects *whether* a server offers HTTP/2. With the `http2` extra (which installs `httpx`
and `h2`), it also sends a real query over HTTP/2 and HTTP/1.1 and compares them. Use it when you suspect a problem
specific to HTTP/2 (stream resets, `GOAWAY`, Java 11+ clients failing while older ones work).

```bash
pip install "sparql-doctor[http2] @ git+https://github.com/germanbraun/SPARQL_Doctor.git"
```

## Behind a corporate proxy

SPARQL Doctor uses the standard `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY` environment variables. Pip uses them too:

```bash
export HTTPS_PROXY=http://proxy.corp.example:3128
pip install git+https://github.com/germanbraun/SPARQL_Doctor.git
```

Use `--no-proxy` to make SPARQL Doctor ignore them and connect directly.

## For contributors

```bash
git clone https://github.com/germanbraun/SPARQL_Doctor.git
cd SPARQL_Doctor
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,http2]"      # or: pip install -r requirements.txt && pip install -e .
pytest                             # about 30 s, runs against a local fake endpoint and needs no network
```

`tests/fake_endpoint.py` starts a misbehaving SPARQL endpoint for manual experiments:

```bash
python tests/fake_endpoint.py --port 8890 --idle 4 --idle-rst --block-ua --max-url 4096 --cors
sparql-doctor http://127.0.0.1:8890/sparql --quick
```

Project conventions are in [CLAUDE.md](CLAUDE.md). Usage is documented in [README.md](README.md).

## Uninstall

```bash
pipx uninstall sparql-doctor        # or: pip uninstall sparql-doctor
```
