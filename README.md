# HullTrace

HullTrace is a maritime investigation web app For OSINT purposes. Enter a ship’s seven-digit IMO number to collect public records about its identity, former names, ownership and sanctions history in one lookup. Each report shows where the findings came from and when HullTrace retrieved them.

## Start in GitHub Codespaces

1. Extract the HullTrace ZIP and upload its **contents** to the root of your repository. On GitHub, you should see this README alongside `Maritim/`, `tests/` and `.devcontainer/`. Include `.gitignore` when uploading.
2. Open **Code → Codespaces** and create a codespace for your branch. The included configuration starts HullTrace automatically.
3. If the app does not open in a browser tab, open the **Ports** panel, find **HullTrace** on port **8765**, and choose **Open in Browser**. Keep the port visibility **Private**.

The ZIP excludes saved reports and caches. If you work from a local checkout instead, Git ignores those folders. You can keep them on your computer.

## Run on your computer

Use Python 3.12 or newer. HullTrace uses the Python standard library and public sources, so you can start it without installing packages or setting up API keys.

**Windows:** Double-click **Open HullTrace.cmd** in the extracted folder. Keep its window open while you work. The launcher opens [HullTrace on port 8767](http://127.0.0.1:8767/).

**From a terminal:** Open the folder containing this README and run the command below. It opens the app on port **8765** by default. Press `Ctrl+C` in that terminal to stop the server.

```sh
python -m Maritim.server
```

## Look up a vessel

1. Open **Vessel lookup**. Enter an IMO number, or select **Use example 9299898** to try the built-in example.
2. Select **Look up vessel**. HullTrace queries OpenSanctions, VesselFinder and the UK Sanctions List. It also follows a link to a GUR War & Sanctions vessel profile when one is available.
3. Review the findings and **Source coverage** together. Sources can disagree or be unavailable. An empty sanctions section alone cannot establish a vessel’s sanctions status. OpenSanctions data reuse is subject to its [licensing terms](https://www.opensanctions.org/licensing/).

Completed lookups appear under **Saved reports**. Search by ship name or IMO, then choose **Open report** for the HTML version or **Download JSON** for structured findings. Reports and source snapshots are stored in `Maritim/output/scans/`. Download reports you want to keep before deleting a codespace.

## Import historical records

Open **Historical records**, select a UTF-8 CSV up to **1 MB (1,048,576 bytes)**, then choose **View records**. Use this page to search your own dated observations. Imported rows stay in the current view until you leave or reload it.

Every row needs a `kind`, an `imo` and a `source` URL. Include the additional columns for its record type.

| Record type (`kind`) | Additional columns |
| --- | --- |
| `port` | `date`, `port` |
| `inspection` | `date`, `condition`, `deficiency` |
| `voyage` | `container`, `vessel` |
| `radio` | `month`, `mmsi` |

This fictional port visit shows the CSV format. Replace the example URL with the source for your observation. Use `YYYY-MM-DD` for dates, `YYYY-MM` for months and nine digits for an MMSI.

```csv
kind,imo,source,date,port
port,9299898,https://example.org/port-log,2025-04-18,Example Harbour
```

## Development

The web server is in `Maritim/server.py`, public-source collection in `Maritim/vessel_lookup.py`, CSV processing in `Maritim/historical_records.py`, and browser assets in `Maritim/web/`. Run the test suite from the repository root.

```sh
python -m unittest discover -s tests -v
```

For optional command-line case management, run `python -m Maritim.case_cli --help`. If the server fails to start in Codespaces, check `/tmp/hulltrace.log` for the startup error.
