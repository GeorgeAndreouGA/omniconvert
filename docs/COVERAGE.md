# Format coverage

Generated from real conversions on the running service by `scripts/coverage.py --all`, which runs every
conversion the catalog offers from every test file. Regenerate it after changing engines.

- **Run on:** x86_64, Landlock off (one job at a time), as on Raspberry Pi OS
- **Formats in the catalog:** 915
- **Test files:** 165, each a real file of its format
- **Conversions offered from them, and all run:** 9,050
- **Passed:** 8,967. **Correctly reported no text to extract:** 20. **Failed even after a retry, and so blocked:** 63
- **Target formats reached:** 415

Each file is only offered formats of its own kind (see the README). Formats with no test file here are
offered because the tool that handles them says it supports them, within that rule, and haven't been run
in this test. A rare format the tool lists can still fail on a real file.

A conversion that passed produced a non-empty file that passed the type check. About 50 common targets
are checked for their exact type. For the rest, the check only catches output that's plainly some
other common format. Nobody looked at the content, so this says the conversion works, not that the
result looks perfect.

## By grade, of the conversions that passed

| Grade | Count |
|---|---|
| exact | 2,418 |
| lossy | 3,715 |
| extract | 798 |
| package | 2,036 |

## By engine

| Engine | Passed | Failed |
|---|---|---|
| Archive tools | 152 | 0 |
| Assimp | 71 | 1 |
| Calibre | 431 | 1 |
| DCMTK | 117 | 1 |
| DjVuLibre | 33 | 0 |
| Encode | 330 | 0 |
| ExifTool | 165 | 0 |
| FFmpeg | 1,350 | 10 |
| FluidSynth | 35 | 2 |
| FontForge | 16 | 0 |
| GDAL | 119 | 11 |
| Ghostscript | 39 | 2 |
| Graphviz | 93 | 1 |
| ImageMagick | 2,403 | 19 |
| Inkscape | 103 | 8 |
| LibreDWG | 2 | 1 |
| LibreOffice | 764 | 10 |
| Open Babel | 437 | 7 |
| OpenSCAD | 13 | 0 |
| Package | 1,706 | 0 |
| Poppler | 129 | 1 |
| Potrace | 3 | 0 |
| Settings files | 232 | 6 |
| Strings | 145 | 0 |
| Tables | 419 | 8 |
| Tika | 2 | 0 |
| VTracer | 18 | 2 |
| WeasyPrint | 27 | 0 |
| XeLaTeX | 3 | 0 |
| chdman | 24 | 0 |
| dvisvgm | 1 | 0 |
| fontTools | 21 | 0 |
| libheif | 26 | 0 |
| libjxl | 141 | 10 |
| librsvg | 89 | 0 |
| libvips | 247 | 1 |
| ncdump | 1 | 0 |
| ncgen | 1 | 0 |
| pandoc | 858 | 1 |
| qemu-img | 42 | 0 |
| resvg | 74 | 0 |
| samtools | 2 | 0 |

## Blocked: failed even after a retry

These are in `worker/known_bad_pairs.json`, and the catalog never offers them. Most are limits of a tool
with this particular test file, such as an Open Babel crash on one molecule, or a layered picture that
comes out as several images.

