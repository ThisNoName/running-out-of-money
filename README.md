# mny2qif / qif2kmy

Moves a Microsoft Money `.mny` file to KMyMoney, in two steps and without
KMyMoney's import dialog. Pure Python 3, no third-party packages.

```
python mny2qif.py ms_money.mny -o out  ms_money_output    # .mny  -> one QIF per account
python qif2kmy.py ms_money_putput out km_money.kmy        # QIFs  -> km_money.kmy
```

## Files

| File | Purpose |
| --- | --- |
| `jet4.py` | Read-only Jet4/MSISAM reader: pages, table definitions, row decoding |
| `money.py` | Money schema interpretation and QIF rendering |
| `mny2qif.py` | `.mny` -> QIF command line entry point |
| `qif.py` | QIF parsing |
| `kmymoney.py` | KMyMoney book model and XML writer |
| `qif2kmy.py` | QIF -> `.kmy` command line entry point |
| `test_qif2kmy.py` | `python -m unittest discover -s src` |

# mny2qif

```
python mny2qif.py FILE.mny [-o OUTDIR] [--prefix NAME] [--list]
                  [--skip-closed] [--no-manifest]
```

One QIF file is written per account (matching Money's own "loose QIF" export),
named `<prefix>_<account name>.qif`; `--prefix` defaults to the `.mny` base
name. `--list` prints the accounts without writing anything.

Alongside them goes `<prefix>_accounts.json`, holding the one account fact QIF
has no field for: whether Money had the account closed. See *Closed accounts*
below. `--no-manifest` suppresses it.

## How it reads the file

A `.mny` file is a Jet 4 database with an `MSISAM Database` signature, and it is
always encrypted — which is why mdbtools and the usual Access readers fail on it.
Money only encrypts **pages 1–14**, the system catalogue. Every table definition
and every data page belonging to the application tables lives at page 15 or
beyond and is stored in the clear, so no key is needed.

The cost is that `MSysObjects` is unreadable, so tables cannot be looked up by
name. Instead each table is identified by its **column signature** —
`find_table(['hacct', 'szFull', 'at'])` locates the account table, and so on.

Two details of the format are worth knowing if you modify this code:

* Money declares almost every non-boolean column as *variable length*, so most
  values are reached through the row's variable-column offset table rather than
  at a fixed offset. Booleans have no payload at all; the null-mask bit is the
  value.
* A row that outgrew its page is replaced in place by a 4-byte **overflow
  pointer** (slot flag `0x4000`) to a row on another page, and that target row
  is marked deleted in its own page so a plain scan skips it. Missing this costs
  you whole accounts — in the test file, "Presidential Adv Checking" is stored
  this way.

## Money-specific export rules

* `ACCT.at` 0 is a bank account (`!Type:Bank`) and 1 is a credit card
  (`!Type:CCard`). Other account types are reported on stderr and skipped.
* Each file opens with the synthetic opening-balance record Money emits: amount
  `ACCT.amtOpen`, flagged `CX`, payee `Opening Balance`, categorised as a
  transfer to the account itself. Its date is the earliest date the account has
  history for (`ACCT.dtOpen`, or the first transaction if that is earlier).
* A transaction with `TRN.hacctLink` set is a transfer and is written as
  `L[Other Account]`; otherwise `TRN.hcat` is resolved to a full
  `Parent:Child` category path with the top-level `INCOME`/`EXPENSE` root
  dropped.
* Scheduled recurring bills are stored in `TRN` as templates but never appear in
  a register. They are excluded, identified by the `0x200000` bit of
  `TRN.grftt`, by a null `hacct`, or by being referenced from `BILL.lHtrn`.
* Splits are child `TRN` rows linked through `TRN_SPLIT` and are emitted as
  `S`/`E`/`$` lines.

# qif2kmy

```
python qif2kmy.py QIF_OR_DIR... [-o FILE.kmy] [--list] [--plain]
                  [--currency USD] [--income CAT] [--expense CAT]
                  [--closed ACCOUNT] [--manifest FILE] [--no-manifest]
                  [--entry-date YYYY-MM-DD] [--file-id UUID]
```

Takes the QIF files, or directories of them, and writes one KMyMoney file —
gzipped XML, which is what KMyMoney itself writes; `--plain` leaves it
uncompressed (KMyMoney reads either). The default output name is the inputs'
common prefix, so `qif2kmy.py out` on the files above gives `km_money.kmy`.
`--list` prints the chart of accounts it derived and exits.

## Reading the account out of a QIF file

QIF has no reliable account header, so the account each file describes comes
from the leading opening-balance record: `POpening Balance` categorised as a
transfer to itself names the account, and supplies its opening date and
balance. That record is consumed rather than imported; `Opening Balance` does
not become a payee. A non-zero balance becomes a transaction against an
`Opening Balances` equity account, which is how KMyMoney represents one.

`!Type:Bank` becomes a checking account under `Asset`, `!Type:CCard` a credit
card under `Liability`, and `Cash`/`Oth A`/`Oth L` the corresponding types.

## Why both QIF files are read before any transfer is resolved

Money writes each transfer into both registers, as `L[Other Account]` on each
side. Two things follow, and both are why this tool exists rather than the
import dialog:

* **Every account is registered first, from its own file.** Importing the files
  one at a time means `L[Citi Credit]` in the checking register is resolved
  before the credit card's own file has been read, so there is nothing to
  resolve it to and a new account gets invented. In the reference import this
  left two `Citi Credit` accounts — an asset holding +60.00 and the real credit
  card — where the QIF files describe one.
* **The two halves of a transfer are matched and merged into one transaction**
  with one split per side, each keeping its own cleared flag. Same date, same
  accounts, opposite amounts; repeated identical transfers on one day pair off
  one for one. A half with no counterpart is still written as a balanced
  two-split transaction. In the reference import the 60.00 credit card payment
  was imported from both registers and never matched, so checking came out as
  1403.11 instead of 1463.11.

## Closed accounts

A QIF file describes a register, not an account, so there is nowhere in it to
say that Money had the account closed — the flag would be lost between the two
steps. So `mny2qif` writes it beside the QIF files instead, in
`<prefix>_accounts.json`:

```json
{"generator": "mny2qif", "source": "ms_money.mny",
 "accounts": [{"name": "12 month CD",
               "qif": "ms_money12_month_CD.qif", "closed": true}]}
```

`qif2kmy` picks up any `_accounts.json` beside the QIF files it is given,
reports which it read, and closes the accounts named there by writing the
`mm-closed` key/value pair KMyMoney uses:

```xml
<ACCOUNT ... name="12 month CD" opened="2022-06-06">
 <KEYVALUEPAIRS>
  <PAIR value="2022-06-06" key="lastImportedTransactionDate"/>
  <PAIR value="yes" key="mm-closed"/>
 </KEYVALUEPAIRS>
</ACCOUNT>
```

Keeping it out of band is what lets the QIF files stay byte-for-byte what Money
itself would have exported. For QIF from anywhere else, `--closed ACCOUNT`
names them on the command line (repeatable, case-insensitive, and it warns if a
name matches nothing). `--manifest FILE` points at a sidecar explicitly and
`--no-manifest` ignores them.

Closing an account changes nothing about its register: its transactions and
balance are imported either way, and KMyMoney just hides it from the account
list by default.

`ACCT.fClosed` and `ACCT.dtClose` both count as closed — Money sets the flag
and stamps the date, and either one on its own is enough. Every exportable
account is listed in the manifest, including ones `--skip-closed` left out of
the QIF export, so that an omitted account still named by a transfer is created
closed rather than quietly reopened.

## Income or expense

QIF does not say which a category is, so the sign decides, as KMyMoney's own
importer does: money arriving in the account came from income, money leaving it
went to an expense. A category used both ways follows the majority of its uses
and says so on stderr; `--income` and `--expense` override it. `Parent:Child`
paths become a tree of accounts, with shared parents created once.

## Verification

`test_qif2kmy.py` covers the paths the test file does not reach — split
transactions, non-zero opening balances, memos, check numbers, cleared flags,
one-sided transfers, mixed-use categories and XML escaping — and asserts on the
four real QIF files that every transaction balances to zero and that the closing
balances are the ones the registers imply.

The closed-account path is covered by tests but not end to end: no account in
`ms_money.mny` is actually closed (`fClosed` and `dtClose` are both unset on
all four), so the flag has nothing to carry there. The markup it produces is the
same as in `ms_moneywith_closed_act.kmy`, where the CD was closed by hand in
KMyMoney.

Against `km_money.kmy`, the hand-made import of the same four files, the
generated XML differs only in:

* account and transaction numbering, there being one `Citi Credit` rather than
  two, and six transactions rather than seven;
* `bankid` import ids, which are ours rather than a reproduction of
  KMyMoney's hash, and the `description` naming the source file;
* no `kmm-match-split`/`kmm-matched-tx` pairs: a merged transfer is written as
  one transaction instead of KMyMoney's matched pair, which keeps the absorbed
  copy around so it can be unmatched later.

## Limitations

* The QIF `C` mapping (`*` → cleared, `X` → reconciled) follows the format but
  is not confirmed against a KMyMoney import, because every cleared record in
  the test file is an opening-balance record, which is consumed.
* Dates are read month-first. Money writes `M/D'YYYY` and nothing else.
* Transfers only pair when at least one side is a plain record. If two split
  transactions name each other, both are kept and the transfer is counted
  twice. Money writes the counterpart of a split transfer leg as a plain
  record, so this needs hand-written QIF to hit.
* Investment accounts, securities, prices and schedules are not handled;
  mny2qif does not export them either.
* Quicken classes (`L Category/Class`) are dropped, and every account is given
  the same currency.
* A transfer to an account that no input file declares is created as an asset
  account, with a warning. Exporting that account too is the better fix.
