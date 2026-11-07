"""Maps, 3D, CAD, chemistry, medical and scientific data."""

from __future__ import annotations

import re
import subprocess

from worker.engines.base import Engine, Simple, group


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=60, check=False, errors="replace").stdout


# VRT and its relatives are XML files that name other files to open, so they could read anything.
# The rest connect to servers or databases, which is meaningless with no network anyway.
_GDAL_DENY = {
    "VRT", "OGR_VRT", "GTI", "WMS", "WMTS", "WCS", "WFS", "OAPIF", "OGCAPI", "CSW", "PostgreSQL", "PostGISRaster",
    "MySQL", "MSSQLSpatial", "OCI", "ODBC", "PGeo", "DB2", "Memory", "MEM", "HTTP", "Elasticsearch", "MongoDBv3",
    "Carto", "AmigoCloud", "NGW", "PLMOSAIC", "PLSCENES", "EEDA", "EEDAI", "DAAS", "STACIT", "STACTA", "NULL",
    "GeoRSS", "SVG", "XLSX", "ODS", "CSV", "PDF", "PNG", "JPEG", "GIF", "BMP", "WEBP", "HTTP", "JP2OpenJPEG",
    "GPKG-raster-only", "Parquet", "Arrow", "SQLite", "XLS", "HDF5", "netCDF", "FITS", "DICOM", "PCIDSK",
}
_GDAL_LINE = re.compile(r"^\s*(\S.*?)\s+-([a-z, ]+)-\s+\(([a-z+]+)\):\s*(.*?)(?:\s+\((.*)\))?$")


class GDAL(Engine):
    name = "GDAL"
    binary = "gdal_translate"

    def __init__(self):
        self.vector: dict[str, str] = {}
        self.raster: dict[str, str] = {}

    def describe(self, db):
        vec_reads, vec_writes, ras_reads, ras_writes = set(), set(), set(), set()
        for tool, table, reads, writes in (
            ("ogrinfo", self.vector, vec_reads, vec_writes),
            ("gdalinfo", self.raster, ras_reads, ras_writes),
        ):
            for line in _run(tool, "--formats").splitlines():
                match = _GDAL_LINE.match(line)
                if not match:
                    continue
                driver, kinds, rights, title, ext_text = match.groups()
                if driver in _GDAL_DENY or not ext_text:
                    continue
                want = "vector" if tool == "ogrinfo" else "raster"
                if want not in kinds:
                    continue
                exts = re.findall(r"\*\.([A-Za-z0-9._-]+)", ext_text)
                if not exts:
                    continue
                writable_done = False
                for ext in exts:
                    fid = db.resolve(ext)
                    if fid is None:
                        fid = db.ensure(ext, title, "map")
                    if not fid or db.family(fid) != "map":
                        continue
                    table.setdefault(fid, driver)
                    if "r" in rights:
                        reads.add(fid)
                    # The driver's first extension of ours. GeoJSON lists *.json first, which is
                    # plain data here and not a map, and *.geojson second.
                    if not writable_done and ("w" in rights or "+" in rights):
                        writes.add(fid)
                        writable_done = True
        # A few formats, such as GeoPackage and KML, can hold either kind. Most real ones hold vector
        # data, and the two tools can't convert between the kinds, so those count as vector only.
        self.raster = {k: v for k, v in self.raster.items() if k not in self.vector}
        return [
            group(self.name, sorted(vec_reads), sorted(vec_writes), "lossy"),
            group(self.name, sorted(ras_reads - vec_reads), sorted(ras_writes - vec_reads - vec_writes), "lossy", priority=0.1),
        ]

    def export_data(self):
        return {"vector": self.vector, "raster": self.raster}

    def load_data(self, data):
        self.vector, self.raster = data["vector"], data["raster"]

    def env(self, step):
        return {
            "CPL_TMPDIR": str(step.tmp),
            "GDAL_PAM_ENABLED": "NO",
            "GDAL_SKIP": "VRT,WMS,WMTS,WCS,GTI",
            "OGR_SKIP": "OGR_VRT,WFS,OAPIF",
            # A lone .shp upload has no .shx beside it. This rebuilds the index from the file.
            "SHAPE_RESTORE_SHX": "YES",
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_CURL_VERBOSE": "NO",
        }

    def commands(self, step):
        # -if pins the input driver, so a file's contents can't choose a different one.
        if step.dst_fmt in self.vector:
            return [["ogr2ogr", "-if", self.vector[step.src_fmt], "-f", self.vector[step.dst_fmt], str(step.output), str(step.src)]]
        return [["gdal_translate", "-if", self.raster[step.src_fmt], "-of", self.raster[step.dst_fmt], str(step.src), str(step.output)]]


