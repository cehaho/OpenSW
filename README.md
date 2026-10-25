# openSW

Convert SolidWorks part files (`.SLDPRT`) to Parasolid (`.x_t`) and STEP,
without SolidWorks.

## Install

```sh
git clone https://github.com/Emilien-Etadam/OpenSW
cd OpenSW
pip install -e ".[occ]"
```

Python 3.10+. The `occ` extra (OpenCASCADE) is only needed for the 3D view of
the web app.

## Convert

```sh
python tools/convert.py part.SLDPRT -o out/    # .x_t and .STEP
python tools/convert.py --help
```

## Web app

```sh
python tools/webui.py                          # http://127.0.0.1:8765
```

Upload a part, download the `.x_t` or STEP, and view it in 3D. Uploads are
deleted after conversion, results after 30 minutes. `PORT` and `OPENSW_HOST`
set the address; on a public server, put an authenticating reverse proxy in
front.

## Parasolid schemas

Not included: they are Siemens files. Built-in facts cover SolidWorks parts.
If one is asked for, copy `pschema` and `schema_18` from your SolidWorks
installation into `schemas/`.

## Legal

Interoperability work on files the author owns. This repository contains no
SolidWorks or Parasolid code. AGPL-3.0-only, see [`LICENSE`](LICENSE) and
[`NOTICE`](NOTICE).
