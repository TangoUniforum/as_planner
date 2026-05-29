"""Repo-root CLI shim: python run.py"""
from forecast.run import _cli

if __name__ == "__main__":
    raise SystemExit(_cli())
