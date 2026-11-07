"""Trace a bitmap into an SVG with VTracer. Runs inside the sandbox."""

import sys

import vtracer


def main():
    source, output = sys.argv[1:3]
    vtracer.convert_image_to_svg_py(source, output)


main()
