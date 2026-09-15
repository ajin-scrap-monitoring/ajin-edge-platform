"""Abort publication when an existing GHCR package is not private or unverifiable."""

import json
import os
import sys
import urllib.error
import urllib.request

from release_metadata import SERVICES


def main():
    service = sys.argv[1]
    if service not in SERVICES:
        raise SystemExit("unsupported service")
    request = urllib.request.Request(
        "https://api.github.com/orgs/ajin-scrap-monitoring/packages/container/ajin-" + service,
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            package = json.load(response)
    except urllib.error.HTTPError:
        # 404 can mean missing or inaccessible: require explicit private provisioning.
        raise SystemExit("cannot verify private package access") from None
    if package.get("visibility") != "private":
        raise SystemExit("publication blocked: package is not private")


if __name__ == "__main__":
    main()
