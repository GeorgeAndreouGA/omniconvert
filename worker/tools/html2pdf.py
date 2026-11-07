"""HTML to PDF, with no outside fetching at all. Runs inside the sandbox.

By default WeasyPrint follows file:// links in images and stylesheets, which would let a page read
local files into its PDF. Only data: URLs embedded in the page itself are allowed through.
"""

import sys

from weasyprint import HTML, default_url_fetcher


def fetcher(url, *args, **kwargs):
    if url.startswith("data:"):
        return default_url_fetcher(url, *args, **kwargs)
    raise ValueError("outside resources are not fetched")


def main():
    source, output = sys.argv[1:3]
    with open(source, "rb") as fh:
        HTML(file_obj=fh, url_fetcher=fetcher, base_url="data:,").write_pdf(output)


main()
