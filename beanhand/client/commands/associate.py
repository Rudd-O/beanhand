import argparse
import difflib
import json
import pprint
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from textwrap import indent
from traceback import print_exception
from typing import cast

from beanhand.client.beancount_loader import (
    MatchResults,
    load_transaction_contexts,
)
from beanhand.client.beanfiles import (
    FileGuard,
    FileModifiedError,
    predict_receipt_destination_path,
    update_document_metadata,
    write_beancount_file,
)
from beanhand.client.config import Configuration
from beanhand.client.display import print_diff
from beanhand.client.receipts import (
    ReceiptRef,
    Source,
    maybe_warn_collision,
)
from beanhand.client.server.ai import (
    AIClient,
    demarkdownify,
    stream_reasoning_and_capture_output,
)
from beanhand.client.server.documents import (
    DocumentsClient,
    preview_receipt,
    save_receipt,
)
from beanhand.structs import load_json


def run(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
    """Associate a receipt with an existing Beancount transaction.

    Flow:
      1. Process the receipt → get date + amount from LLM.
      2. Query Beancount for candidates within -1/+45 days.
      3. Send candidates to beanhand.MatchCandidates on server.
      4. If unambiguous, auto-select + update document metadata.
      5. If ambiguous, present ranked list to user.
      6. Organize the receipt file.
    """
    doc_vm = DocumentsClient.from_cfg(cfg)
    ai_vm = AIClient.from_cfg(cfg)

    # With filenames, each argument resolves independently (a local path or a
    # store name); a local path that does not exist fails fast, before any LLM
    # or transport call.  A store name is no longer pre-checked against the
    # listing — the fetch during processing is the single source of truth.
    # Without filenames, the batch is the store's unassociated queue (all STORE).
    # ``resolve`` of a list resolves each argument and de-duplicates (D7) in one
    # step, so a receipt named more than once is processed exactly once.
    if args.filename:
        refs = ReceiptRef.resolve(args.filename)
    else:
        refs = ReceiptRef.resolve(doc_vm.list_receipts("unassociated"))

    if not refs:
        print("No receipts to associate.", file=sys.stderr)
        return

    def do_associate_one(ref: ReceiptRef, preview_dir: Path) -> None:  # noqa: C901
        # Load the receipt's bytes (local read or store fetch).  A local file
        # that vanished between resolution and read surfaces here, before any
        # AI call (most commonly because an earlier receipt in this run moved it).
        try:
            fetched = ref.load(doc_vm)
        except FileNotFoundError as e:
            raise Exception(
                f"Association of {ref.arg} failed: file not found "
                "(already processed earlier in this run?)"
            ) from e

        maybe_warn_collision(doc_vm, "unassociated", ref.arg, ref)

        # Step 1: Process the receipt via LLM (existing flow).
        try:
            cmd, proc, stdin, stdout = ai_vm.help_associate_receipt(
                ref.filename, fetched
            )
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error processing {ref.arg}: {e}") from e

        llm_output = demarkdownify(stream_reasoning_and_capture_output(stdout))

        receipt_info = load_json(llm_output)

        try:
            receipt_date = datetime.strptime(receipt_info["date"], "%Y-%m-%d").date()
        except KeyError:
            receipt_date = None
        try:
            amt_cur = cast(str, receipt_info["amount"])
        except KeyError:
            amt_cur = None

        if receipt_date:
            print(f"Receipt date: {receipt_date.isoformat()}", file=sys.stderr)
        else:
            print("No date in receipt", file=sys.stderr)
        if amt_cur:
            print(f"Receipt amount: {amt_cur}", file=sys.stderr)

        # Step 2: Get candidates from Beancount.
        if receipt_date:
            start_date = receipt_date - timedelta(days=1)
            end_date = receipt_date + timedelta(
                days=45
            )  # 45 days ought to be good for receipts paid up to a month later

            try:
                _, contexts = load_transaction_contexts(
                    str(cfg.beancount.main_file), start_date, end_date
                )
            except Exception as e:
                raise Exception(f"Error loading candidates from Beancount: {e}") from e
        else:
            assert 0, "Date for receipt could not be deduced."

        print(f"Found {len(contexts)} candidate transactions.", file=sys.stderr)

        candidates_data = [
            ctx.__dict__ if hasattr(ctx, "__dict__") else ctx for ctx in contexts
        ]
        # Write candidates JSON to the server.
        candidates_raw = (json.dumps(candidates_data) + "\n").encode("utf-8")
        stdin.write(candidates_raw)
        stdin.flush()
        stdin.close()

        llm_output = demarkdownify(stream_reasoning_and_capture_output(stdout))
        stdout.close()

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        resp = cast(MatchResults, load_json(llm_output))

        matches = resp.get("matches", [])
        if not matches:
            print(f"No valid matches found for receipt {ref.arg}.", file=sys.stderr)
            return

        # Step 4 & 5: Interpret results.
        is_ambiguous = resp.get("ambiguous", False) or (
            len(matches) > 0 and matches[0].get("score", 0) < 0.8
        )

        if is_ambiguous:
            raise Exception(
                f"sorry, matches are ambiguous, cannot proceed; list of matches:\n{pprint.pformat(matches)}"
            )

            # Present ranked list to user.  This is dead code for now, but we will enable it in the future
            # when more testing has taken place.
            print("\nRanked candidates (select by index):", file=sys.stderr)
            for candidate_match in matches[:5]:  # show top 5
                print("candidate:", file=sys.stderr)
                print(pprint.pformat(candidate_match), file=sys.stderr)
            #     idx = candidate_match["index"]
            #     score = candidate_match.get("score", 0)
            #     ctx = contexts[idx]

            #     amount_str = (
            #         f"{ctx['paid_amount']} {ctx['paid_currency']}"
            #         if ctx.get("paid_amount")
            #         else "N/A"
            #     )
            #     payee = ctx.get("payee") or "N/A"
            #     narration = (ctx.get("narration") or "N/A").ljust(30)

            #     print(
            #         f"  [{idx}] score={score:.2f} {ctx['date_str']}  {str(payee).ljust(25)}  {narration}{amount_str:>15}",
            #         file=sys.stderr,
            #     )

            # while True:
            #     try:
            #         choice = input("\nSelect candidate (index, or 'r' to retry): ")
            #         if choice.strip().lower() == "r":
            #             print(
            #                 "Retrying...", file=sys.stderr
            #             )  # TODO: actually reload and re-match
            #             pass  # stay in loop

            #         selected_idx = int(choice.strip())
            #         if 0 <= selected_idx < len(contexts):
            #             is_ambiguous = False
            #             break
            #         else:
            #             print(
            #                 f"Invalid index. Use [0-{len(contexts) - 1}].", file=sys.stderr
            #             )
            #     except (ValueError, EOFError):
            #         print("Please enter a valid number.", file=sys.stderr)
            return

        selected_match_index = 0
        selected_match = matches[selected_match_index]

        selected_txes = [
            tx
            for tx in contexts
            if tx.line_no == selected_match["line_no"]
            and tx.source_file == selected_match["source_file"]
        ]

        if len(selected_txes) > 1:
            raise Exception(
                f"Multiple transactions fit selected transaction match produced by LLM: {selected_match}"
            )
        elif not selected_txes:
            raise Exception(
                f"No transactions fit selected transaction match produced by LLM: {selected_match}"
            )

        selected_tx = selected_txes[0]

        # # We won't be generating descriptions for now.
        # description: str | None = None
        if selected_tx.narration and selected_tx.narration not in ["EFT payment"]:
            description = selected_tx.narration
        elif selected_tx.payee:
            description = selected_tx.payee
        else:
            description = None

        if amt_cur:
            if description is None:
                description = amt_cur
            else:
                description = description + f", {amt_cur}"

        # Step 6: Download + organize receipt.
        receipt_path = predict_receipt_destination_path(
            cfg.beancount.main_folder,
            receipt_date,
            ref.filename,
            selected_tx.crediting_account,
            description=description,
        )

        # Step 7: Update document metadata.
        tx_file = Path(selected_tx.source_file)
        line_no = selected_tx.line_no

        if not tx_file.exists():
            raise Exception(
                f"Warning: Transaction source file '{tx_file}' does not exist. Cannot update metadata."
            )

        # Read the transaction text and update it.  Snapshot the content so we
        # can detect (and abort on) external edits made while we were waiting,
        # instead of clobbering them at write time.
        all_lines = tx_file.read_text(encoding="utf-8").splitlines(True)
        tx_file_guard = FileGuard.take(tx_file)

        if line_no > len(all_lines):
            raise Exception(
                f"Error: line number {line_no} exceeds file length ({len(all_lines)})."
            )

        new_content = update_document_metadata(
            line_no,
            all_lines,
            str(receipt_path),
        )

        old_lines = all_lines
        new_lines_txt = (
            new_content.rstrip("\n") + "\n"
            if not new_content.endswith("\n")
            else new_content
        )
        new_lines = new_lines_txt.splitlines(True)

        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=str(tx_file),
                tofile=str(tx_file),
                n=5,
            )
        )
        if diff:
            print_diff(diff)
        else:
            print(f"No changes to {receipt_path}", file=sys.stderr)
            return

        if not args.no and not args.yes:
            while True:
                print(
                    f"\nSave proposed changes to '{tx_file}' and import {ref.arg}? [y]es / [n]o / [p]review receipt / [q]uit ",
                    file=sys.stderr,
                    end="",
                )
                try:
                    answer = input().strip().lower()
                except EOFError:
                    return

                if answer == "q":
                    sys.exit(0)

                if answer == "p":
                    preview_receipt(fetched, preview_dir)
                    continue  # re-prompt for the same receipt

                if answer != "y":
                    return

                break  # leave prompt loop after y or n

        if args.no:
            print(f"Skipping changes to {tx_file} (--no requested)", file=sys.stderr)
            return

        # The transaction file may have changed while we waited (e.g. an LLM
        # call or the user's editor).  If so, the matched line and our edit are
        # both based on stale content, so back out before writing anything
        # (including the new receipt file).
        try:
            tx_file_guard.verify()
        except FileModifiedError as e:
            raise Exception(f"Refusing to associate: {e}") from e

        # Save the (already-fetched) receipt organized.
        save_receipt(receipt_path, fetched)

        print(f"Receipt saved to {receipt_path}", file=sys.stderr)

        write_beancount_file(tx_file, new_content)
        print(
            f"Updated document metadata on line {line_no} of {tx_file}", file=sys.stderr
        )

        # Consume the receipt from wherever it came from.  A store receipt is
        # removed from the server; a local source file is moved into the account
        # folder (its organized copy was just written, with the source's mtime).
        # associate writes iff not --no and the match was confirmed, which is
        # exactly where we are, so no further gating is needed.
        if ref.src is Source.STORE:
            doc_vm.remove_receipt(ref.filename)
        else:
            ref.path.unlink()

    with tempfile.TemporaryDirectory() as tmpdir:
        preview_dir = Path(tmpdir)

        exceptions: list[tuple[str, Exception]] = []
        for ref in refs:
            try:
                do_associate_one(ref, preview_dir)
            except Exception as e:
                exceptions.append((ref.arg, e))
                if args.yes or args.no:
                    print(f"{e} — continuing to next receipt", file=sys.stderr)
                else:
                    raise
        if exceptions:
            # Can only get here when not in batch mode.
            print("Summary of errors encountered:", file=sys.stderr)
            for f, exc in exceptions:
                print(f"* {f}:", file=sys.stderr)
                capt = StringIO()
                print_exception(exc, file=capt)
                capt.seek(0)
                print(f"{indent(capt.read(), '    ')}", file=sys.stderr)
            sys.exit(1)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    assoc_cmd = sp.add_parser(
        "associate", help="Associate a receipt with an existing Beancount transaction"
    )
    assoc_cmd.add_argument(
        "filename",
        help="One or more receipt filenames (documents store) or paths to local files (if none are present, all store receipts are processed)",
        nargs="*",
    )
    yes_group = assoc_cmd.add_mutually_exclusive_group()
    yes_group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help="Make changes without confirmation (equivalent to answering 'yes' to every prompt)",
    )
    yes_group.add_argument(
        "--no",
        "-n",
        action="store_true",
        default=False,
        dest="no",
        help="Show the changes that would be made but make none (equivalent to answering 'no' to every prompt)",
    )
    return sp
