"""
test_skill_search.py

Quick manual test harness for the skills vector store built by embed_skills.py.
Give it a query and it fetches the top 5 closest skills.

Usage:
    python test_skill_search.py                       # interactive prompt loop
    python test_skill_search.py "debug a firmware crash"   # one-off query
"""

from __future__ import annotations

import sys

from embed_skills import search

TOP_K = 5


def print_results(query: str) -> None:
    results = search(query, top_k=TOP_K)
    print(f"\nTop {TOP_K} matches for: {query!r}\n")
    for r in results:
        print(f"  {r['similarity']:.4f}  {r['id']:<25} {r['name']}")
        print(f"           {r['description']}\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        print_results(" ".join(sys.argv[1:]))
    else:
        print("Enter a query (blank line to quit):")
        while True:
            query = input("> ").strip()
            if not query:
                break
            print_results(query)
