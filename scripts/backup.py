#!/usr/bin/env python3
"""Create a consistent online backup of the ECO Bee SQLite database."""
import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument("--database",default="ecobee.db")
parser.add_argument("--output-dir",default="backups")
args=parser.parse_args()

source=Path(args.database).resolve()
output_dir=Path(args.output_dir).resolve()
output_dir.mkdir(parents=True,exist_ok=True)
target=output_dir/f"ecobee-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.db"
with sqlite3.connect(source) as src,sqlite3.connect(target) as dst:
    src.backup(dst)
    result=dst.execute("PRAGMA integrity_check").fetchone()[0]
if result!="ok":
    target.unlink(missing_ok=True)
    raise SystemExit("Backup integrity check failed")
print(target)
