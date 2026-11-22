"""A restricted exec() sandbox for running LLM-generated pandas code.

This is an *application-level* defense-in-depth layer only: it restricts
the names and builtins the generated code can see (no ``__import__``,
``open``, ``exec``, ``eval``, ``os``, ``sys``, network access, etc.) and
enforces a short wall-clock timeout so a buggy/malicious snippet (e.g. an
infinite loop) can't hang the service.

It is explicitly NOT a substitute for real OS-level isolation. Running
genuinely untrusted/adversarial code requires sandboxing at the pod level
(e.g. a gVisor/Kata ``runtimeClass`` on the Kubernetes Pod that runs this
service) -- that is expected to be configured in the deployment manifests,
outside this codebase. Treat everything below as "make a buggy LLM less
likely to break this process", not "withstand a malicious actor with code
execution".
"""
from __future__ import annotations

import threading
from typing import Any

import pandas as pd

# Builtins explicitly allowed inside the sandbox, enumerated as an
# allow-list (rather than "all of __builtins__ minus a blocklist") so that
# Python adding new builtins in the future can't silently widen what
# generated code can reach. Deliberately excludes __import__, open, exec,
# eval, compile, input, getattr/setattr/delattr, and anything else that
# could reach the filesystem, network, or process.
_SAFE_BUILTINS: dict[str, Any] = {
    "len": len,
    "sum": sum,
    "min": min,
    "max": max,
    "round": round,
    "abs": abs,
    "range": range,
    "enumerate": enumerate,
    "sorted": sorted,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "True": True,
    "False": False,
    "None": None,
}


class SandboxTimeoutError(Exception):
    """Raised when the generated code doesn't finish within the budget."""


class SandboxSession:
    """A restricted exec() namespace that persists across multiple calls to
    :meth:`run_step`, so a short *sequence* of LLM-generated pandas steps
    can build on each other -- step 1 might add a derived column, step 2
    sort/filter by it and assign the final answer to ``result`` -- without
    each step losing what the previous one computed.

    This backs the "plan -> execute step -> observe -> retry that step"
    loop in ``app/main.py``. It is still the same application-level
    defense-in-depth as a single-shot run (see module docstring): the
    builtins allow-list and per-step timeout are identical, just applied
    once per step instead of once per whole snippet.
    """

    def __init__(self, dataframes: dict[str, pd.DataFrame]) -> None:
        first_df = next(iter(dataframes.values()))
        self._locals: dict[str, Any] = {"pd": pd}
        if len(dataframes) == 1:
            self._locals["df"] = first_df
        else:
            self._locals["dfs"] = dataframes
            self._locals["df"] = first_df
        self._globals: dict[str, Any] = {"__builtins__": _SAFE_BUILTINS}

    def run_step(self, code: str, timeout_seconds: float = 5.0) -> None:
        """Execute one step's code against the session's accumulated
        namespace (previously-defined variables/columns are visible to it).

        Any exception raised by the code (SyntaxError, KeyError, etc.)
        propagates to the caller unchanged, so the caller can decide
        whether to regenerate and retry just this step. A timeout raises
        ``SandboxTimeoutError`` instead. If this step's code partially ran
        before raising, any side effects it had (e.g. a column it added
        before the failing line) are *not* rolled back -- acceptable here
        because a failed step is always fully regenerated on retry (see
        ``app/llm.py:regenerate_step``), never resumed from its partial
        state.
        """
        outcome: dict[str, Any] = {}

        def _target() -> None:
            try:
                compiled = compile(code, "<llm_generated_pandas_code>", "exec")
                exec(compiled, self._globals, self._locals)  # noqa: S102 -- sandboxed, see module docstring
            except BaseException as exc:  # noqa: BLE001 -- surface any failure to the caller as-is
                outcome["error"] = exc

        # Run in a worker thread (not signal.alarm) so this also works when
        # the FastAPI sync route handler itself is already running off the
        # main thread (Starlette's threadpool executor for `def` endpoints).
        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(timeout_seconds)

        if thread.is_alive():
            # The thread can't be force-killed from here (Python has no
            # safe primitive for that) and is left to run to completion in
            # the background. Accepted leak for this short-timeout,
            # low-concurrency defense-in-depth layer -- the real backstop
            # against a runaway/hostile snippet is pod-level isolation (see
            # module docstring), not keeping this process perfectly tidy.
            raise SandboxTimeoutError(f"Execution exceeded {timeout_seconds}s timeout")

        if "error" in outcome:
            raise outcome["error"]

    @property
    def result(self) -> Any:
        """The value currently bound to ``result`` in this session's
        namespace, or ``None`` if no step has set it (yet)."""
        return self._locals.get("result")


def run_pandas_code(
    code: str, dataframes: dict[str, pd.DataFrame], timeout_seconds: float = 5.0
) -> Any:
    """Single-shot convenience wrapper: run one snippet in a fresh
    ``SandboxSession`` and return its ``result``. Equivalent to a
    one-element plan; kept around for any single-step caller that doesn't
    need the multi-step machinery.
    """
    session = SandboxSession(dataframes)
    session.run_step(code, timeout_seconds=timeout_seconds)
    return session.result
