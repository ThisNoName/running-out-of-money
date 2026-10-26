#!/usr/bin/env python3
"""
mny2qif - export banking and credit card accounts from a Microsoft Money
(.mny) file to QIF.

Each exported account is written to its own QIF file, which is how Money's
own "loose QIF" export behaves.

Usage:
    python mny2qif.py FILE.mny [-o OUTDIR] [--list] [--prefix NAME]
"""

import argparse
import json
import os
import sys

from jet4 import JetDatabase
from money import MoneyFile, ConversionError


def build_parser():
    p = argparse.ArgumentParser(
        prog="mny2qif",
        description="Export banking and credit card accounts from a "
                    "Microsoft Money (.mny) file to QIF.")
    p.add_argument("mny", metavar="FILE.mny", help="Microsoft Money file to read")
    p.add_argument("-o", "--out-dir", default=".",
                   help="directory to write the QIF files into (default: .)")
    p.add_argument("--prefix",
                   help="filename prefix for the QIF files "
                        "(default: the .mny file's base name)")
    p.add_argument("-l", "--list", action="store_true",
                   help="list the accounts and exit without writing anything")
    p.add_argument("--include-closed", action="store_true", default=True,
                   help="include closed accounts (default)")
    p.add_argument("--skip-closed", dest="include_closed", action="store_false",
                   help="omit accounts marked closed in Money")
    p.add_argument("--no-manifest", dest="manifest", action="store_false",
                   help="do not write the <prefix>_accounts.json sidecar that "
                        "tells qif2kmy which accounts are closed")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        db = JetDatabase(args.mny)
        mny = MoneyFile(db)
    except (OSError, ValueError, ConversionError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1

    for w in mny.warnings:
        sys.stderr.write("warning: %s\n" % w)

    accounts = mny.exportable_accounts()
    if not args.include_closed:
        accounts = [a for a in accounts if not a.closed]

    for a in mny.skipped_accounts():
        sys.stderr.write("skipping %r: %s accounts cannot be exported as QIF\n"
                         % (a.name, a.type_name))

    if args.list:
        for a in accounts:
            print("%-40s %-12s %5d transactions%s"
                  % (a.name, a.type_name, len(a.transactions),
                     "  (closed)" if a.closed else "")
                  + ("  [%s]" % a.institution if a.institution else ""))
        return 0

    if not accounts:
        sys.stderr.write("error: no banking or credit card accounts to export\n")
        return 1

    stem = args.prefix or os.path.splitext(os.path.basename(args.mny))[0]
    os.makedirs(args.out_dir, exist_ok=True)

    for a in accounts:
        path = os.path.join(args.out_dir, MoneyFile.filename_for(a, stem))
        # newline="" keeps the CRLF line endings that QIF files use.
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(mny.account_qif(a))
        print("%s  (%s, %d transactions%s)"
              % (path, a.type_name, len(a.transactions),
                 ", closed" if a.closed else ""))

    if args.manifest:
        path = os.path.join(args.out_dir, MoneyFile.manifest_name(stem))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(mny.manifest(stem, os.path.basename(args.mny)), fh,
                      indent=1)
            fh.write("\n")
        print("%s  (closed-account flags for qif2kmy)" % path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