| From | To | Route | Message |
|---|---|---|---|
| `dwg` | `step` | LibreDWG > Assimp | Assimp could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `emf` | `cur` | LibreOffice > ImageMagick | ImageMagick could not convert this file. It may be damaged, password-protected, or use a feature it  |
| `emf` | `ico` | LibreOffice > ImageMagick | ImageMagick could not convert this file. It may be damaged, password-protected, or use a feature it  |
| `emf` | `rgf` | LibreOffice > ImageMagick | ImageMagick could not convert this file. It may be damaged, password-protected, or use a feature it  |
| `eps` | `exr` | ImageMagick > libjxl > libjxl | libjxl could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `eps` | `rgf` | ImageMagick | ImageMagick could not convert this file. It may be damaged, password-protected, or use a feature it  |
| `fits` | `emf` | ImageMagick > Inkscape | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `fits` | `fodg` | ImageMagick > Inkscape > LibreOffice | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `fits` | `odg` | ImageMagick > Inkscape > LibreOffice | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `fits` | `wmf` | ImageMagick > Inkscape | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `gif` | `exr` | libjxl > libjxl | libjxl could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `gpx` | `dgn` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `gpx` | `fgb` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `gpx` | `gdb` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `gpx` | `geojson` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `gpx` | `gmt` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `gpx` | `jml` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `gpx` | `mif` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `gv` | `exr` | Graphviz > libjxl > libjxl | libjxl could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `hdr` | `jp2` | libvips | libvips could not convert this file. It may be damaged, password-protected, or use a feature it does |
| `jpg` | `exr` | libjxl > libjxl | libjxl could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `json` | `dta` | Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `json` | `sqlite` | Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `kml` | `gdb` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `kml` | `gpx` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `mid` | `mmf` | FluidSynth > FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `mid` | `rso` | FluidSynth > FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `mkv` | `dv` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `mol2` | `gro` | Open Babel | Open Babel could not convert this file. It may be damaged, password-protected, or use a feature it d |
| `mol2` | `mcif` | Open Babel | Open Babel could not convert this file. It may be damaged, password-protected, or use a feature it d |
| `mol2` | `mmcif` | Open Babel | Open Babel could not convert this file. It may be damaged, password-protected, or use a feature it d |
| `mxf` | `mmf` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `mxf` | `rso` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `opus` | `mmf` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `ps` | `azw3` | Ghostscript > Calibre | Calibre could not convert this file. It may be damaged, password-protected, or use a feature it does |
| `ps` | `wiki` | Ghostscript > Poppler > pandoc | pandoc could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `psd` | `dcm` | ImageMagick > DCMTK | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `psd` | `emf` | ImageMagick > Inkscape | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `psd` | `exr` | ImageMagick > libjxl > libjxl | libjxl could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `psd` | `fodg` | ImageMagick > Inkscape > LibreOffice | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `psd` | `odg` | ImageMagick > Inkscape > LibreOffice | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `psd` | `svg` | ImageMagick > VTracer | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `psd` | `wmf` | ImageMagick > Inkscape | ImageMagick produced several files here, so this route can't continue. Choose another format. |
| `shp` | `gxt` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `shp` | `mif` | GDAL | GDAL could not convert this file. It may be damaged, password-protected, or use a feature it doesn't |
| `smi` | `cdjson` | Open Babel | Open Babel could not convert this file. It may be damaged, password-protected, or use a feature it d |
| `smi` | `pcjson` | Open Babel | Open Babel could not convert this file. It may be damaged, password-protected, or use a feature it d |
| `tiff` | `svg` | VTracer | VTracer could not convert this file. It may be damaged, password-protected, or use a feature it does |
| `toml` | `dta` | Settings files > Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `toml` | `sqlite` | Settings files > Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `vob` | `mmf` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `webm` | `dv` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `webm` | `mmf` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `wmf` | `cur` | LibreOffice > ImageMagick | ImageMagick could not convert this file. It may be damaged, password-protected, or use a feature it  |
| `wmf` | `ico` | LibreOffice > ImageMagick | ImageMagick could not convert this file. It may be damaged, password-protected, or use a feature it  |
| `wmf` | `rgf` | LibreOffice > ImageMagick | ImageMagick could not convert this file. It may be damaged, password-protected, or use a feature it  |
| `wmv` | `dv` | FFmpeg | FFmpeg could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `xml` | `dta` | Settings files > Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `xml` | `sqlite` | Settings files > Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `xyz` | `copy` | Open Babel | Open Babel could not convert this file. It may be damaged, password-protected, or use a feature it d |
| `xyz` | `fs` | Open Babel | Open Babel could not convert this file. It may be damaged, password-protected, or use a feature it d |
| `yaml` | `dta` | Settings files > Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
| `yaml` | `sqlite` | Settings files > Tables | Tables could not convert this file. It may be damaged, password-protected, or use a feature it doesn |
