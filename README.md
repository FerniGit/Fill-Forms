# First Touch Rate Discrepancy Helper

This repo contains the `fill_form.py` tool we use to gather shipment/package details and rates from ShipperHQ logs to complete the First Touch Rate Discrepancy form.

## Quick start (support agents)

1) Install/check Python 3:
   ```bash
   python3 --version   # should print 3.x
   ```
   If it’s missing, install Python 3 from python.org or your package manager, then rerun the version check.

2) Pick or create a folder to hold the tool (anywhere is fine), then clone the repo:
   ```bash
   git clone https://github.com/FerniGit/Fill-Forms.git
   cd /path/to/Fill-Forms   # replace with where you cloned
   ```
3) Install deps if needed:
   ```bash
   pip install -r requirements.txt
   ```
4) From the ops-tools directory, link the helper scripts needed by `fill_form.py` (so it can auto-fetch logs by transaction ID):
   ```bash
   cd /path/to/ops-tools           # the folder that has findlog.sh, split_logs.sh, log_trimmer.sh
   ln -s "$(pwd)/findlog.sh"      /path/to/Fill-Forms/findlog.sh
   ln -s "$(pwd)/split_logs.sh"   /path/to/Fill-Forms/split_logs.sh
   ln -s "$(pwd)/log_trimmer.sh"  /path/to/Fill-Forms/log_trimmer.sh
   chmod +x "$(pwd)/findlog.sh" "$(pwd)/split_logs.sh" "$(pwd)/log_trimmer.sh"
   ```
   Replace `/path/to/Fill-Forms` with where you cloned in step 2.
5) (Optional) Add the Fill-Forms folder to your PATH so you can run `fill_form.py` from anywhere:
   ```bash
   echo 'export PATH="/path/to/Fill-Forms:$PATH"' >> ~/.zshrc
   source ~/.zshrc
   ```

## Running the tool

With a log file you already have:
```bash
fill_form.py shipperws.log
```

If you run it with no arguments, it will pop a file picker so you can select a log.

Output: a `rate_analysis_<timestamp>.txt` is written next to the log file you processed. This text file has the ship-from/ship-to, cart items, returned carrier services, and the per-box packing list you need for the First Touch Rate Discrepancy form.

To update the tool later (grab latest fixes):
```bash
cd /path/to/Fill-Forms
git pull
```

## Notes

- Use ShipperWS logs when possible (they include both request and response JSON).
- If you see a parsing error, double-check you selected the right log file and that it contains the request/response blocks.
