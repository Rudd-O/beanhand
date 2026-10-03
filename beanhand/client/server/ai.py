"""Client-side access to the AI server (beanhand-ai-server).

This is the program that talks to the LLM.  It runs on the VM named by the
``ai.vm`` config key, or locally when that key is absent.  It never touches the
receipt storage: when an operation needs a document, the caller fetches it from
the documents server and relays it here inline over stdin.
"""

import subprocess
import sys
from typing import IO, ClassVar

from colorama import Fore, Style  # type: ignore

from beanhand.client.server.transport import ServerTransport
from beanhand.structs import (
    AccountRef,
    AssociateRequest,
    FetchedReceipt,
    ProcessRequest,
    ProcessResponse,
    ReceiptPayload,
    RefineRequest,
    RefineResponse,
    load_json,
)


def stream_reasoning_and_capture_output(stdout: IO[bytes]) -> str:
    accumulated: list[str] = []

    reasoning_over = False
    for line in stdout:
        msg = load_json(line)

        if msg.get("finish"):
            break
        elif "reasoning" in msg:
            sys.stderr.write(Fore.CYAN)
            sys.stderr.write(msg["reasoning"])
            sys.stderr.write(Style.RESET_ALL)
            sys.stderr.flush()
        elif "output" in msg:
            if not reasoning_over:
                sys.stderr.write("\n")
                sys.stderr.flush()
                reasoning_over = True
            accumulated.append(msg["output"])
        else:
            assert 0, msg

    return "".join(accumulated).strip()


def demarkdownify(llm_output: str) -> str:
    llm_output_lines = llm_output.splitlines(True)
    if llm_output_lines[0].startswith("```"):
        llm_output_lines = llm_output_lines[1:]
    if llm_output_lines[-1].startswith("```"):
        llm_output_lines = llm_output_lines[:-1]
    return "".join(llm_output_lines)


class AIClient(ServerTransport):
    """Relay client subcommands to the AI server."""

    program: ClassVar[str] = "beanhand-ai-server"
    target_vm_key: ClassVar[str] = "ai_target_vm"

    def process_receipt(
        self,
        filename: str,
        fetched: FetchedReceipt,
        account_refs: list[AccountRef],
    ) -> ProcessResponse:
        """Ask the AI server to turn one receipt into a Beancount transaction.

        The (already-fetched) receipt is relayed to the AI server inline over
        stdin, since the AI server has no access to the receipt storage.
        """
        receipt = ReceiptPayload(filename=filename, content=fetched.data)
        request = ProcessRequest(accounts=account_refs, receipt=receipt)

        cmd, proc, stdin, stdout = self._call("beanhand.Process")
        stdin.write(request.serialize().encode("utf-8"))
        stdin.close()

        llm_output_original = stream_reasoning_and_capture_output(stdout)
        stdout.close()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        # Remove Markdown quote formatting from JSON output.
        llm_output = demarkdownify(llm_output_original)

        # Fish out first account in the payment accounts list.
        try:
            return ProcessResponse.deserialize(llm_output)
        except Exception as e:
            raise Exception(
                f"Error interpreting LLM response: {e}\nOriginal LLM output:\n{llm_output_original}",
            ) from e

    def help_associate_receipt(
        self,
        filename: str,
        fetched: FetchedReceipt,
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """Start an AI-server association run and write the receipt to its stdin.

        The (already-fetched) receipt is relayed to the AI server inline as the
        first stdin line; the AI server has no access to the receipt storage.
        The caller still writes the candidate transactions as the second stdin
        line and reads the streamed response from the returned stdout handle,
        so the communication logic stays with the caller.
        """
        receipt = ReceiptPayload(
            filename=filename,
            content=fetched.data,
        )
        request = AssociateRequest(receipt=receipt)

        cmd, proc, stdin, stdout = self._call("beanhand.HelpAssociateReceipt")
        stdin.write((request.serialize() + "\n").encode("utf-8"))
        stdin.flush()
        return cmd, proc, stdin, stdout

    def refine(self, req: RefineRequest) -> RefineResponse:
        """Send one refine pass to the AI server and return its response.

        The ``req`` is written to the server's stdin, the streamed LLM output is
        captured, and the deserialized ``RefineResponse`` is returned.  Raises
        ``subprocess.CalledProcessError`` when the server exits non-zero and
        ``Exception`` when the LLM output cannot be parsed.
        """
        cmd, proc, stdin, stdout = self._call("beanhand.Refine")
        stdin.write(req.serialize().encode("utf-8"))
        stdin.flush()
        stdin.close()

        llm_output_original = stream_reasoning_and_capture_output(stdout)
        stdout.close()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        # 6. Parse the response — strip any markdown fences, then parse JSON.
        llm_output = demarkdownify(llm_output_original).strip()

        try:
            return RefineResponse.deserialize(llm_output)
        except Exception as e:
            raise Exception(
                f"Error interpreting LLM response: {e}\nOriginal LLM output:\n{llm_output_original}"
            )
