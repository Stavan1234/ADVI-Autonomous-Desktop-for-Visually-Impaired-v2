from __future__ import annotations


def print_banner() -> None:
    print("Advi 0.1.0")
    print("Foundation runtime online. Intelligence modules are not enabled yet.")


def read_line() -> str:
    return input("\nYou: ").strip()


def print_shutdown() -> None:
    print("Advi: Shutting down.")