class Assimp(Engine):
    name = "Assimp"
    binary = "assimp"

    def __init__(self):
        self.exports: dict[str, str] = {}

    def describe(self, db):
        reads = []
        for ext in re.findall(r"\*\.([A-Za-z0-9]+)", _run("assimp", "listext")):
            fid = db.resolve(ext)
            if fid is None:
                fid = db.ensure(ext, None, "model", True)
            if fid and db.family(fid) in ("model", "cad"):
                reads.append(fid)
        for export_id in _run("assimp", "listexport").split():
            info = _run("assimp", "exportinfo", export_id).splitlines()
            if len(info) < 2 or export_id in ("assbin", "assxml", "assjson", "pbrt", "stlb", "plyb", "objnomtl", "fbxa", "gltf2", "glb2"):
                continue
            ext = info[1].strip()
            fid = db.resolve(ext)
            if fid is None:
                fid = db.ensure(ext, info[2].strip() if len(info) > 2 else None, "model", True)
            if fid and db.family(fid) in ("model", "cad"):
                self.exports.setdefault(fid, export_id)
        return [group(self.name, sorted(set(reads)), sorted(self.exports), "lossy")]

    def export_data(self):
        return {"exports": self.exports}

    def load_data(self, data):
        self.exports = data["exports"]

    def commands(self, step):
        return [["assimp", "export", str(step.src), str(step.output), f"-f{self.exports[step.dst_fmt]}"]]


class OpenBabel(Engine):
    name = "Open Babel"
    binary = "obabel"

    def __init__(self):
        self.tokens: dict[str, str] = {}

    def describe(self, db):
        reads, writes = set(), set()
        for mode, bucket in (("read", reads), ("write", writes)):
            for line in _run("obabel", "-L", "formats", mode).splitlines():
                match = re.match(r"^\s*(\S+)\s+--\s+(.*)$", line)
                if not match:
                    continue
                token, title = match.groups()
                if token == "pdb":
                    fid = "pdbx"  # .pdb here means Protein Data Bank; the table has it as Palm ebook
                else:
                    fid = db.resolve(token) or db.ensure(token, title, "science")
                if not fid or db.family(fid) != "science":
                    continue
                self.tokens.setdefault(fid, token)
                bucket.add(fid)
        return [group(self.name, sorted(reads), sorted(writes), "lossy")]

    def export_data(self):
        return {"tokens": self.tokens}

    def load_data(self, data):
        self.tokens = data["tokens"]

    def commands(self, step):
        return [["obabel", f"-i{self.tokens[step.src_fmt]}", str(step.src), f"-o{self.tokens[step.dst_fmt]}", "-O", str(step.output)]]


def _libredwg(s):
    if s.src_fmt == "dwg" and s.dst_fmt == "dxf":
        return ["dwg2dxf", "-y", "-o", s.output, s.src]
    if s.src_fmt == "dxf" and s.dst_fmt == "dwg":
        return ["dxf2dwg", "-y", "-o", s.output, s.src]
    if s.dst_fmt == "geojson":
        return ["dwgread", "-O", "GeoJSON", "-o", s.output, s.src]
    return ["dwgread", "-O", "JSON", "-o", s.output, s.src]


def _dcmtk(s):
    if s.src_fmt == "dcm":
        flag = {"png": "+on", "bmp": "+ob", "tiff": "+ot", "ppm": "+op"}[s.dst_fmt]
        return ["dcmj2pnm", flag, s.src, s.output]
    return ["img2dcm", "-i", "JPEG", s.src, s.output]


def technical_engines():
    return [
        GDAL(),
        Assimp(),
        OpenBabel(),
        Simple("OpenSCAD", "openscad", [(["scad"], ["stl", "off", "amf", "3mf", "dxf", "svg"], "lossy")],
               lambda s: ["openscad", "-o", s.output, s.src], env=lambda s: {"QT_QPA_PLATFORM": "offscreen"}),
        Simple("LibreDWG", "dwg2dxf", [
            (["dwg"], ["dxf"], "lossy"),
            (["dxf"], ["dwg"], "lossy"),
            (["dwg"], ["geojson"], "lossy", {"priority": 0.2}),
            (["dwg"], ["json"], "extract", {"priority": 0.2}),
        ], _libredwg),
        Simple("LibreDWG SVG", "dwg2SVG", [(["dwg"], ["svg"], "lossy", {"priority": 0.2})],
               lambda s: ["dwg2SVG", s.src], stdout=True),
        Simple("DCMTK", "dcmj2pnm", [
            (["dcm"], ["png", "bmp", "tiff", "ppm"], "lossy"),
            # JPEG only: img2dcm refuses the BMP headers most tools write today, so BMP goes through JPEG.
            (["jpg"], ["dcm"], "lossy"),
        ], _dcmtk),
        Simple("DCMTK XML", "dcm2xml", [(["dcm"], ["xml"], "extract")], lambda s: ["dcm2xml", s.src], stdout=True),
        Simple("DCMTK JSON", "dcm2json", [(["dcm"], ["json"], "extract")], lambda s: ["dcm2json", s.src], stdout=True),
        Simple("samtools", "samtools", [(["sam", "bam"], ["sam", "bam"], "exact")],
               lambda s: ["samtools", "view", "-@", str(s.threads), "-h", "-b" if s.dst_fmt == "bam" else "-O", *([] if s.dst_fmt == "bam" else ["SAM"]), "-o", s.output, s.src]),
        Simple("ncdump", "ncdump", [(["nc"], ["cdl"], "exact")], lambda s: ["ncdump", s.src], stdout=True),
        Simple("ncgen", "ncgen", [(["cdl"], ["nc"], "exact")], lambda s: ["ncgen", "-o", s.output, s.src]),
        Simple("h5dump", "h5dump", [(["hdf5"], ["txt"], "extract")], lambda s: ["h5dump", s.src], stdout=True),
    ]
